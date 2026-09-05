// 离线回测：用当前 flip.Engine 判定逻辑重放历史事件数据（Go↔py 口径对账工具）。
//
// 对照基准：python/v4/01_backtest_r1.py 的组合版纯现货头条（引擎 DefaultConfig =
// 组合带 yes(−0.6,0)+no(−1,0)）：trades_r1_combo.csv in_pure==1 → n=625, WR 24.6%,
// EV +0.633U/注, +395.8U/14 天。
//
// 口径注入与回测 1:1（本工具不复制任何规则判定，只喂数据给引擎）：
//   - anchor   = twap_open_price（settlement_correction 合并后，python load_events 同款）
//   - σ        = python hist_ranges 同款：按事件索引前 ≤18 个事件 |close−open|
//     均值（≥3 个有值），hist_bps = σ/anchor×1e4
//   - 事件过滤 = 无 outcome 或无锚跳过（01_backtest_r1.py :86 同款）
//   - 判定     = flip.Engine.BeginWindow + ProcessTick 逐 tick 驱动（引擎私有常数、
//     四字段门控、急跌窗 ring、判定顺序原样生效），首个观测即停
//
// live 数据源 seam（锚=边界 TWAP 流值 vs 官方开盘、σ 推窗门控、spot 2s 新鲜度、
// rem==0 终 tick）不属于本工具范围——那是 live vs 镜像数据的口径差，另行评估。
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"time"

	"github.com/necklace/flip-signal/internal/flip"
)

// rawTick 与 data/btc 记录 schema 对应（v2 采集器 1s tick）。
// 盘口/spot/TWAP 缺失时价格为 0——喂给引擎后由引擎门控处理（与回测 tick 一致）。
type rawTick struct {
	Ts  int64   `json:"ts"`
	Rem float64 `json:"rem"`
	PM  *struct {
		YesBid        float64 `json:"yes_bid"`
		YesAsk        float64 `json:"yes_ask"`
		NoBid         float64 `json:"no_bid"`
		NoAsk         float64 `json:"no_ask"`
		BookLatencyMs float64 `json:"book_latency_ms"`
	} `json:"pm"`
	Bin  *struct {
		Price float64 `json:"price"`
	} `json:"bin"`
	Twap *struct {
		Price float64 `json:"price"`
		AgeMs float64 `json:"age_ms"`
	} `json:"twap"`
}

type rawEvent struct {
	EventType  string    `json:"event_type"`
	StartTime  int64     `json:"start_time"`
	Outcome    *int      `json:"outcome"`
	TwapOpen   float64   `json:"twap_open_price"`
	TwapClose  float64   `json:"twap_close_price"`
	Ticks      []rawTick `json:"ticks"`
	mergedFrom string    // 本窗有 settlement_correction 覆盖（诊断）
}

func loadEvents(dataDir string) []rawEvent {
	paths, err := filepath.Glob(filepath.Join(dataDir, "events_*.jsonl"))
	if err != nil {
		fatal("glob events: %v", err)
	}
	sort.Strings(paths) // 按日升序（python 同款）
	type corr struct {
		StartTime int64   `json:"start_time"`
		Outcome   *int    `json:"outcome"`
		TwapOpen  float64 `json:"twap_open_price"`
		TwapClose float64 `json:"twap_close_price"`
	}
	corrections := map[int64]corr{}
	events := []rawEvent{}
	for _, p := range paths {
		f, err := os.Open(p)
		if err != nil {
			fatal("open %s: %v", p, err)
		}
		sc := bufio.NewScanner(f)
		sc.Buffer(make([]byte, 0, 1<<20), 64<<20)
		for sc.Scan() {
			line := sc.Text()
			if line == "" {
				continue
			}
			var ev rawEvent
			if err := json.Unmarshal([]byte(line), &ev); err != nil {
				fatal("decode %s: %v", p, err)
			}
			if ev.EventType == "settlement_correction" {
				// 合并语义复刻 python load_events: 按 start_time 归并官方口径行
				var c corr
				_ = json.Unmarshal([]byte(line), &c)
				corrections[ev.StartTime] = c
				continue
			}
			events = append(events, ev)
		}
		f.Close()
	}
	// 官方口径覆盖流值口径（python: e.update({k:v for k,v in corr if k!='event_type'})）
	nMerge := 0
	for i := range events {
		c, ok := corrections[events[i].StartTime]
		if !ok {
			continue
		}
		if c.Outcome != nil {
			events[i].Outcome = c.Outcome
		}
		if c.TwapOpen != 0 {
			events[i].TwapOpen = c.TwapOpen
		}
		if c.TwapClose != 0 {
			events[i].TwapClose = c.TwapClose
		}
		events[i].mergedFrom = "correction"
		nMerge++
	}
	sort.Slice(events, func(i, j int) bool { return events[i].StartTime < events[j].StartTime })
	fmt.Printf("事件 %d（correction 覆盖 %d 窗）\n", len(events), nMerge)
	return events
}

// histBps 复刻 python hist_ranges: 事件 i 的 σ = 前 ≤18 个事件 |close−open| 均值
// （≥3 个有值）→ bps = σ/anchor×1e4。注意窗口按「事件」计（python 同款，
// 非按有值窗计），无结算事件的索引位置同样占位。
func histBps(events []rawEvent, i int, anchor float64) (float64, bool) {
	n := 0
	sum := 0.0
	for j := i - 18; j < i; j++ {
		if j < 0 {
			continue
		}
		e := &events[j]
		if e.TwapOpen > 0 && e.TwapClose > 0 { // python 真值语义（0 视为缺）
			sum += abs(e.TwapClose - e.TwapOpen)
			n++
		}
	}
	if n < 3 {
		return 0, false
	}
	return sum / float64(n) / anchor * 1e4, true
}

func abs(v float64) float64 {
	if v < 0 {
		return -v
	}
	return v
}

func fnum(v float64) string {
	if v == 0 {
		return "" // python NaN 语义（未计算/窗口无有效 ask）
	}
	return strconv.FormatFloat(v, 'g', -1, 64)
}

func main() {
	dataDir := flag.String("data", "data/btc", "事件数据目录（data/btc）")
	outPath := flag.String("out", "btreplay_out.tsv", "ok 行明细输出（TSV）")
	flag.Parse()

	events := loadEvents(*dataDir)

	// 明细输出（对照 trades_r1_combo.csv in_pure==1 的列）
	w, err := os.Create(*outPath)
	if err != nil {
		fatal("create %s: %v", *outPath, err)
	}
	bw := bufio.NewWriter(w)
	fmt.Fprintln(bw, "date\tevent_start\tside\trem\tfill\tm_20\tm_30\tm_45\tdist_s\tdist_t\tsettle_won")

	cfg := flip.DefaultConfig() // 组合带 yes(−0.6,0)+no(−1,0) = 引擎现行
	nOk, nWin := 0, 0
	pnlSum := 0.0
	sideCnt := map[string]int{}
	skipped := map[string]int{} // 事件级跳过原因计数（诊断，无 outcome 现网为 0）
	var rows []string

	for i := range events {
		ev := &events[i]
		if ev.Outcome == nil {
			skipped["no_outcome"]++
			continue
		}
		if !(ev.TwapOpen > 0) {
			skipped["no_anchor"]++
			continue
		}
		hb, ok := histBps(events, i, ev.TwapOpen)
		if !ok {
			hb = 0 // 引擎 no_hist 兜底（dist 不计算，与 python hist_bps=None 等价）
		}

		eng := flip.NewEngine(cfg)
		eng.BeginWindow(ev.TwapOpen, hb)

		var obs *flip.Observation
		for _, t := range ev.Ticks {
			tk := flip.Tick{
				Ts:        t.Ts,
				Rem:       int(t.Rem),
				BinPrice:  binPrice(t),
				TwapPrice: twapPrice(t),
			}
			if t.PM != nil {
				tk.UpBid = t.PM.YesBid
				tk.UpAsk = t.PM.YesAsk
				tk.DownBid = t.PM.NoBid
				tk.DownAsk = t.PM.NoAsk
				tk.BookLatMs = int64(t.PM.BookLatencyMs)
			}
			if t.Twap != nil {
				tk.TwapAgeMs = int64(t.Twap.AgeMs)
			}
			obs = eng.ProcessTick(tk)
			if obs != nil || t.Rem <= 0 {
				break // 已产出观测；rem==0 终 tick 后引擎转 Done，不再喂
			}
		}
		if obs == nil || !obs.OK {
			continue
		}
		won := 0
		if (obs.Side == flip.SideYes && *ev.Outcome == flip.OutcomeUp) ||
			(obs.Side == flip.SideNo && *ev.Outcome == flip.OutcomeDown) {
			won = 1
		}
		shares := cfg.Stake / obs.Fill
		pnl := -cfg.Stake
		if won == 1 {
			pnl = shares - cfg.Stake
		}
		nOk++
		nWin += won
		pnlSum += pnl
		sideCnt[obs.Side]++
		rows = append(rows, fmt.Sprintf("%s\t%d\t%s\t%d\t%s\t%s\t%s\t%s\t%s\t%s\t%d",
			time.Unix(ev.StartTime, 0).UTC().Format("2006-01-02"),
			ev.StartTime, obs.Side, obs.Rem, fnum(obs.Fill),
			fnum(obs.M20), fnum(obs.M30), fnum(obs.M45),
			fnum(obs.DistS), fnum(obs.DistT), won))
	}
	for _, r := range rows {
		fmt.Fprintln(bw, r)
	}
	bw.Flush()
	w.Close()

	fmt.Printf("ok n=%d  WR %.1f%%  EV %+.3fU/注  P&L %+.1fU  side %v\n",
		nOk, float64(nWin)/float64(nOk)*100, pnlSum/float64(nOk), pnlSum, sideCnt)
	if len(skipped) > 0 {
		fmt.Printf("事件跳过: %v\n", skipped)
	}
	fmt.Printf("明细已写 %s\n", *outPath)
}

func binPrice(t rawTick) float64 {
	if t.Bin != nil {
		return t.Bin.Price
	}
	return 0
}

func twapPrice(t rawTick) float64 {
	if t.Twap != nil {
		return t.Twap.Price
	}
	return 0
}

func fatal(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "btreplay: "+format+"\n", args...)
	os.Exit(1)
}
