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

// parity_test.go —— Go 引擎 ↔ python oracle 的逐窗对账（**opt-in**）。
//
// 重放 data/btc/events_*.jsonl（14 天）驱动本包引擎，断言**三段递进判定链**的
// 各段行数 / 信号数 / 胜率 / P&L 等于 python/v4/23_tail_integrated.py 的 oracle
// （该脚本第六节直接打印本文件照抄的那张表）。与 cmd/btreplay 同一形制（那边对
// flip 做同样的事），区别只在本文件是**测试**：`go test` 触发、不新增交付二进制。
//
// ⚠️ 数据目录不存在即跳过（data/ 在 .gitignore 里，干净克隆不能因此失败）。
//
// 口径注入与 oracle 1:1（本文件**不复制任何规则判定**，只喂数据给引擎）:
//   - anchor = twap_open_price（settlement_correction 合并后，python load_events 同款）
//   - σ      = **按事件索引**前 ≤18 个事件 |close−open| 均值（≥3 个有值）——即
//     python hist_ranges 的语义，**不是** flip.HistState 的「最近 18 个已 push 振幅」:
//     两者在「窗口缺 open/close」时不等（缺的那窗在 python 里占索引位、不贡献值,
//     在 HistState 里连位置都不占）。回测基准就是前者，故这里现算后喂给 BeginWindow。
//   - **σ 未就绪（<3 个可用窗）整窗跳过**——与 cmd/tail 的前置闸同源（决策 #13:
//     `hist.Count() < HistMin` ⇒ 整窗不产出观测）。历史 14 天里恰好 3 窗, 但它们是
//     「引擎落一行 hist_bps=0 的观测」与「oracle 一行不产」的分水岭, 不跳过必然对不上。
//   - 事件过滤 = 无 outcome 或无锚跳过（13_tail_sweep.py 的 snapshots 同款）
//   - 判定     = tail.Engine.BeginWindow + ProcessTick 逐 tick 驱动
//
// 行 ↔ oracle 的对应关系（stage 字段直接可比, 不再需要按 kind 反推）:
//
//	t150 行（首个 rem ≤ 150 的可判定 tick）⇔ oracle chain() 的第一段
//	t60  行（t150 被拒后首个 rem ≤ 60 的可判定 tick）⇔ 第二段
//	listen 行（前两段都没信号后, 首个 ② 达标的 tick）⇔ 第三段（只在达标时落行）
//
// ⚠️ 需要 audited、但不需断言的已知事实（oracle 第五节实测, 2026-09-24 数据）:
// rem ≤ 150 的 542800 个 tick 里「**四档部分缺**」= 0（单侧空簿一次都没有）⇒ Go 的
// 「有效价 ask→bid 兜底」门与 python 的「四档齐全」门在 14 天数据上**完全同源**,
// 故本对账对空侧放宽零曝光——空侧是纯 live 行为改动（见 docs/tail_integrated_2026-09-24.md §2）。
// 「整簿全空」（四档齐 0）361 个 tick 两套门都判无效, 也不影响。
var parityStages = []struct {
	stage string
	rows  int     // 该段全部行（判定行 + 信号行）
	sig   int     // 信号行（ok=true）
	wr    float64 // 信号胜率（%）
	pl    float64 // 14 天 P&L（U, 每笔 2U）
}{
	{StageT150, 3638, 1220, 93.934426, 16.022637},
	{StageT60, 2414, 568, 99.119718, 15.756180},
	{StageListen, 347, 347, 97.982709, 3.895932},
}

// parityTotals = 三段合计（oracle 第六节末行）。
const (
	parityAllRows   = 6399
	parityAllSig    = 2135
	parityAllWR     = 95.971897
	parityAllPL     = 35.674749
	parityWindows   = 3640 // 参与的窗数（有 ≥1 个 rem ≤ 150 可判定 tick 且有 σ）
	parityNoSigma   = 3    // σ 未就绪整窗跳过（决策 #13 的前置闸）
	parityTolerance = 5e-5 // oracle 打印 6 位小数 ⇒ 容差取其末位之半; 实测两边差 <1e-9
)

// parityRow 把一条观测与它所属事件的官方结果配对（Observation 本身不带 outcome
// ——结算字段在 Record 段，本测试没有 recorder）。
type parityRow struct {
	obs     Observation
	outcome int
}

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
	rows := map[string]int{}             // 各段全部行数
	universe := map[string][]parityRow{} // 各段信号行（ok=true）
	windows, noSigma, bidFallback := 0, 0, 0

	for i := range events {
		ev := &events[i]
		if ev.Outcome == nil || !(ev.TwapOpen > 0) {
			continue
		}
		hb, ok := parityHistBps(events, i, ev.TwapOpen)
		if !ok {
			noSigma++ // σ 未就绪 ⇒ 整窗跳过（与 cmd/tail 的前置闸同源）
			continue
		}
		eng := NewEngine(cfg)
		eng.BeginWindow(ev.TwapOpen, hb)

		emitted := 0
		for _, tk := range ev.Ticks {
			o := eng.ProcessTick(tk)
			if o == nil {
				if tk.Rem <= 0 {
					break // rem==0 终 tick: 窗口结束
				}
				continue
			}
			// 引擎的行形态守卫: 唯一 kind + 三个已知 stage（legacy kind 不再产出）。
			if o.Kind != KindSnap {
				t.Fatalf("窗 %d: 行 kind = %q, 期望 %q（引擎只产出这一种）", ev.StartTime, o.Kind, KindSnap)
			}
			if o.Stage != StageT150 && o.Stage != StageT60 && o.Stage != StageListen {
				t.Fatalf("窗 %d: 未知 stage %q", ev.StartTime, o.Stage)
			}
			// 可判定 tick 的门控: spot 必在场、锚必 >0（无锚整窗不产出）。
			if !(o.Spot > 0) || !(o.Anchor > 0) {
				t.Fatalf("窗 %d: 行 spot=%.4f anchor=%.4f（都须 >0）", ev.StartTime, o.Spot, o.Anchor)
			}
			// 判定行内部一致性 + 拒绝原因只能是这两条（missing_spot/no_hist 是纯函数
			// 防线, 现网到不了——若出现说明引擎的前置门控被绕过了）。
			if o.OK {
				if o.Stage == StageListen {
					if !o.Rules.Rule2() {
						t.Fatalf("窗 %d: listen 信号行不满足 ②: %+v", ev.StartTime, o.Rules)
					}
				} else if !o.Rules.Rule5() {
					t.Fatalf("窗 %d: %s 信号行不满足 ⑤: %+v", ev.StartTime, o.Stage, o.Rules)
				}
				if o.RejectReason != "" {
					t.Fatalf("窗 %d: 信号行不该带 reject_reason %q", ev.StartTime, o.RejectReason)
				}
			} else {
				switch o.RejectReason {
				case RejectPriceLow:
					if o.Rules.Price {
						t.Fatalf("窗 %d: price_low 但价格腿为真: %+v", ev.StartTime, o.Rules)
					}
				case RejectLegOut:
					if !o.Rules.Price || o.Rules.Rule5() {
						t.Fatalf("窗 %d: leg_out 但价格腿为假/⑤ 已达标: %+v", ev.StartTime, o.Rules)
					}
				default:
					t.Fatalf("窗 %d: %s 判定行的拒绝原因 = %q（只应是 price_low/leg_out）",
						ev.StartTime, o.Stage, o.RejectReason)
				}
			}
			// 空侧兜底在 14 天数据上零命中（全部走 ask）——bid 一旦出现即 oracle 宇宙分家。
			if o.HotSrc != BookSrcAsk {
				bidFallback++
				t.Errorf("窗 %d: 行取价来源 = %q（14 天里应为恒 %q, 见文件头）",
					ev.StartTime, o.HotSrc, BookSrcAsk)
			}
			rows[o.Stage]++
			emitted++
			if o.OK {
				universe[o.Stage] = append(universe[o.Stage], parityRow{obs: *o, outcome: *ev.Outcome})
			}
			if o.OK {
				break // 整窗只下一单: 出信号即止
			}
		}
		if emitted > 0 {
			windows++
		}
	}

	if windows != parityWindows {
		t.Errorf("参与判定的窗数 = %d, 期望 %d（oracle 的 windows）", windows, parityWindows)
	}
	if noSigma != parityNoSigma {
		t.Errorf("σ 未就绪跳过 = %d 窗, 期望 %d", noSigma, parityNoSigma)
	}

	// 各段行数 / 信号数（**精确整数**）+ 胜率 / P&L（容差 5e-5）——oracle 第六节。
	var allRows, allSig int
	allPL := 0.0
	allWon := 0
	for _, ps := range parityStages {
		if got := rows[ps.stage]; got != ps.rows {
			t.Errorf("%s 行数 = %d, 期望 %d", ps.stage, got, ps.rows)
		}
		uni := universe[ps.stage]
		if len(uni) != ps.sig {
			t.Errorf("%s 信号数 = %d, 期望 %d", ps.stage, len(uni), ps.sig)
		}
		allRows += rows[ps.stage]
		allSig += len(uni)
		if len(uni) == 0 {
			continue
		}
		// P&L 口径 = oracle 的 pl(): 赢 shares−stake, 输 −stake, shares = stake/有效价。
		won, pl := 0, 0.0
		for k := range uni {
			o := &uni[k].obs
			if flip.WonFor(o.Side, uni[k].outcome) {
				won++
				pl += cfg.Stake/o.HotAsk - cfg.Stake
			} else {
				pl -= cfg.Stake
			}
		}
		if wr := float64(won) / float64(len(uni)) * 100; math.Abs(wr-ps.wr) > parityTolerance {
			t.Errorf("%s WR = %.6f%%, 期望 %.6f%%", ps.stage, wr, ps.wr)
		}
		if math.Abs(pl-ps.pl) > parityTolerance {
			t.Errorf("%s P&L = %+.6fU, 期望 %+.6fU", ps.stage, pl, ps.pl)
		}
		allWon, allPL = allWon+won, allPL+pl
		t.Logf("%s: 行 %d / 信号 %d / WR %.4f%% / P&L %+.4fU",
			ps.stage, rows[ps.stage], len(uni), float64(won)/float64(len(uni))*100, pl)
	}

	if allRows != parityAllRows {
		t.Errorf("三段合计行数 = %d, 期望 %d", allRows, parityAllRows)
	}
	if allSig != parityAllSig {
		t.Errorf("三段合计信号数 = %d, 期望 %d", allSig, parityAllSig)
	}
	if allSig > 0 {
		if wr := float64(allWon) / float64(allSig) * 100; math.Abs(wr-parityAllWR) > parityTolerance {
			t.Errorf("三段合计 WR = %.6f%%, 期望 %.6f%%", wr, parityAllWR)
		}
	}
	if math.Abs(allPL-parityAllPL) > parityTolerance {
		t.Errorf("三段合计 P&L = %+.6fU, 期望 %+.6fU", allPL, parityAllPL)
	}
	if bidFallback > 0 {
		t.Errorf("有 %d 行的有效价来自 bid —— 与 oracle 的「四档齐全」宇宙分家, 须先对齐口径", bidFallback)
	}
	t.Logf("合计: 行 %d / 信号 %d / 窗 %d / σ跳过 %d / WR %.4f%% / P&L %+.4fU",
		allRows, allSig, windows, noSigma, float64(allWon)/float64(allSig)*100, allPL)
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
