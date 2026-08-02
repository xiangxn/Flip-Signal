package lab

import (
	"math"
	"time"

	"github.com/necklace/lasttrading/internal/feed"
)

const maxPriceHistory = 31 // enough for 30s lookback of 1s returns

// Collector generates ResearchSnapshots by consuming Binance market data
// at 1-second intervals within a 5-minute event window.
//
// It maintains a rolling price history sufficient for computing
// Return1s, Volatility10s, and Volatility30s.
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
// Call before each Tick() so prices are included in the snapshot.
func (c *Collector) UpdatePolymarket(yesPrice, noPrice float64) {
	c.yesPrice = yesPrice
	c.noPrice = noPrice
}

// StartEvent begins a new 5-minute event window.
// conditionID should be the Polymarket conditionId.
func (c *Collector) StartEvent(conditionID string, startTime int64, openPrice float64) {
	c.conditionID = conditionID
	c.startTime = startTime
	c.endTime = startTime + WindowSec
	c.openPrice = openPrice
	c.snapshots = make([]*ResearchSnapshot, 0, 300)
	c.prices = c.prices[:0]
	c.yesPrice = 0
	c.noPrice = 0
}

// Tick generates a ResearchSnapshot for the current moment.
// Returns nil if Binance data is not yet available (e.g., WS not connected).
//
// Must be called at ~1 second intervals. Consumes accumulated volume
// from the BinanceAdapter and resets its 1s counters.
func (c *Collector) Tick(now time.Time) *ResearchSnapshot {
	btc := c.binance.LatestData()
	if btc.Price == 0 {
		return nil
	}

	buy1s, sell1s, _, _ := c.binance.ConsumeVolume()

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

	// 1-second return
	ret1s := 0.0
	if len(c.prices) >= 2 {
		prev := c.prices[len(c.prices)-2]
		if prev > 0 {
			ret1s = (price - prev) / prev
		}
	}

	snap := &ResearchSnapshot{
		Timestamp:     now.UnixMilli(),
		RemainingSec:  remaining,
		OpenPrice:     c.openPrice,
		CurrentPrice:  price,
		Return1s:      ret1s,
		BuyVolume1s:   buy1s,
		SellVolume1s:  sell1s,
		SignedFlow1s:  buy1s - sell1s,
		Volatility10s: c.computeVolatility(10),
		Volatility30s: c.computeVolatility(30),
		BidDepth:      btc.BidDepth5,
		AskDepth:      btc.AskDepth5,
		YesPrice:      c.yesPrice,
		NoPrice:       c.noPrice,
	}

	c.snapshots = append(c.snapshots, snap)
	return snap
}

// computeVolatility returns the standard deviation of 1s returns
// over the last lookbackSec seconds. Returns 0 if insufficient history.
func (c *Collector) computeVolatility(lookbackSec int) float64 {
	n := lookbackSec + 1
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
