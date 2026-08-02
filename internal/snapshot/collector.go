package snapshot

import (
	"math"
	"time"
)

// Collector generates 1-second snapshots by merging:
//   - Binance BTC/USDT market data (price, volume, depth, order flow)
//   - Polymarket YES/NO order book data (YesPrice, NoPrice)
type Collector struct {
	buffer    *RingBuffer
	openPrice float64
	marketID  string

	// Latest BTC market data (from Binance WS)
	latestPrice     float64
	latestBidDepth5  float64
	latestAskDepth5  float64
	latestBidDepth10 float64
	latestAskDepth10 float64

	// Latest volume (consumed from Binance adapter each tick)
	buyVol1s  float64
	sellVol1s float64
	buyVol10s  float64
	sellVol10s float64

	// Polymarket data
	latestYesPrice float64
	latestNoPrice  float64

	// Previous volume for delta computation
	prevBuyVol10s  float64
	prevSellVol10s float64

	// Market end time
	marketEndTime int64
}

// NewCollector creates a new snapshot collector.
func NewCollector(marketID string, openPrice float64, capacity int) *Collector {
	return &Collector{
		buffer:    NewRingBuffer(capacity),
		marketID:  marketID,
		openPrice: openPrice,
	}
}

// SetMarketEndTime sets the market end time for remaining seconds calculation.
func (c *Collector) SetMarketEndTime(endTime int64) {
	c.marketEndTime = endTime
}

// SetOpenPrice updates the BTC open price.
func (c *Collector) SetOpenPrice(price float64) {
	if c.openPrice == 0 && price > 0 {
		c.openPrice = price
	}
}

// UpdateBTC updates BTC market data from Binance.
func (c *Collector) UpdateBTC(price, bid5, ask5, bid10, ask10 float64) {
	c.latestPrice = price
	c.latestBidDepth5 = bid5
	c.latestAskDepth5 = ask5
	c.latestBidDepth10 = bid10
	c.latestAskDepth10 = ask10
}

// UpdateVolume sets the volume deltas from Binance trade stream.
// These are consumed/accumulated values since last Tick().
func (c *Collector) UpdateVolume(buy1s, sell1s, buy10s, sell10s float64) {
	c.buyVol1s = buy1s
	c.sellVol1s = sell1s
	c.buyVol10s = buy10s
	c.sellVol10s = sell10s
}

// UpdatePolymarket updates the Polymarket YES/NO prices.
func (c *Collector) UpdatePolymarket(yesPrice, noPrice float64) {
	c.latestYesPrice = yesPrice
	c.latestNoPrice = noPrice
}

// Tick generates a new snapshot. Should be called every 1 second.
func (c *Collector) Tick(now time.Time) *Snapshot {
	s := &Snapshot{
		Timestamp:    now.UnixMilli(),
		MarketID:     c.marketID,
		RemainingSec: c.calcRemainingSec(now),
		OpenPrice:    c.openPrice,
		Price:        c.latestPrice,

		// Volume (consumed from Binance adapter)
		BuyVolume1s:  c.buyVol1s,
		SellVolume1s: c.sellVol1s,
		BuyVolume10s:  c.buyVol10s,
		SellVolume10s: c.sellVol10s,

		// Depth (from Binance order book)
		BidDepth5:  c.latestBidDepth5,
		AskDepth5:  c.latestAskDepth5,
		BidDepth10: c.latestBidDepth10,
		AskDepth10: c.latestAskDepth10,

		// Polymarket
		YesPrice: c.latestYesPrice,
		NoPrice:  c.latestNoPrice,
	}

	// Compute return from open
	if c.openPrice > 0 {
		s.ReturnFromOpen = (c.latestPrice - c.openPrice) / c.openPrice
	}

	// Signed flow 10s
	s.SignedFlow10s = s.BuyVolume10s - s.SellVolume10s

	// Push to buffer BEFORE computing derived fields that depend on history
	c.buffer.Push(s)

	// Compute returns from buffer history
	s.Return1s = c.computeReturn(1)
	s.Return5s = c.computeReturn(5)
	s.Return10s = c.computeReturn(10)
	s.Return30s = c.computeReturn(30)

	// Compute OHLC from open
	s.HighFromOpen, s.LowFromOpen = c.computeOHLC()

	// Compute volatility from price returns
	s.Volatility10s = c.computeVolatility(10)
	s.Volatility30s = c.computeVolatility(30)

	// Save 10s volume snapshots
	c.prevBuyVol10s = c.buyVol10s
	c.prevSellVol10s = c.sellVol10s

	// Re-push the fully computed snapshot
	c.buffer.Push(s)

	return s
}

func (c *Collector) calcRemainingSec(now time.Time) int {
	if c.marketEndTime == 0 {
		return 300
	}
	remaining := int(c.marketEndTime - now.Unix())
	if remaining < 0 {
		remaining = 0
	}
	return remaining
}

func (c *Collector) computeReturn(lookbackSec int) float64 {
	prev := c.buffer.SnapshotAt(lookbackSec)
	if prev == nil || prev.Price == 0 {
		return 0
	}
	return (c.latestPrice - prev.Price) / prev.Price
}

func (c *Collector) computeOHLC() (high, low float64) {
	high = c.latestPrice
	low = c.latestPrice

	window := c.buffer.Window(c.buffer.Size())
	for _, s := range window {
		if s.Price > high {
			high = s.Price
		}
		if s.Price < low {
			low = s.Price
		}
	}

	if c.openPrice > 0 {
		return (high - c.openPrice) / c.openPrice, (low - c.openPrice) / c.openPrice
	}
	return 0, 0
}

func (c *Collector) computeVolatility(lookbackSec int) float64 {
	window := c.buffer.Window(lookbackSec)
	if len(window) < 2 {
		return 0
	}

	returns := make([]float64, 0, len(window)-1)
	for i := 1; i < len(window); i++ {
		if window[i-1].Price > 0 {
			r := (window[i].Price - window[i-1].Price) / window[i-1].Price
			returns = append(returns, r)
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

// Buffer returns the internal ring buffer.
func (c *Collector) Buffer() *RingBuffer {
	return c.buffer
}
