package tail

import (
	"bufio"
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"sort"
	"testing"

	"github.com/necklace/flip-signal/internal/flip"
)

// parity_test.go —— Go 引擎 ↔ python 回测的逐窗对账（**opt-in**）。
//
// 重放 data/btc/events_*.jsonl（14 天）驱动本包引擎，断言五格聚合等于
// python/v4/13_tail_sweep.py + 15_tail_sweep_union_sigma.py 的 oracle，断言监听
// 增量（B∖A）等于 16_tail_t150_scan.py 的 scan60∖snap60。
// 与 cmd/btreplay 同一形制（那边对 flip 做同样的事），区别只在本文件是**测试**：
// `go test` 触发、不新增交付二进制。
//
// ⚠️ 数据目录不存在即跳过（data/ 在 .gitignore 里，干净克隆不能因此失败）。
//
// 口径注入与回测 1:1（本文件不复制任何规则判定，只喂数据给引擎）:
//   - anchor = twap_open_price（settlement_correction 合并后，python load_events 同款）
//   - σ      = **按事件索引**前 ≤18 个事件 |close−open| 均值（≥3 个有值）——即
//     python hist_ranges 的语义，**不是** flip.HistState 的「最近 18 个已 push 振幅」:
//     两者在「窗口缺 open/close」时不等（缺的那窗在 python 里占索引位、不贡献值,
//     在 HistState 里连位置都不占）。回测基准就是前者，故这里现算后喂给 BeginWindow。
//   - 事件过滤 = 无 outcome 或无锚跳过（13_tail_sweep.py 的 snapshots 同款）
//   - 判定     = tail.Engine.BeginWindow + ProcessTick 逐 tick 驱动
//
// 行 ↔ python 的对应关系:
//
//	frame 行（rem ≤ frame_rem 150） ⇔ snapshots(T=150) 的首个有效 tick
//	snap  行（rem ≤ rem_start   60） ⇔ snapshots(T=60)   的首个有效 tick
//	scan  行（snap 之后首个达标）   ⇔ scan60∖snap60（16_tail_t150_scan.py 的监听增量）
//
// 两边**都要**再按「快照 tick 上 spot 与 twap 同时在场」过滤才是同一宇宙
// （python 是整窗丢弃，Go 是落一行 missing_spot/missing_twap 后继续——记录更全，
// 但聚合时必须对齐到 python 的宇宙）。
//
// oracle 数字由 13/15 的脚本在 2026-09-23 现算核对（见 docs/tail_engine_mapping_2026-09-23.md）。
var parityOracle = []struct {
	name string
	fn   func(o *Observation) bool
	n    int
	wr   float64 // 胜率（%）
	pl   float64 // 14 天 P&L（U, 每笔 2U）
}{
	{"① 热门侧 ask≥0.80", func(o *Observation) bool { return o.Rules.Rule1() }, 3109, 97.65, 72.10},
	{"② ①∧dev≥63美元", func(o *Observation) bool { return o.Rules.Rule2() }, 1458, 99.59, 32.69},
	{"③ ①∧dev≥1σ", func(o *Observation) bool { return o.Rules.Rule3() }, 1469, 99.46, 22.27},
	{"④ ①∧(dev≥63∨dev≥1σ)", func(o *Observation) bool { return o.Rules.Rule4() }, 1747, 99.37, 33.98},
	{"⑤ ①∧(dev≥63∨(1σ≥40美元∧dev≥1σ))", func(o *Observation) bool { return o.Rules.Rule5() }, 1536, 99.61, 36.93},
}

// paritySnap 把一条 snap 观测与它所属事件的官方结果配对（Observation 本身不带
// outcome——结算字段在 Record 段，本测试没有 recorder）。listen 行（KindScan）共用
// 同一形制——判定与 P&L 公式都相同，区别只在「有没有真下单」。
type paritySnap struct {
	obs     Observation
	outcome int
}

// parityScanOracle = 监听增量 B∖A 的 python oracle（2026-09-23 现算核对）:
// B（rem≤60 起首个达标 tick）n=2021 WR 99.26% +43.93U 减去 A（快照）n=1536
// WR 99.61% +36.93U。⚠️ 该增量**不显著**（日级 bootstrap 95% 区间跨 0:
// [−9.1, +22.5], 2000 次重采样）——它正是本行的存在理由: 只登记、不改引擎。
var parityScanOracle = struct {
	n  int
	wr float64
	pl float64
}{485, 98.14, 7.0}

// TestParityBacktest 是 14 天全量对账（唯一一条 Go↔py 红线测试）。
func TestParityBacktest(t *testing.T) {
	const dataDir = "../../data/btc"
	if _, err := os.Stat(dataDir); err != nil {
		t.Skipf("跳过: %s 不存在（回测数据不在仓库里, 见 .gitignore）", dataDir)
	}
	events := loadParityEvents(t, dataDir)
	if len(events) == 0 {
		t.Fatalf("%s 下没有事件", dataDir)
	}
	t.Logf("事件 %d 窗", len(events))

	cfg := DefaultConfig()
	var (
		frames, framesUni int // 帧行: 全部 / 通过 spot+twap 在场
		snaps, snapsUni   int // snap 行: 全部 / 同上
		scans             int // 监听增量行（B∖A, 只记录; 同样取 spot+twap 在场的宇宙）
		universe          []paritySnap
		scanUniverse      []paritySnap
	)
	for i := range events {
		ev := &events[i]
		if ev.Outcome == nil || !(ev.TwapOpen > 0) {
			continue
		}
		hb, ok := parityHistBps(events, i, ev.TwapOpen)
		if !ok {
			hb = 0 // 引擎 no_hist 兜底（= python hist_bps=None, σ 腿不可用）
		}
		eng := NewEngine(cfg)
		eng.BeginWindow(ev.TwapOpen, hb)

		// ⚠️ 不在 snap 行处 break: 监听段（只记录）要继续跑到闭市, 否则这条增量
		// 曲线永远没法与 python 的 scan60 对账（见 parityScanOracle）。
		for _, tk := range ev.Ticks {
			for _, o := range eng.ProcessTick(tk) {
				present := o.Spot > 0 && o.Twap > 0 // python 的宇宙: 缺一则整窗丢弃（13:99-101）
				switch o.Kind {
				case KindFrame:
					frames++
					if present {
						framesUni++
					}
				case KindSnap:
					snaps++
					if present {
						snapsUni++
						universe = append(universe, paritySnap{obs: o, outcome: *ev.Outcome})
					}
				case KindScan:
					// 宇宙同 snap: python 的 scan60 也要求该 tick spot+twap 在场
					//（valid_ticks 逐 tick 过滤），缺 twap 的 tick 它根本看不到。
					if present {
						scans++
						scanUniverse = append(scanUniverse, paritySnap{obs: o, outcome: *ev.Outcome})
					}
				}
			}
			if tk.Rem <= 0 {
				break
			}
		}
	}

	// 闩锁计数 = python snapshots(T) 的 n: T=150 → 3643, T=60 → 3634。
	// Go 侧「全部行数」只会更多（缺 spot/twap 的窗口照记一行）。
	if framesUni != 3643 {
		t.Errorf("T=150 宇宙窗数 = %d, 期望 3643（python snapshots(T=150) 的 n）", framesUni)
	}
	if snapsUni != 3634 {
		t.Errorf("T=60 宇宙窗数 = %d, 期望 3634（python snapshots(T=60) 的 n）", snapsUni)
	}
	if frames < framesUni || snaps < snapsUni {
		t.Errorf("全部行数 frame=%d snap=%d 不得少于宇宙窗数 %d/%d", frames, snaps, framesUni, snapsUni)
	}
	t.Logf("行数: frame %d（宇宙 %d）snap %d（宇宙 %d）scan %d", frames, framesUni, snaps, snapsUni, scans)

	// 监听增量（B∖A）与 python 的 scan60∖snap60 对账: n 必须是精确整数,
	// WR/P&L 两位小数内相等（同五格口径: 赢 shares−stake, 输 −stake）。
	if scans != parityScanOracle.n {
		t.Errorf("监听增量宇宙窗数 = %d, 期望 %d（python scan60∖snap60 的 n）", scans, parityScanOracle.n)
	}
	if scans > 0 {
		k := 0
		pl := 0.0
		for i := range scanUniverse {
			o := &scanUniverse[i].obs
			if flip.WonFor(o.Side, scanUniverse[i].outcome) {
				k++
				pl += cfg.Stake/o.HotAsk - cfg.Stake
			} else {
				pl -= cfg.Stake
			}
		}
		if wr := float64(k) / float64(scans) * 100; math.Abs(wr-parityScanOracle.wr) > 5e-3 {
			t.Errorf("监听增量 WR = %.4f%%, 期望 %.2f%%", wr, parityScanOracle.wr)
		}
		if math.Abs(pl-parityScanOracle.pl) > 5e-3 {
			t.Errorf("监听增量 P&L = %+.4fU, 期望 %+.2fU", pl, parityScanOracle.pl)
		}
	}

	// 五格聚合（P&L 口径 = 13_tail_sweep.py 的 stat: 赢 shares−stake, 输 −stake）
	for _, ora := range parityOracle {
		var n, k int
		pl := 0.0
		for i := range universe {
			o := &universe[i].obs
			if !ora.fn(o) {
				continue
			}
			n++
			if flip.WonFor(o.Side, universe[i].outcome) {
				k++
				pl += cfg.Stake/o.HotAsk - cfg.Stake
			} else {
				pl -= cfg.Stake
			}
		}
		if n != ora.n {
			t.Errorf("%s: n = %d, 期望 %d", ora.name, n, ora.n)
		}
		// 容差 5e-3 = oracle 表两位小数舍入的半个末位（n 是精确整数比对, 不容忍）。
		if n > 0 {
			if wr := float64(k) / float64(n) * 100; math.Abs(wr-ora.wr) > 5e-3 {
				t.Errorf("%s: WR = %.4f%%, 期望 %.2f%%", ora.name, wr, ora.wr)
			}
		}
		if math.Abs(pl-ora.pl) > 5e-3 {
			t.Errorf("%s: P&L = %+.4fU, 期望 %+.2fU", ora.name, pl, ora.pl)
		}
	}
}

// ── 数据加载（复刻 cmd/btreplay.loadEvents + python v2.lib.load_events 的合并语义）──

type parityTick struct {
	Ts  int64   `json:"ts"`
	Rem float64 `json:"rem"`
	PM  *struct {
		YesBid        float64 `json:"yes_bid"`
		YesAsk        float64 `json:"yes_ask"`
		NoBid         float64 `json:"no_bid"`
		NoAsk         float64 `json:"no_ask"`
		BookLatencyMs float64 `json:"book_latency_ms"`
	} `json:"pm"`
	Bin *struct {
		Price float64 `json:"price"`
	} `json:"bin"`
	Twap *struct {
		Price float64 `json:"price"`
		AgeMs float64 `json:"age_ms"`
	} `json:"twap"`
}

type parityEvent struct {
	EventType string       `json:"event_type"`
	Slug      string       `json:"slug"`
	StartTime int64        `json:"start_time"`
	Outcome   *int         `json:"outcome"`
	TwapOpen  float64      `json:"twap_open_price"`
	TwapClose float64      `json:"twap_close_price"`
	RawTicks  []parityTick `json:"ticks"`
	Ticks     []flip.Tick  `json:"-"`
}

// loadParityEvents 读全部 events_*.jsonl（按日升序）并合并 settlement_correction
// 行（官方口径覆盖流值口径）——语义与 cmd/btreplay.loadEvents 一致。
func loadParityEvents(t *testing.T, dir string) []parityEvent {
	t.Helper()
	paths, err := filepath.Glob(filepath.Join(dir, "events_*.jsonl"))
	if err != nil {
		t.Fatalf("glob: %v", err)
	}
	sort.Strings(paths)
	type corr struct {
		StartTime int64   `json:"start_time"`
		Outcome   *int    `json:"outcome"`
		TwapOpen  float64 `json:"twap_open_price"`
		TwapClose float64 `json:"twap_close_price"`
	}
	corrections := map[int64]corr{}
	var events []parityEvent
	for _, p := range paths {
		f, err := os.Open(p)
		if err != nil {
			t.Fatalf("open %s: %v", p, err)
		}
		sc := bufio.NewScanner(f)
		sc.Buffer(make([]byte, 0, 1<<20), 64<<20)
		for sc.Scan() {
			line := sc.Bytes()
			if len(line) == 0 {
				continue
			}
			var ev parityEvent
			if err := json.Unmarshal(line, &ev); err != nil {
				t.Fatalf("decode %s: %v", p, err)
			}
			if ev.EventType == "settlement_correction" {
				var c corr
				if err := json.Unmarshal(line, &c); err != nil {
					t.Fatalf("decode correction %s: %v", p, err)
				}
				corrections[c.StartTime] = c
				continue
			}
			events = append(events, ev)
		}
		f.Close()
	}
	for i := range events {
		if c, ok := corrections[events[i].StartTime]; ok {
			if c.Outcome != nil {
				events[i].Outcome = c.Outcome
			}
			if c.TwapOpen != 0 {
				events[i].TwapOpen = c.TwapOpen
			}
			if c.TwapClose != 0 {
				events[i].TwapClose = c.TwapClose
			}
		}
		// 原始 tick → flip.Tick（缺失即 0，与 btreplay 同款——引擎门控自己判无效）
		for _, rt := range events[i].RawTicks {
			tk := flip.Tick{Ts: rt.Ts, Rem: int(rt.Rem)}
			if rt.PM != nil {
				tk.UpBid, tk.UpAsk = rt.PM.YesBid, rt.PM.YesAsk
				tk.DownBid, tk.DownAsk = rt.PM.NoBid, rt.PM.NoAsk
				tk.BookLatMs = int64(rt.PM.BookLatencyMs)
			}
			if rt.Bin != nil {
				tk.BinPrice = rt.Bin.Price
			}
			if rt.Twap != nil {
				tk.TwapPrice = rt.Twap.Price
				tk.TwapAgeMs = int64(rt.Twap.AgeMs)
			}
			events[i].Ticks = append(events[i].Ticks, tk)
		}
	}
	sort.Slice(events, func(i, j int) bool { return events[i].StartTime < events[j].StartTime })
	return events
}

// parityHistBps 复刻 python hist_ranges: 事件 i 的 σ = 前 ≤18 个事件 |close−open|
// 均值（≥3 个有值）→ bps = σ/anchor×1e4。
// ⚠️ 窗口按「事件」计——无结算/缺端点的索引位置同样占位（与内部 flip.HistState 不同,
// 后者只记已 push 的振幅，见文件头）。
func parityHistBps(events []parityEvent, i int, anchor float64) (float64, bool) {
	n := 0
	sum := 0.0
	for j := i - 18; j < i; j++ {
		if j < 0 {
			continue
		}
		e := &events[j]
		if e.TwapOpen > 0 && e.TwapClose > 0 { // python 真值语义（0 视为缺）
			sum += math.Abs(e.TwapClose - e.TwapOpen)
			n++
		}
	}
	if n < 3 {
		return 0, false
	}
	return sum / float64(n) / anchor * 1e4, true
}
