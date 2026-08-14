package flip

import (
	"bufio"
	"encoding/json"
	"os"
	"testing"

	"github.com/necklace/flip-signal/internal/lab"
)

// eventFile 定义回放用的 JSONL 事件文件（cmd/flip 的 lab-output 格式）。
type eventFile struct {
	ConditionID string `json:"condition_id"`
	StartTime   int64  `json:"start_time"`
	OpenPrice   float64 `json:"open_price"`
	ClosePrice  float64 `json:"close_price"`
	Outcome     int    `json:"outcome"`
	Snapshots   []struct {
		Ts               int64   `json:"ts"`
		RemainingSec     int     `json:"remaining_sec"`
		Open             float64 `json:"open"`
		Price            float64 `json:"price"`
		YesPrice         float64 `json:"yes_price"`
		NoPrice          float64 `json:"no_price"`
		OrderBookLatency int64   `json:"order_book_latency"`
		// 2026-08-14 起的新 lab 数据携带 TWAP 字段；旧数据缺失时回放以
		// Binance 值占位（见 replayLive）
		TwapPrice float64 `json:"twap_price"`
		TwapOpen  float64 `json:"twap_open"`
	} `json:"snapshots"`
}

// replayDataPath 指向 testdata 内的数据快照（2026-08-13 实盘采集的前 26 个事件）。
// 不用 data/btc 实时文件 —— 实盘持续追加会改变信号数，导致断言漂移。
const replayDataPath = "testdata/events_2026-08-13.jsonl"

// warmupRanges 为 2026-08-13 实盘启动时预热的 18 根 5m K 线 |close-open| 振幅
// （按时间升序，最后一根为启动时的进行中 K 线，用其近似值）。
// 数据来自 data-api.binance.vision 2026-08-13 06:45–08:10 UTC。
// ⚠️ 2026-08-14 起实盘历史振幅改用 TWAP 口径冷启动（无历史预热）；
// 此数据仅作引擎机械回放的占位基准，不代表 TWAP 振幅。
var warmupRanges = []float64{
	36.60, 3.98, 16.00, 39.85, 15.98, 13.71,
	4.68, 11.84, 2.33, 32.50, 21.83, 11.15,
	39.07, 107.31, 35.10, 73.71, 22.70, 13.3,
}

func loadReplayEvents(t *testing.T) []*eventFile {
	t.Helper()
	f, err := os.Open(replayDataPath)
	if err != nil {
		t.Skipf("回放数据不存在: %v", err)
	}
	defer f.Close()

	var events []*eventFile
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1024*1024), 4*1024*1024)
	for sc.Scan() {
		if len(sc.Bytes()) == 0 {
			continue
		}
		var e eventFile
		if err := json.Unmarshal(sc.Bytes(), &e); err != nil {
			t.Fatalf("解析事件失败: %v", err)
		}
		events = append(events, &e)
	}
	if err := sc.Err(); err != nil {
		t.Fatalf("读取数据失败: %v", err)
	}
	return events
}

// replayLive 按实盘主循环的时序回放全部事件：
// 启动预热（warmup）→ 每周期 Reset → 逐 snapshot ProcessSnapshot →
// 周期结束 AddRange。返回每个事件检测到的信号（nil 表示无信号）。
func replayLive(t *testing.T, events []*eventFile, cfg FlipConfig) []*FlipSignal {
	t.Helper()
	hist := NewHistRangeTracker(cfg.HistWindowN)
	for _, r := range warmupRanges {
		hist.AddRange(0, r) // 仅注入振幅：AddRange(open,close)=|close-open|=r
	}

	engine := NewEngine(cfg, hist)
	var signals []*FlipSignal
	for i, e := range events {
		engine.Reset(int64(i + 1))
		for _, s := range e.Snapshots {
			// TWAP 口径占位：旧回放数据无 TWAP 字段，以 Binance 值代替。
			// 本测试只验证引擎机械行为（状态机/评分/延迟否决），
			// 不验证口径标定 —— 后者待 TWAP lab 数据积累后在 Python 重做。
			twPrice, twOpen := s.TwapPrice, s.TwapOpen
			if twPrice == 0 {
				twPrice = s.Price
				twOpen = s.Open
			}
			snap := &lab.ResearchSnapshot{
				Timestamp:        s.Ts,
				RemainingSec:     s.RemainingSec,
				OpenPrice:        s.Open,
				CurrentPrice:     s.Price,
				TwapPrice:        twPrice,
				TwapOpen:         twOpen,
				YesPrice:         s.YesPrice,
				NoPrice:          s.NoPrice,
				OrderBookLatency: s.OrderBookLatency,
			}
			if sig := engine.ProcessSnapshot(snap, int64(i+1)); sig != nil {
				sig.ConditionID = e.ConditionID
				signals = append(signals, sig)
			}
		}
		hist.AddRange(e.OpenPrice, e.ClosePrice)
	}
	return signals
}

// TestReplay_OldFormulaB_LatencyVeto 用 2026-08-13 原 Formula B 参数
// （div=0.05, range<0.5, minrem=35）+ max_latency_ms=300 回放：
// 应无信号 —— 回测找到的唯一信号在穿越 tick 订单簿延迟 1212ms > 300ms，
// 被 L0 延迟否决。本测试固化 2026-08-13 的实盘诊断结论。
func TestReplay_OldFormulaB_LatencyVeto(t *testing.T) {
	events := loadReplayEvents(t)
	cfg := DefaultConfig()
	cfg.DivergenceFloor = divergenceFloorDisabled // 旧版无 floor
	cfg.MinDivergence = 0.05
	cfg.RangeExpThreshold = 0.5
	cfg.MinRemainingSec = 35
	cfg.MaxLatencyMs = 300
	signals := replayLive(t, events, cfg)
	if len(signals) != 0 {
		t.Fatalf("旧参数+延迟否决回放应无信号（1212ms 延迟否决），实际 %d 个", len(signals))
	}
}

// TestReplay_LiveNewConfig 用新默认参数（floor=0, range<1.0, minrem=15）
// + max_latency_ms=300 回放：与 config.yaml 当前实盘配置一致。
// 应恰好 1 个信号 —— 新参数放行更多穿越，其中 1 笔延迟 ≤300ms 存活。
func TestReplay_LiveNewConfig(t *testing.T) {
	events := loadReplayEvents(t)
	cfg := DefaultConfig()
	cfg.MaxLatencyMs = 300 // config.yaml 的实盘值
	signals := replayLive(t, events, cfg)
	if len(signals) != 1 {
		t.Fatalf("新参数+延迟否决回放应恰好 1 个信号，实际 %d 个", len(signals))
	}
	t.Logf("实盘存活信号: %s>0.7 score=%d entry=%.3f fill=%.3f rem=%ds",
		signals[0].Side, signals[0].Score, signals[0].EntryPrice,
		signals[0].FillPrice, signals[0].RemainingSec)
}

// TestReplay_NoLatencyVeto 关闭延迟否决（max_latency_ms=0）回放：
// 应与 Python backtest_flip_scoring.py 在同文件上的结果一致 —— 4 个信号。
func TestReplay_NoLatencyVeto(t *testing.T) {
	events := loadReplayEvents(t)
	cfg := DefaultConfig()
	signals := replayLive(t, events, cfg)
	if len(signals) != 4 {
		t.Fatalf("关闭延迟否决后应恰好 4 个信号（与 Python 回测一致），实际 %d 个", len(signals))
	}
	for _, s := range signals {
		t.Logf("回放信号: %s>0.7 score=%d entry=%.3f fill=%.3f other_d=%+.3f range_exp=%.2f div=%+.2f",
			s.Side, s.Score, s.EntryPrice, s.FillPrice, s.OtherDelta, s.RangeExpansion, s.BtcDivergence)
	}
}
