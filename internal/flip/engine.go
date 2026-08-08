package flip

import (
	"math"
	"time"

	"github.com/necklace/flip-signal/internal/lab"
)

// T0Features holds the features computed at the crossing moment (T=0).
// Exported for use by the dashboard to display crossing-level detail.
type T0Features struct {
	PathEff        float64 `json:"path_eff"`
	NoiseRatio     float64 `json:"noise_ratio"`
	Flips          int     `json:"flips"`
	Oscillating    bool    `json:"is_oscillating"`
	RangeExpansion float64 `json:"range_expansion"`
	BTCPosition    float64 `json:"btc_position"`
	BTCExtreme     bool    `json:"btc_extreme"`
}

// Engine detects flip signals from a stream of ResearchSnapshots.
//
// Multi-crossing mode (AllowRetryCrossings=true, default):
// Every rising edge (>0.7) triggers observation; the first crossing that
// passes scoring wins. One bet per event. YES-first priority.
//
// Legacy mode (AllowRetryCrossings=false): first crossing only per side.
//
// Usage per market cycle:
//
//	engine.Reset(gen)
//	for each snapshot:
//	    sig := engine.ProcessSnapshot(snap, gen)
//	    if sig != nil { ... }
type Engine struct {
	cfg       FlipConfig
	histRange *HistRangeTracker
	state     flipState

	snapBuffer []*lab.ResearchSnapshot // all snapshots in this cycle (always appended)
	crossSnap  *lab.ResearchSnapshot   // snapshot at current crossing moment
	crossSide  string                  // "yes" or "no"
	crossIdx   int                     // index in snapBuffer of current crossing

	confirmCount int  // ticks waited in CONFIRMING state
	generation   int64
	doneThisGen  bool

	// Multi-crossing: rising-edge detection (≤threshold → >threshold)
	// Replaces the old first_crossing_only fields (yesFirstCrossIdx, noFirstCrossIdx, yesTried, noTried).
	yesWasAbove bool // YES was > threshold in previous snapshot
	noWasAbove  bool // NO was > threshold in previous snapshot

	// Legacy first_crossing_only tracking (only used when AllowRetryCrossings=false)
	yesFirstCrossIdx int  // index in snapBuffer of first YES >0.7, -1 if none
	noFirstCrossIdx  int  // index in snapBuffer of first NO >0.7, -1 if none
	yesTried         bool // YES side was attempted & failed this generation
	noTried          bool // NO side was attempted & failed this generation

	// T=0 features (computed in onCrossing, read in onConfirmed)
	pathEff        float64
	noiseRatio     float64
	flips          int
	oscillating    bool
	rangeExpansion float64
	btcPosition    float64
	btcExtreme     bool
}

// NewEngine creates a new flip detection engine.
func NewEngine(cfg FlipConfig, histRange *HistRangeTracker) *Engine {
	return &Engine{
		cfg:               cfg,
		histRange:         histRange,
		state:             stateIdle,
		yesFirstCrossIdx:  -1,
		noFirstCrossIdx:   -1,
	}
}

// Reset prepares the engine for a new market cycle.
func (e *Engine) Reset(generation int64) {
	e.state = stateWatching
	e.snapBuffer = e.snapBuffer[:0]
	e.crossSnap = nil
	e.crossSide = ""
	e.crossIdx = 0
	e.confirmCount = 0
	e.generation = generation
	e.doneThisGen = false

	e.yesWasAbove = false
	e.noWasAbove = false

	e.yesFirstCrossIdx = -1
	e.noFirstCrossIdx = -1
	e.yesTried = false
	e.noTried = false

	e.pathEff = 0
	e.noiseRatio = 0
	e.flips = 0
	e.oscillating = false
	e.rangeExpansion = 0
	e.btcPosition = 0
	e.btcExtreme = false
}

// ProcessSnapshot processes one ResearchSnapshot. Returns a FlipSignal if
// all conditions are met, nil otherwise.
//
// Multi-crossing mode (AllowRetryCrossings=true, default):
//   - Detects rising edges (≤threshold → >threshold) for both YES and NO.
//   - Each rising edge triggers observation; the first crossing that passes
//     all pre-checks AND scoring wins. Once a bet is placed, the cycle ends.
//   - If confirmation fails, checks whether the other side crossed during
//     the wait and tries it; otherwise returns to watching.
//
// Legacy mode (AllowRetryCrossings=false): first crossing only per side.
func (e *Engine) ProcessSnapshot(snap *lab.ResearchSnapshot, gen int64) *FlipSignal {
	if gen != e.generation || e.doneThisGen {
		return nil
	}

	// Always append to buffer so it reflects full cycle history.
	bufIdx := len(e.snapBuffer)
	e.snapBuffer = append(e.snapBuffer, snap)

	// Rising-edge detection for both sides (§2.1: remaining_sec < MaxRemainingSec)
	yesIsAbove := snap.YesPrice > e.cfg.TriggerThreshold && snap.RemainingSec < e.cfg.MaxRemainingSec
	noIsAbove := snap.NoPrice > e.cfg.TriggerThreshold && snap.RemainingSec < e.cfg.MaxRemainingSec

	yesRisingEdge := yesIsAbove && !e.yesWasAbove
	noRisingEdge := noIsAbove && !e.noWasAbove

	e.yesWasAbove = yesIsAbove
	e.noWasAbove = noIsAbove

	switch e.state {
	case stateIdle:
		return nil

	case stateWatching:
		if e.cfg.AllowRetryCrossings {
			// Multi-crossing: try every rising edge, YES-first priority
			if yesRisingEdge && bufIdx >= e.cfg.MinPreSnaps {
				return e.enterConfirming(bufIdx, "yes")
			}
			if noRisingEdge && bufIdx >= e.cfg.MinPreSnaps {
				return e.enterConfirming(bufIdx, "no")
			}
		} else {
			// Legacy: track first crossing per side, try YES then NO once each
			if e.yesFirstCrossIdx < 0 && yesIsAbove {
				e.yesFirstCrossIdx = bufIdx
			}
			if e.noFirstCrossIdx < 0 && noIsAbove {
				e.noFirstCrossIdx = bufIdx
			}
			if !e.yesTried && e.yesFirstCrossIdx >= 0 {
				return e.enterConfirming(e.yesFirstCrossIdx, "yes")
			}
			if !e.noTried && e.noFirstCrossIdx >= 0 {
				return e.enterConfirming(e.noFirstCrossIdx, "no")
			}
		}
		return nil

	case stateConfirming:
		e.confirmCount++
		if e.confirmCount >= e.cfg.ConfirmDelayTicks {
			sig := e.onConfirmed(snap)
			if sig != nil {
				e.state = stateDone
				e.doneThisGen = true
				return sig
			}
			// Confirmation failed — try the other side or return to watching
			return e.afterFailedConfirm(snap)
		}
		return nil

	case stateDone:
		return nil
	}
	return nil
}

// enterConfirming transitions to CONFIRMING state using the crossing at the
// given buffer index. Computes all T=0 features from snapshots up to crossIdx.
//
// In multi-crossing mode (AllowRetryCrossings=true): veto failures on this
// crossing don't exhaust the side — future rising edges will retry. Only
// a successfully scored signal (score ≥ ScoreEntry) ends the cycle.
func (e *Engine) enterConfirming(crossIdx int, side string) *FlipSignal {
	crossSnap := e.snapBuffer[crossIdx]

	// Check minimum pre-snapshots (including crossing snap).
	nPre := crossIdx + 1 // snapshots up to and including crossing
	if nPre < e.cfg.MinPreSnaps {
		return e.handleEnterFail(side)
	}

	// Extract pre-prices from snap buffer up to crossing
	prePrices := make([]float64, nPre)
	for i := 0; i < nPre; i++ {
		prePrices[i] = e.snapBuffer[i].CurrentPrice
	}

	openPrice := crossSnap.OpenPrice

	// Compute T=0 features
	netMove := math.Abs(prePrices[len(prePrices)-1] - openPrice)
	preHigh, preLow := prePrices[0], prePrices[0]
	for _, p := range prePrices {
		if p > preHigh {
			preHigh = p
		}
		if p < preLow {
			preLow = p
		}
	}
	preRange := preHigh - preLow
	if preRange == 0 {
		return e.handleEnterFail(side)
	}

	e.pathEff = netMove / preRange

	// path_eff too low → trend unclear, veto
	if e.pathEff < e.cfg.PathEffVetoMin {
		return e.handleEnterFail(side)
	}

	totalPathVal := TotalPath(prePrices)
	if netMove > 0 {
		e.noiseRatio = totalPathVal / netMove
	} else {
		e.noiseRatio = totalPathVal // pure oscillation
	}

	// high noise ratio → PM price unstable, veto
	if e.noiseRatio > e.cfg.NoiseRatioVetoMax {
		return e.handleEnterFail(side)
	}

	e.flips = CountFlips(prePrices)
	e.oscillating = IsOscillating(e.pathEff, e.noiseRatio, e.flips, e.cfg)

	// Range expansion (only when hist is ready)
	if e.histRange.IsReady() {
		e.rangeExpansion = RangeExpansion(crossSnap.CurrentPrice, openPrice, e.histRange.AvgRange())
	}

	// F0: Range expansion too large — real breakout, PM is right, veto
	if e.histRange.IsReady() && e.rangeExpansion >= e.cfg.RangeExpMax {
		return e.handleEnterFail(side)
	}

	// All pre-checks passed — save crossing state
	e.crossSnap = crossSnap
	e.crossSide = side
	e.crossIdx = crossIdx
	e.confirmCount = 0

	// If the confirmation tick is already in the buffer (happens when
	// falling back to a crossing that occurred before the failed side's
	// confirmation), evaluate synchronously — matching Python's
	// check_signal which has all data available at once.
	if confIdx := crossIdx + e.cfg.ConfirmDelayTicks; confIdx < len(e.snapBuffer) {
		sig := e.onConfirmed(e.snapBuffer[confIdx])
		if sig != nil {
			e.state = stateDone
			e.doneThisGen = true
			return sig
		}
		// This side failed — try the other side or return to watching
		return e.afterFailedConfirm(e.snapBuffer[confIdx])
	}

	// Normal path: wait for the confirmation tick to arrive
	e.state = stateConfirming
	return nil
}

// handleEnterFail handles a veto during enterConfirming. In multi-crossing mode
// this crossing is abandoned but the side is not exhausted; in legacy mode the
// side is marked as tried.
func (e *Engine) handleEnterFail(side string) *FlipSignal {
	if !e.cfg.AllowRetryCrossings {
		e.markSideTried(side)
		return e.fallbackAfterFailedConfirm()
	}
	// Multi-crossing: this crossing failed, return to watching for the next rising edge
	e.returnToWatching()
	return nil
}

// afterFailedConfirm handles the situation when a side's confirmation fails to
// produce a signal. In multi-crossing mode it checks whether the other side
// crossed during the confirmation window and tries it; otherwise returns to
// watching. In legacy mode it delegates to fallbackAfterFailedConfirm.
func (e *Engine) afterFailedConfirm(snap *lab.ResearchSnapshot) *FlipSignal {
	if !e.cfg.AllowRetryCrossings {
		return e.fallbackAfterFailedConfirm()
	}

	// Multi-crossing: check if the other side is currently above threshold
	// (crossed during our confirmation window)
	otherSide := "no"
	if e.crossSide == "no" {
		otherSide = "yes"
	}

	otherIsAbove := false
	if otherSide == "yes" {
		otherIsAbove = snap.YesPrice > e.cfg.TriggerThreshold && snap.RemainingSec < e.cfg.MaxRemainingSec
	} else {
		otherIsAbove = snap.NoPrice > e.cfg.TriggerThreshold && snap.RemainingSec < e.cfg.MaxRemainingSec
	}

	if otherIsAbove {
		// Find the most recent rising edge for the other side
		otherCrossIdx := e.findRecentCrossing(otherSide)
		if otherCrossIdx >= 0 && otherCrossIdx >= e.cfg.MinPreSnaps {
			return e.enterConfirming(otherCrossIdx, otherSide)
		}
	}

	// Nothing pending → return to watching
	e.returnToWatching()
	return nil
}

// findRecentCrossing scans snapBuffer backwards to find the most recent
// rising edge (≤threshold → >threshold) for the given side.
// Returns -1 if not found.
func (e *Engine) findRecentCrossing(side string) int {
	wasAbove := false
	for i := len(e.snapBuffer) - 1; i >= 0; i-- {
		s := e.snapBuffer[i]
		var price float64
		if side == "yes" {
			price = s.YesPrice
		} else {
			price = s.NoPrice
		}
		isAbove := price > e.cfg.TriggerThreshold && s.RemainingSec < e.cfg.MaxRemainingSec
		if wasAbove && !isAbove {
			return i + 1 // rising edge at next snapshot
		}
		wasAbove = isAbove
	}
	if wasAbove {
		return 0 // very first snapshot was already above threshold
	}
	return -1
}

// returnToWatching resets the crossing state and transitions back to Watching.
func (e *Engine) returnToWatching() {
	e.state = stateWatching
	e.crossSnap = nil
	e.crossSide = ""
	e.crossIdx = 0
	e.confirmCount = 0
}

// fallbackAfterFailedConfirm implements the legacy first_crossing_only fallback:
// after one side fails, try the other side's first crossing from the buffer.
// Only used when AllowRetryCrossings=false.
func (e *Engine) fallbackAfterFailedConfirm() *FlipSignal {
	// Check if the other side has also crossed (earlier or at current tick)
	if !e.yesTried && e.yesFirstCrossIdx >= 0 {
		return e.enterConfirming(e.yesFirstCrossIdx, "yes")
	}
	if !e.noTried && e.noFirstCrossIdx >= 0 {
		return e.enterConfirming(e.noFirstCrossIdx, "no")
	}

	// Both sides tried or neither crossed → resume watching for future crossings
	e.state = stateWatching
	e.crossSnap = nil
	e.crossSide = ""
	e.crossIdx = 0
	e.confirmCount = 0
	return nil
}

// onConfirmed is called after the confirmation delay (ConfirmDelayTicks).
// It computes the T+5s confirmation signal and the final score.
func (e *Engine) onConfirmed(snap *lab.ResearchSnapshot) *FlipSignal {
	// Compute other_delta — opposite-side price change
	var otherDelta float64
	if e.crossSide == "yes" {
		// YES>0.7, opposite is NO
		otherDelta = snap.NoPrice - e.crossSnap.NoPrice
	} else {
		// NO>0.7, opposite is YES
		otherDelta = snap.YesPrice - e.crossSnap.YesPrice
	}

	// Formula A: other_delta hard filter (default -999 = disabled, scoring handles it)
	if otherDelta < e.cfg.ODHardFilter {
		return nil
	}

	// Entry price = opposite side price at crossing time
	var entryPrice float64
	if e.crossSide == "yes" {
		entryPrice = e.crossSnap.NoPrice // buy NO, bet DOWN
	} else {
		entryPrice = e.crossSnap.YesPrice // buy YES, bet UP
	}

	// BTC position (only when hist is ready)
	e.btcPosition = 0.0
	e.btcExtreme = false
	if e.histRange.IsReady() {
		e.btcPosition = BTCPosition(e.crossSnap.CurrentPrice, e.crossSnap.OpenPrice, e.histRange.AvgRange())
		// Formula A: BTC divergence from PM = flip edge. PM and BTC agree = real trend.
		if e.crossSide == "yes" {
			// YES>0.7 (PM bullish), BTC down → PM overreacting
			e.btcExtreme = e.btcPosition < e.cfg.BTCPosMin // btc_pos < -0.1
		} else {
			// NO>0.7 (PM bearish), BTC up → PM overreacting
			e.btcExtreme = e.btcPosition > e.cfg.BTCPosMax // btc_pos > 0.1
		}
	}

	// Compute composite score
	params := ScoreParams{
		Side:           e.crossSide,
		OtherDelta:     otherDelta,
		IsOscillating:  e.oscillating,
		EntryPrice:     entryPrice,
		RangeExpansion: e.rangeExpansion,
		BTCPosition:    e.btcPosition,
		BTCExtreme:     e.btcExtreme,
		HistReady:      e.histRange.IsReady(),
		Cfg:            e.cfg,
	}

	score, vetoed := ComputeFlipScore(params)
	if vetoed || score < e.cfg.ScoreEntry {
		return nil
	}

	shares := 1
	if score >= e.cfg.ScoreAdd {
		shares = 2
	}

	return &FlipSignal{
		Time:           time.Now().UTC(),
		Side:           e.crossSide,
		Score:          score,
		EntryPrice:     entryPrice,
		Shares:         shares,
		RemainingSec:   e.crossSnap.RemainingSec,
		PathEff:        e.pathEff,
		NoiseRatio:     e.noiseRatio,
		Flips:          e.flips,
		IsOscillating:  e.oscillating,
		RangeExpansion: e.rangeExpansion,
		BTCPosition:    e.btcPosition,
		BTCExtreme:     e.btcExtreme,
		OtherDelta:     otherDelta,
	}
}

// ── Dashboard getters ──
// Engine fields are written only from the main market-loop goroutine.
// Dashboard HTTP handlers read from a different goroutine; Go's memory
// model guarantees eventual visibility for simple types, which is
// sufficient for display purposes.

// State returns the current engine state.
func (e *Engine) State() flipState { return e.state }

// Generation returns the current cycle generation number.
func (e *Engine) Generation() int64 { return e.generation }

// SnapCount returns the number of snapshots collected in the current cycle.
func (e *Engine) SnapCount() int { return len(e.snapBuffer) }

// CurrentSide returns the side being evaluated ("yes", "no", or "").
func (e *Engine) CurrentSide() string { return e.crossSide }

// ConfirmTicksWaited returns how many ticks have elapsed since entering CONFIRMING.
func (e *Engine) ConfirmTicksWaited() int { return e.confirmCount }

// T0Features returns a snapshot of the T=0 features computed at crossing time.
// Returns nil if no crossing is active.
func (e *Engine) T0Features() *T0Features {
	if e.crossSnap == nil {
		return nil
	}
	return &T0Features{
		PathEff:        e.pathEff,
		NoiseRatio:     e.noiseRatio,
		Flips:          e.flips,
		Oscillating:    e.oscillating,
		RangeExpansion: e.rangeExpansion,
		BTCPosition:    e.btcPosition,
		BTCExtreme:     e.btcExtreme,
	}
}

// IsDone returns true if this cycle already produced a signal.
func (e *Engine) IsDone() bool { return e.doneThisGen }

// StateLabel returns a human-readable label for the current engine state.
func (e *Engine) StateLabel() string {
	switch e.state {
	case stateIdle:
		return "Idle"
	case stateWatching:
		return "Watching"
	case stateConfirming:
		return "Confirming"
	case stateDone:
		return "Done"
	default:
		return "Unknown"
	}
}

// Config returns a copy of the engine's running configuration.
func (e *Engine) Config() FlipConfig { return e.cfg }

// markSideTried marks the given side as having been attempted this generation.
func (e *Engine) markSideTried(side string) {
	if side == "yes" {
		e.yesTried = true
	} else {
		e.noTried = true
	}
}
