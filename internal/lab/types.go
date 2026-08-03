// Package lab implements the Feature Research Lab data collection pipeline.
// Per PRD2 — collects pure-fact snapshots from Binance + Polymarket and
// organizes them into 5-minute BTC events for offline feature analysis in Python.
package lab

// ResearchSnapshot captures market state at ~5-second intervals.
// Design principle (PRD2 §5.2): facts only, no pre-computed features.
//
// Field names kept for Python backward-compatibility; semantics are now per-tick
// (each tick ≈ 5s) rather than per-second.
type ResearchSnapshot struct {
	Timestamp    int64 `json:"ts"`
	RemainingSec int   `json:"remaining_sec"`

	// --- BTC price ---
	OpenPrice    float64 `json:"open"`
	CurrentPrice float64 `json:"price"`

	// --- 10-second return (2-tick) ---
	Return1s float64 `json:"ret_1s"`

	// --- Volume since last tick (5s accumulation) ---
	BuyVolume1s  float64 `json:"buy_vol_1s"`
	SellVolume1s float64 `json:"sell_vol_1s"`

	// --- Raw order flow ---
	SignedFlow1s float64 `json:"signed_flow_1s"`

	// --- Volatility aligned to wall-clock time ---
	Volatility10s float64 `json:"vol_10s"` // 2 ticks = 10s
	Volatility30s float64 `json:"vol_30s"` // 6 ticks = 30s

	// --- Order book depth (top 5 levels, sum of sizes) ---
	BidDepth float64 `json:"bid_depth"`
	AskDepth float64 `json:"ask_depth"`

	// --- Polymarket YES/NO mid prices ---
	YesPrice float64 `json:"yes_price"`
	NoPrice  float64 `json:"no_price"`
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

// TickIntervalSec is the snapshot interval in seconds.
const TickIntervalSec = 5
