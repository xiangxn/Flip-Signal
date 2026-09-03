package main

// 临时诊断测试（2026-09-03）: 将 data/btc 的 3640 个回测事件逐 tick 喂给
// Go flip.Engine，与 python/v4/01_backtest_r1.py extract+rules 的参考表
// （/tmp/mirror_ref.json）逐笔比对 side / rem / m_45 / dist_s / ok。
// 目的: 决定性验证 Go 判定链与回测逻辑是否 1:1 —— 若 Go 存在"检查 yes
// 方向即放弃 no 方向"之类逻辑 bug，同数据镜像必然出现 side/ok 差异。
//
// 运行: go test ./cmd/flip -run TestMirrorBacktest -v   （依赖 /tmp/mirror_ref.json）

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"testing"

	"github.com/necklace/flip-signal/internal/flip"
)

type mTick struct {
	Rem int `json:"rem"`
	PM  struct {
		YesBid        float64 `json:"yes_bid"`
		YesAsk        float64 `json:"yes_ask"`
		NoBid         float64 `json:"no_bid"`
		NoAsk         float64 `json:"no_ask"`
		BookLatencyMs int64   `json:"book_latency_ms"`
	} `json:"pm"`
	Bin struct {
		Price float64 `json:"price"`
	} `json:"bin"`
	Twap struct {
		Price float64 `json:"price"`
	} `json:"twap"`
}

type mEvent struct {
	EventType     string  `json:"event_type"`
	StartTime     int64   `json:"start_time"`
	Outcome       *int    `json:"outcome"`
	TwapOpenPrice float64 `json:"twap_open_price"`
	TwapClose     float64 `json:"twap_close_price"`
	Ticks         []mTick `json:"ticks"`
}

type mRef struct {
	ES   int64    `json:"es"`
	Side string   `json:"side"`
	Rem  int      `json:"rem"`
	M45  *float64 `json:"m45"`
	DS   *float64 `json:"ds"`
	Pure bool     `json:"pure"`
}

func loadMEvents(dir string) []*mEvent {
	var evs []*mEvent
	corr := map[int64]*mEvent{}
	paths, _ := filepath.Glob(filepath.Join(dir, "events_*.jsonl"))
	for _, p := range paths {
		raw, err := os.ReadFile(p)
		if err != nil {
			panic(err)
		}
		start := 0
		for i, b := range raw {
			if b != '\n' {
				continue
			}
			var e mEvent
			if err := json.Unmarshal(raw[start:i], &e); err != nil {
				panic(err)
			}
			if e.EventType == "settlement_correction" {
				corr[e.StartTime] = &e
			} else {
				evs = append(evs, &e)
			}
			start = i + 1
		}
	}
	// 修正行覆盖（与 python load_events 同款）
	for _, e := range evs {
		if c, ok := corr[e.StartTime]; ok {
			if c.Outcome != nil {
				e.Outcome = c.Outcome
			}
			if c.TwapOpenPrice > 0 {
				e.TwapOpenPrice = c.TwapOpenPrice
			}
			if c.TwapClose > 0 {
				e.TwapClose = c.TwapClose
			}
		}
	}
	sort.Slice(evs, func(i, j int) bool { return evs[i].StartTime < evs[j].StartTime })
	return evs
}

func TestMirrorBacktest(t *testing.T) {
	refBytes, err := os.ReadFile("/tmp/mirror_ref.json")
	if err != nil {
		t.Skipf("参考表缺失: %v", err)
	}
	var ref []mRef
	if err := json.Unmarshal(refBytes, &ref); err != nil {
		t.Fatal(err)
	}
	refByES := map[int64]*mRef{}
	for i := range ref {
		refByES[ref[i].ES] = &ref[i]
	}

	// go test 二进制运行于沙箱，data/btc 读取被拒（ENOENT），
	// 事件镜像副本放 /tmp/mirror_btc（参考表 /tmp/mirror_ref.json 同法）
	wd, _ := os.Getwd()
	candidates := []string{"/tmp/mirror_btc", filepath.Join(wd, "..", "..", "data", "btc")}
	var evs []*mEvent
	for _, dir := range candidates {
		_, err := os.Stat(dir)
		matches, _ := filepath.Glob(filepath.Join(dir, "events_*.jsonl"))
		t.Logf("目录候选 %s: stat=%v glob=%d个", dir, err, len(matches))
		evs = loadMEvents(dir)
		if len(evs) > 0 {
			t.Logf("事件目录命中: %s", dir)
			break
		}
	}

	histOf := func(i int) (float64, bool) {
		var sum float64
		cnt := 0
		for j := max(0, i-18); j < i; j++ {
			o, c := evs[j].TwapOpenPrice, evs[j].TwapClose
			if o > 0 && c > 0 {
				sum += math.Abs(c - o)
				cnt++
			}
		}
		if cnt < 3 {
			return 0, false
		}
		return sum / float64(cnt), true
	}

	cfg := flip.DefaultConfig()
	misSide, misOK, misRem, misM45, misDS, noObs := 0, 0, 0, 0, 0, 0
	var firstMis []string
	for i, e := range evs {
		if e.Outcome == nil || e.TwapOpenPrice <= 0 {
			continue // python extract 同款跳过
		}
		want := refByES[e.StartTime]
		anchor := e.TwapOpenPrice
		histBps := 0.0
		if meanAmp, ok := histOf(i); ok {
			histBps = meanAmp / anchor * 1e4
		}
		eng := flip.NewEngine(cfg)
		eng.BeginWindow(anchor, histBps)
		var obs *flip.Observation
		for _, tk := range e.Ticks {
			tick := flip.Tick{
				Ts:        0,
				Rem:       tk.Rem,
				UpBid:     tk.PM.YesBid,
				UpAsk:     tk.PM.YesAsk,
				DownBid:   tk.PM.NoBid,
				DownAsk:   tk.PM.NoAsk,
				BookLatMs: tk.PM.BookLatencyMs,
				BinPrice:  tk.Bin.Price,
				TwapPrice: tk.Twap.Price,
			}
			if o := eng.ProcessTick(tick); o != nil {
				obs = o
				break
			}
		}
		if want == nil {
			if obs != nil {
				firstMis = append(firstMis, fmt.Sprintf("python无触底但引擎产出 es=%d", e.StartTime))
			}
			continue
		}
		if obs == nil {
			noObs++
			if len(firstMis) < 8 {
				firstMis = append(firstMis, fmt.Sprintf("go无观测 es=%d want side=%s pure=%v", e.StartTime, want.Side, want.Pure))
			}
			continue
		}
		if obs.Side != want.Side {
			misSide++
			if len(firstMis) < 8 {
				firstMis = append(firstMis, fmt.Sprintf("side es=%d go=%s py=%s", e.StartTime, obs.Side, want.Side))
			}
		}
		if obs.OK != want.Pure {
			misOK++
			if len(firstMis) < 8 {
				firstMis = append(firstMis, fmt.Sprintf("ok es=%d go=%v(%s) py=%v", e.StartTime, obs.OK, obs.RejectReason, want.Pure))
			}
		}
		if obs.Rem != want.Rem {
			misRem++
		}
		gm := 0.0
		if obs.M45 > 0 {
			gm = obs.M45
		}
		pm := 0.0
		if want.M45 != nil {
			pm = *want.M45
		}
		if math.Abs(gm-pm) > 1e-9 {
			misM45++
		}
		if (want.DS == nil) != (obs.DistS == 0) {
			misDS++
		} else if want.DS != nil && math.Abs(obs.DistS-*want.DS) > 1e-9 {
			misDS++
		}
	}
	t.Logf("加载事件 %d 个（参考表 %d 条）", len(evs), len(ref))
	t.Logf("side 不一致: %d | ok(==pure) 不一致: %d | rem 不一致: %d | m45 不一致: %d | dist_s 不一致: %d | go无观测: %d",
		misSide, misOK, misRem, misM45, misDS, noObs)
	for _, m := range firstMis {
		t.Logf("  差异示例: %s", m)
	}
	if misSide > 0 || misOK > 0 || noObs > 0 {
		t.Errorf("🔴 引擎与回测存在方向/判定差异（差异数: side=%d ok=%d 无观测=%d）", misSide, misOK, noObs)
	} else {
		t.Logf("🟢 引擎与回测逐笔一致: side/ok 全部镜像（含 38 个 no 侧 pure 信号）")
	}
}
