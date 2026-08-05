package flip

import (
	"encoding/json"
	"fmt"
	"log"
	"math"
	"net/http"
	"sync"
)

// HistRangeTracker maintains a rolling window of historical 5m kline ranges
// (|close - open|) and provides the average as a baseline for range_expansion
// and btc_position calculations.
//
// Mirrors Python compute_hist_avg_range() — uses a FIFO queue of the last N
// kline ranges across market cycles.
type HistRangeTracker struct {
	mu       sync.RWMutex
	windowN  int
	ranges   []float64 // FIFO, most recent last
	avgRange float64
	ready    bool // len(ranges) >= 3
}

// NewHistRangeTracker creates a tracker that keeps the last windowN ranges.
func NewHistRangeTracker(windowN int) *HistRangeTracker {
	return &HistRangeTracker{
		windowN: windowN,
		ranges:  make([]float64, 0, windowN),
	}
}

// Warmup fetches the last `windowN` 5m klines from Binance REST and seeds the
// tracker with their |close - open| values. Call once at startup.
//
// restBaseURL example: "https://data-api.binance.vision"
// symbol example: "BTCUSDT"
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
		// k[1] = open, k[4] = close (as strings)
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

// AddRange appends a completed cycle's range and updates the average.
// Called at the end of each 5-minute market cycle.
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

// AvgRange returns the current average range. Returns 0 if not ready.
func (t *HistRangeTracker) AvgRange() float64 {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.avgRange
}

// IsReady returns true when enough historical data is available (≥3 cycles).
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

// parseFloat parses a JSON string-or-number to float64.
// Handles Binance's string-encoded numeric values.
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
