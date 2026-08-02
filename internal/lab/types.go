// Package lab implements the Feature Research Lab data collection pipeline.
// Per PRD2 — collects pure-fact snapshots from Binance + Polymarket and
// organizes them into 5-minute BTC events for offline feature analysis in Python.
package lab

// ResearchSnapshot is a 1-second snapshot of pure market facts.
// Design principle (PRD2 §5.2): facts only, no pre-computed features.
//
// Event-level fields (condition_id, outcome, close_price) are stored on the
// parent Event, not duplicated in each snapshot.
type ResearchSnapshot struct {
	Timestamp    int64 `json:"ts"`
	RemainingSec int   `json:"remaining_sec"`

	// --- BTC price ---
	OpenPrice    float64 `json:"open"`
	CurrentPrice float64 `json:"price"`

	// --- 1-second return ---
	Return1s float64 `json:"ret_1s"`

	// --- 1-second volume (delta since last tick) ---
	BuyVolume1s  float64 `json:"buy_vol_1s"`
	SellVolume1s float64 `json:"sell_vol_1s"`

	// --- Raw order flow (BuyVol - SellVol, not normalized) ---
	SignedFlow1s float64 `json:"signed_flow_1s"`

	// --- Rolling volatility of 1s returns ---
	Volatility10s float64 `json:"vol_10s"`
	Volatility30s float64 `json:"vol_30s"`

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

	// Snapshots collected during this 5-minute window (~300 entries)
	Snapshots []*ResearchSnapshot `json:"snapshots"`
}

// WindowSec is the duration of one BTC event window in seconds.
const WindowSec = 300 // 5 minutes
