package flip

import (
	"encoding/json"
	"fmt"
	"log"
	"math"
	"net/http"
	"sync"
)

// HistRangeTracker 维护历史 5m K 线振幅（|close - open|）的滑动窗口，
// 提供均值作为 range_expansion 和 btc_position 计算的基准。
//
// 对应 Python compute_hist_avg_range() —— 跨市场周期维护最近 N 根 K 线的
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

// Warmup 从 Binance REST 获取最近 windowN 根 5m K 线，
// 用其 |close - open| 初始化追踪器。启动时调用一次。
//
// restBaseURL 示例: "https://data-api.binance.vision"
// symbol 示例: "BTCUSDT"
func (t *HistRangeTracker) Warmup(restBaseURL, symbol string) error {
	url := fmt.Sprintf("%s/api/v3/klines?symbol=%s&interval=5m&limit=%d",
		restBaseURL, symbol, t.windowN)

	resp, err := http.Get(url)
	if err != nil {
		return fmt.Errorf("warmup fetch: %w", err)
	}
	defer resp.Body.Close()

	var klines [][]any
	if err := json.NewDecoder(resp.Body).Decode(&klines); err != nil {
		return fmt.Errorf("warmup decode: %w", err)
	}

	t.mu.Lock()
	defer t.mu.Unlock()

	t.ranges = t.ranges[:0]
	for _, k := range klines {
		// k[1] = 开盘价, k[4] = 收盘价（字符串格式）
		if len(k) < 5 {
			continue
		}
		openStr, ok1 := k[1].(string)
		closeStr, ok2 := k[4].(string)
		if !ok1 || !ok2 {
			continue
		}
		open := parseFloat(openStr)
		close := parseFloat(closeStr)
		r := math.Abs(close - open)
		t.ranges = append(t.ranges, r)
	}

	t.recalcAvg()
	log.Printf("[HistRange] warmup complete: %d ranges, avg=%.4f ready=%v",
		len(t.ranges), t.avgRange, t.ready)
	return nil
}

// AddRange 追加一个已完成周期的振幅并更新均值。
// 在每个 5 分钟市场周期结束时调用。
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

// parseFloat 将 JSON 的字符串或数字解析为 float64。
// 兼容 Binance 以字符串编码的数值。
func parseFloat(v any) float64 {
	switch val := v.(type) {
	case string:
		var f float64
		fmt.Sscanf(val, "%f", &f)
		return f
	case float64:
		return val
	case json.Number:
		f, _ := val.Float64()
		return f
	default:
		return 0
	}
}
