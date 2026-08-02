package snapshot

// Snapshot represents a 1-second snapshot of BTC price and Polymarket order book data.
// Corresponds to PRD §1.2.
type Snapshot struct {
	Timestamp    int64
	MarketID     string
	RemainingSec int

	// BTC price
	OpenPrice float64
	Price     float64

	// Price path returns
	Return1s       float64
	Return5s       float64
	Return10s      float64
	Return30s      float64
	ReturnFromOpen float64

	// OHLC over the window from open
	HighFromOpen float64
	LowFromOpen  float64

	// Volume
	BuyVolume1s  float64
	SellVolume1s float64
	BuyVolume10s  float64
	SellVolume10s float64

	// Order flow
	SignedFlow10s float64

	// Volatility
	Volatility10s float64
	Volatility30s float64

	// Depth (top N levels)
	BidDepth5  float64
	AskDepth5  float64
	BidDepth10 float64
	AskDepth10 float64

	// Polymarket book
	YesPrice float64
	NoPrice  float64
}
