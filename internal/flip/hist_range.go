package flip

import (
	"math"
	"sync"
)

// HistRangeTracker 维护历史 5m 窗口振幅（|close - open|）的滑动窗口，
// 提供均值作为 range_expansion 和 btc_position 计算的基准。
//
// ⚠️ 2026-08-14 起振幅为 Chainlink TWAP-60 口径（btc-updown-5m 结算基准）：
// 由主循环在每个窗口结束时 AddRange(官方 TWAP open, 官方 TWAP close) 积累。
// 启动时经 feed.FetchTwapRanges（crypto-price 接口）拉取官方历史振幅预热；
// 接口不可用时冷启动积累 ≥3 个窗口后 IsReady 才为 true，期间 B1/B2
// 不产生信号。不可用 Binance K 线振幅预热 —— 两套口径振幅尺度不同，
// 混用会污染基准。
//
// 对应 Python compute_hist_avg_range() —— 跨市场周期维护最近 N 个振幅的
// FIFO 队列。
type HistRangeTracker struct {
	mu       sync.RWMutex
	windowN  int
	ranges   []float64 // FIFO, most recent last
	avgRange float64
	ready    bool // len(ranges) >= 3
}

// NewHistRangeTracker 创建一个保存最近 windowN 个振幅的追踪器。
func NewHistRangeTracker(windowN int) *HistRangeTracker {
	return &HistRangeTracker{
		windowN: windowN,
		ranges:  make([]float64, 0, windowN),
	}
}

// AddRange 追加一个已完成周期的振幅并更新均值。
// 在每个 5 分钟市场周期结束时调用（TWAP 口径）。
func (t *HistRangeTracker) AddRange(openPrice, closePrice float64) {
	t.mu.Lock()
	defer t.mu.Unlock()

	r := math.Abs(closePrice - openPrice)
	t.ranges = append(t.ranges, r)
	if len(t.ranges) > t.windowN {
		t.ranges = t.ranges[1:] // FIFO: drop oldest
	}
	t.recalcAvg()
}

// AvgRange 返回当前平均振幅，未就绪时返回 0。
func (t *HistRangeTracker) AvgRange() float64 {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.avgRange
}

// IsReady 在有足够历史数据（≥3 个周期）时返回 true。
func (t *HistRangeTracker) IsReady() bool {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.ready
}

func (t *HistRangeTracker) recalcAvg() {
	if len(t.ranges) == 0 {
		t.avgRange = 0
		t.ready = false
		return
	}
	var sum float64
	for _, r := range t.ranges {
		sum += r
	}
	t.avgRange = sum / float64(len(t.ranges))
	t.ready = len(t.ranges) >= 3
}
