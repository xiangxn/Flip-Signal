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
	TriggerThreshold float64 // PM price > this triggers detection (0.7)
	MinPreSnaps      int     // Minimum snapshots before crossing (5)
	MaxRemainingSec  int     // Only crossings with remaining_sec < this are valid (260, window too early = insufficient BTC path)

	// ── Oscillation detection (all three must hold) — Formula A ──
	PathEffOscillating    float64 // path_eff ≤ this → candidate (0.8, was 0.5)
	NoiseRatioOscillating float64 // noise_ratio > this → candidate (1.5, was 5.0)
	FlipsOscillating      int     // flips > this → candidate (1, was 2)

	// ── Range expansion (tick-independent, uses hist_avg_range) ──
	HistWindowN       int     // Historical kline window size (18)
	RangeExpThreshold float64 // < this → BTC barely moved, PM overconfident (0.5)
	RangeExpMax       float64 // ≥ this → F0 veto, real breakout (2.0)

	// ── Opposite-side confirmation (§2.7) — Formula A: 3-tier ──
	ConfirmDelayTicks  int     // Ticks to wait for confirmation (1 tick = 5s)
	ODHardFilter       float64 // other_delta hard lower bound, -999 = disabled (Formula A: scoring handles it)
	OtherDeltaVStrong  float64 // > this → +3 points (0.05, new top tier)
	OtherDeltaStrong   float64 // > this → +2 points (0.02, was 0.03)
	OtherDeltaWeak     float64 // > this → +1 points (0.01, was +2)

	// ── BTC position (§2.8) — Formula A: widened ──
	BTCPosMax float64 // NO>0.7: btc_pos > this → BTC diverges from PM → +1 (0.1)
	BTCPosMin float64 // YES>0.7: btc_pos < this → BTC diverges from PM → +1 (-0.1)

	// ── Entry price (§2.9) — Formula A: re-activated ──
	EntryCheapStrong float64 // < this → +1 point  (0.20)
	EntryCheapWeak   float64 // < this → +1 point  (0.25, elif — no stacking)

	// ── Scoring weights (§2.10) — Formula A ──
	WOtherD5VStrong int // Opposite huge move  (3, new)
	WOtherD5Strong  int // Opposite big move    (2, was 4)
	WOtherD5Weak    int // Opposite small move  (1, was 2)
	WOscillating    int // Oscillation bonus    (2, was 1)
	WCheapEntryStr  int // Very cheap entry     (1, was 2, was disabled)
	WCheapEntryWeak int // Cheap entry          (1)
	WRangeExpansion int // Range too small      (2)
	WBtcExtreme     int // BTC extreme pos      (1)

	// ── Entry thresholds (§2.10) ──
	ScoreEntry int // ≥ this → open 1 share (5)
	ScoreAdd   int // ≥ this → add 2 shares (99 = disabled)
}

// DefaultConfig returns a FlipConfig matching backtest_flip_config.py Formula A.
// Backtest result: 34 signals, 52.9% WR, +10.09 P&L, PF=2.7 on lab data.
func DefaultConfig() FlipConfig {
	return FlipConfig{
		TriggerThreshold: 0.7,
		MinPreSnaps:      5,
		MaxRemainingSec:  260, // §2.1: crossings before this are invalid (too early, BTC path too short)
		PathEffOscillating:    0.8,  // Formula A: relaxed from 0.5
		NoiseRatioOscillating: 1.5,  // Formula A: relaxed from 5.0 (never fired)
		FlipsOscillating:      1,    // Formula A: relaxed from 2
		HistWindowN:           18,
		RangeExpThreshold:     0.5,
		RangeExpMax:           2.0,
		ConfirmDelayTicks:     1,     // 1 tick = 5s
		ODHardFilter:          -999.0, // Formula A: disabled, scoring handles it
		OtherDeltaVStrong:     0.05,   // Formula A: new top tier (>0.05 → +3)
		OtherDeltaStrong:      0.02,   // Formula A: was 0.03 +4
		OtherDeltaWeak:        0.01,   // Formula A: was +2
		BTCPosMax:             0.1,    // Formula A: widened from 0.5
		BTCPosMin:             -0.1,   // Formula A: widened from -0.5
		EntryCheapStrong:      0.20,   // Formula A: re-activated (was 0=disabled)
		EntryCheapWeak:        0.25,
		WOtherD5VStrong:       3,      // Formula A: new weight
		WOtherD5Strong:        2,      // Formula A: was 4
		WOtherD5Weak:          1,      // Formula A: was 2
		WOscillating:          2,      // Formula A: was 1
		WCheapEntryStr:        1,      // Formula A: was 2 (was disabled)
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
