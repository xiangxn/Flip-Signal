// Package lab implements the Feature Research Lab data collection pipeline.
// Per PRD2 — collects pure-fact snapshots from Binance + Polymarket and
// organizes them into 5-minute BTC events for offline feature analysis in Python.
package lab

// ResearchSnapshot captures market state at configurable tick intervals
// (default 5s, or 1s with -interval=1).
// Design principle (PRD2 §5.2): facts only, no pre-computed features.
//
// Field names kept for Python backward-compatibility; "5s" / "10s" in field names
// reflect accumulation periods at the default 5s interval — at 1s, they represent
// 1s / 2s accumulations respectively.
type ResearchSnapshot struct {
	Timestamp    int64 `json:"ts"`
	RemainingSec int   `json:"remaining_sec"`

	// --- BTC price ---
	OpenPrice    float64 `json:"open"`
	CurrentPrice float64 `json:"price"`

	// --- 10-second return (2-tick × 5s) ---
	Return10s float64 `json:"ret_10s"`

	// --- Volume since last tick (5s accumulation) ---
	BuyVolume5s  float64 `json:"buy_vol_5s"`
	SellVolume5s float64 `json:"sell_vol_5s"`

	// --- Raw order flow (5s) ---
	SignedFlow5s float64 `json:"signed_flow_5s"`

	// --- Volatility aligned to wall-clock time ---
	Volatility10s float64 `json:"vol_10s"` // 2 ticks = 10s
	Volatility30s float64 `json:"vol_30s"` // 6 ticks = 30s

	// --- Order book depth (top 5 levels, sum of sizes) ---
	BidDepth float64 `json:"bid_depth"`
	AskDepth float64 `json:"ask_depth"`

	// --- Polymarket YES/NO mid prices ---
	YesPrice float64 `json:"yes_price"`
	NoPrice  float64 `json:"no_price"`

	// --- 订单簿数据质量 ---
	OrderBookLatency int64 `json:"order_book_latency,omitempty"` // YES/NO 订单簿最大延迟（毫秒）
}

// Event represents one 5-minute BTC cycle — the fundamental research unit.
// Per PRD2 §6.
//
// ConditionID is the Polymarket conditionId (from gamma-api market endpoint).
//
// Outcome is determined purely from BTC price: ClosePrice > OpenPrice → YES (Up).
// This matches Polymarket's "Will BTC be up in 5 minutes?" market.
type Event struct {
	ConditionID string `json:"condition_id"`
	StartTime   int64  `json:"start_time"` // unix seconds

	OpenPrice  float64 `json:"open_price"`
	ClosePrice float64 `json:"close_price"`

	// Outcome: Polymarket convention — 0 = Up (YES), 1 = Down (NO)
	Outcome int `json:"outcome"`

	// Snapshots collected during this 5-minute window
	Snapshots []*ResearchSnapshot `json:"snapshots"`
}

// WindowSec is the duration of one BTC event window in seconds.
const WindowSec = 300 // 5 minutes

// DefaultTickIntervalSec is the default snapshot interval in seconds.
const DefaultTickIntervalSec = 5
