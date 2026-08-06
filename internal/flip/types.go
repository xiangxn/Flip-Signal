// Package flip implements the Flip Signal Detection engine for paper trading.
//
// Based on docs/flip_backtest_plan.md and python/backtest_flip_scoring.py.
// Detects when Polymarket YES/NO prices cross 0.7 and evaluates whether
// the crossing is likely to reverse (flip) using a 7-feature composite score.
//
// Architecture mirrors internal/mqs: pure computation layer with zero external
// dependencies, plus an engine with a state machine for real-time operation.
package flip

import "time"

// FlipConfig holds all tunable parameters for flip signal detection.
// Defaults match backtest_flip_config.py exactly (5s data calibration).
type FlipConfig struct {
	// ── Layer 0: Pre-conditions (§2.1) ──
	TriggerThreshold  float64 // PM price > this triggers detection (0.7)
	FirstCrossingOnly bool    // Only trigger on first crossing per cycle
	MinPreSnaps       int     // Minimum snapshots before crossing (5)

	// ── Oscillation detection (all three must hold) ──
	PathEffOscillating    float64 // path_eff ≤ this → candidate (0.5)
	NoiseRatioOscillating float64 // noise_ratio > this → candidate (5.0)
	FlipsOscillating      int     // flips > this → candidate (2)

	// ── Range expansion (tick-independent, uses hist_avg_range) ──
	HistWindowN       int     // Historical kline window size (18)
	RangeExpThreshold float64 // < this → BTC barely moved, PM overconfident (0.5)
	RangeExpMax       float64 // ≥ this → F0 veto, real breakout (2.0)

	// ── Opposite-side confirmation (§2.7) ──
	ConfirmDelayTicks int     // Ticks to wait for confirmation (1 tick = 5s)
	OtherDeltaStrong  float64 // > this → +4 points (0.03)
	OtherDeltaWeak    float64 // > this → +2 points (0.01)

	// ── BTC position (§2.8) ──
	BTCPosMax float64 // YES>0.7: 0 < btc_pos < this → +1 (0.5)
	BTCPosMin float64 // NO>0.7:  this < btc_pos < 0 → +1 (-0.5)

	// ── Entry price (§2.9) ──
	EntryCheapStrong float64 // < this → +2 points (0.20)
	EntryCheapWeak   float64 // < this → +1 point  (0.25)

	// ── Scoring weights (§2.10) ──
	WOtherD5Strong  int // Opposite big move  (4)
	WOtherD5Weak    int // Opposite small move (2)
	WOscillating    int // Oscillation bonus (1)
	WCheapEntryStr  int // Very cheap entry  (2)
	WCheapEntryWeak int // Cheap entry       (1)
	WRangeExpansion int // Range too small   (2)
	WBtcExtreme     int // BTC extreme pos   (1)

	// ── Entry thresholds (§2.10) ──
	ScoreEntry int // ≥ this → open 1 share (5)
	ScoreAdd   int // ≥ this → add 2 shares (99 = disabled)
}

// DefaultConfig returns a FlipConfig matching backtest_flip_config.py.
func DefaultConfig() FlipConfig {
	return FlipConfig{
		TriggerThreshold:      0.7,
		FirstCrossingOnly:     true,
		MinPreSnaps:           5,
		PathEffOscillating:    0.5,
		NoiseRatioOscillating: 5.0,
		FlipsOscillating:      2,
		HistWindowN:           18,
		RangeExpThreshold:     0.5,
		RangeExpMax:           2.0,
		ConfirmDelayTicks:     1,  // 1 tick = 5s
		OtherDeltaStrong:      0.03,
		OtherDeltaWeak:        0.01,
		BTCPosMax:             0.5,
		BTCPosMin:             -0.5,
			EntryCheapStrong:      0,    // OPT#5: disabled, entry<0.20 bonus removed
		EntryCheapWeak:        0.25,
		WOtherD5Strong:        4,
		WOtherD5Weak:          2,
		WOscillating:          1,
			WCheapEntryStr:        2,
		WCheapEntryWeak:       1,
		WRangeExpansion:       2,
		WBtcExtreme:           1,
		ScoreEntry:            5,
		ScoreAdd:              99, // disabled (5s data scoring not fine-grained enough)
	}
}

// ── State machine ──

type flipState int

const (
	stateIdle       flipState = iota // waiting for Reset()
	stateWatching                    // accumulating snapshots, watching for >0.7
	stateConfirming                  // crossed, waiting for T+5s confirmation
	stateDone                        // cycle complete or vetoed
)

// ── Signal output ──

// FlipSignal represents a detected flip trading signal.
// Fields match Python FlipSignal dataclass for JSONL compatibility.
type FlipSignal struct {
	Time         time.Time `json:"time"`
	ConditionID  string    `json:"condition_id"`
	Side         string    `json:"side"` // "yes" or "no"
	Score        int       `json:"score"`
	EntryPrice   float64   `json:"entry_price"`
	Shares       int       `json:"shares"`
	RemainingSec int       `json:"remaining_sec"`

	// Feature details (for analysis/debug)
	PathEff        float64 `json:"path_eff"`
	NoiseRatio     float64 `json:"noise_ratio"`
	Flips          int     `json:"flips"`
	IsOscillating  bool    `json:"is_oscillating"`
	RangeExpansion float64 `json:"range_expansion"`
	BTCPosition    float64 `json:"btc_position"`
	BTCExtreme     bool    `json:"btc_extreme"`
	OtherDelta     float64 `json:"other_delta"`

	// Filled on resolution
	Won bool    `json:"won"`
	PnL float64 `json:"pnl"`
}

// ── Scoring input ──

// ScoreParams bundles the inputs for ComputeFlipScore.
type ScoreParams struct {
	Side           string
	OtherDelta     float64
	IsOscillating  bool
	EntryPrice     float64
	RangeExpansion float64
	BTCPosition    float64
	HistReady      bool
	Cfg            FlipConfig
}
