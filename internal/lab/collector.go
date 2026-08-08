package lab

import (
	"math"
	"time"

	"github.com/necklace/flip-signal/internal/feed"
)

const maxPriceHistory = 15 // enough for 12-tick volatility lookback at 5s intervals

// Collector generates ResearchSnapshots at ~5-second intervals
// within a 5-minute event window.
//
// It maintains a rolling price history for computing
// Return10s (2-tick), VolatilityShort (6-tick), and VolatilityLong (12-tick).
type Collector struct {
	binance *feed.BinanceAdapter

	// Rolling price history — appends on each tick, trimmed to maxPriceHistory.
	prices []float64

	// Polymarket prices (updated externally via UpdatePolymarket before each Tick)
	yesPrice float64
	noPrice  float64

	// Current event state
	conditionID string
	startTime   int64
	endTime     int64
	openPrice   float64
	snapshots   []*ResearchSnapshot
}

// NewCollector creates a new Collector backed by the given BinanceAdapter.
func NewCollector(binance *feed.BinanceAdapter) *Collector {
	return &Collector{
		binance: binance,
		prices:  make([]float64, 0, maxPriceHistory),
	}
}

// UpdatePolymarket sets the latest YES/NO prices from Polymarket order books.
func (c *Collector) UpdatePolymarket(yesPrice, noPrice float64) {
	c.yesPrice = yesPrice
	c.noPrice = noPrice
}

// StartEvent begins a new 5-minute event window.
func (c *Collector) StartEvent(conditionID string, startTime int64, openPrice float64) {
	c.conditionID = conditionID
	c.startTime = startTime
	c.endTime = startTime + WindowSec
	c.openPrice = openPrice
	c.snapshots = make([]*ResearchSnapshot, 0, 60) // ~60 snapshots at 5s intervals
	c.prices = c.prices[:0]
	c.yesPrice = 0
	c.noPrice = 0
}

// Tick generates a ResearchSnapshot for the current moment.
// Returns nil if Binance data is not yet available.
//
// Called at ~TickIntervalSec intervals. Consumes accumulated volume
// from the BinanceAdapter since the last call.
func (c *Collector) Tick(now time.Time) *ResearchSnapshot {
	btc := c.binance.LatestData()
	if btc.Price == 0 {
		return nil
	}

	buyVol, sellVol := c.binance.ConsumeVolume()

	remaining := int(c.endTime - now.Unix())
	if remaining < 0 {
		remaining = 0
	}

	price := btc.Price

	// Maintain rolling price history
	c.prices = append(c.prices, price)
	if len(c.prices) > maxPriceHistory {
		c.prices = c.prices[1:]
	}

	// 10-second return (2 ticks × 5s)
	ret := 0.0
	if len(c.prices) >= 3 {
		prev := c.prices[len(c.prices)-3] // 2 ticks ago ≈ 10s
		if prev > 0 {
			ret = (price - prev) / prev
		}
	}

	snap := &ResearchSnapshot{
		Timestamp:     now.UnixMilli(),
		RemainingSec:  remaining,
		OpenPrice:     c.openPrice,
		CurrentPrice:  price,
		Return10s:      ret,
		BuyVolume5s:   buyVol,
		SellVolume5s:  sellVol,
		SignedFlow5s:  buyVol - sellVol,
		Volatility10s: c.computeVolatilityTicks(2), // 2 ticks = 10s
		Volatility30s: c.computeVolatilityTicks(6), // 6 ticks = 30s
		BidDepth:        btc.BidDepth5,
		AskDepth:        btc.AskDepth5,
		YesPrice:        c.yesPrice,
		NoPrice:         c.noPrice,
	}

	c.snapshots = append(c.snapshots, snap)
	return snap
}

// computeVolatilityTicks returns the std dev of tick-to-tick returns
// over the last nTicks data points. Returns 0 if insufficient history.
func (c *Collector) computeVolatilityTicks(nTicks int) float64 {
	n := nTicks + 1 // need nTicks+1 prices to get nTicks returns
	if n > len(c.prices) {
		n = len(c.prices)
	}
	if n < 2 {
		return 0
	}

	window := c.prices[len(c.prices)-n:]
	returns := make([]float64, 0, n-1)
	for i := 1; i < len(window); i++ {
		if window[i-1] > 0 {
			returns = append(returns, (window[i]-window[i-1])/window[i-1])
		}
	}

	if len(returns) == 0 {
		return 0
	}

	var sum float64
	for _, r := range returns {
		sum += r
	}
	mean := sum / float64(len(returns))

	var sumSqDiff float64
	for _, r := range returns {
		diff := r - mean
		sumSqDiff += diff * diff
	}

	return math.Sqrt(sumSqDiff / float64(len(returns)))
}

// FinalizeEvent seals the current event and returns it with the outcome.
func (c *Collector) FinalizeEvent() *Event {
	closePrice := 0.0
	if len(c.snapshots) > 0 {
		closePrice = c.snapshots[len(c.snapshots)-1].CurrentPrice
	}

	// Polymarket outcomes[] convention: [0]=Up, [1]=Down
	outcome := 1 // Down
	if closePrice > c.openPrice {
		outcome = 0 // Up
	}

	return &Event{
		ConditionID: c.conditionID,
		StartTime:  c.startTime,
		OpenPrice:  c.openPrice,
		ClosePrice: closePrice,
		Outcome:    outcome,
		Snapshots:  c.snapshots,
	}
}

// ── Dashboard getters ──

// ConditionID returns the current event's condition ID.
func (c *Collector) ConditionID() string { return c.conditionID }

// OpenPrice returns the current event's opening price.
func (c *Collector) OpenPrice() float64 { return c.openPrice }

// Snapshots returns a copy of the current event's snapshot slice.
func (c *Collector) Snapshots() []*ResearchSnapshot {
	out := make([]*ResearchSnapshot, len(c.snapshots))
	copy(out, c.snapshots)
	return out
}
