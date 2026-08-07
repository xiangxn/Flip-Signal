package flip

import (
	"math"
	"time"

	"github.com/necklace/lasttrading/internal/lab"
)

// Engine detects flip signals from a stream of ResearchSnapshots.
// It implements the Python backtest first_crossing_only logic exactly:
// check YES first → if signal, done; else check NO → if signal, done.
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

	// first_crossing_only tracking (matches Python: try YES, then NO)
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
// Matches Python backtest first_crossing_only:
//   - YES checked first, if signal produced → done
//   - If YES fails, NO is checked (including past crossings in buffer)
//   - If NO also fails → resume watching for new crossings
func (e *Engine) ProcessSnapshot(snap *lab.ResearchSnapshot, gen int64) *FlipSignal {
	if gen != e.generation || e.doneThisGen {
		return nil
	}

	// Always append to buffer so it reflects full cycle history.
	// When falling back from a failed confirmation, the buffer already
	// contains the confirmation tick and earlier crossings.
	bufIdx := len(e.snapBuffer)
	e.snapBuffer = append(e.snapBuffer, snap)

	// Track first crossing per side regardless of state (so crossings
	// during CONFIRMING are not lost for later fallback).
	// §2.1: remaining_sec >= MaxRemainingSec → window too early, skip (same level as >0.7)
	if e.yesFirstCrossIdx < 0 && snap.YesPrice > e.cfg.TriggerThreshold && snap.RemainingSec < e.cfg.MaxRemainingSec {
		e.yesFirstCrossIdx = bufIdx
	}
	if e.noFirstCrossIdx < 0 && snap.NoPrice > e.cfg.TriggerThreshold && snap.RemainingSec < e.cfg.MaxRemainingSec {
		e.noFirstCrossIdx = bufIdx
	}

	switch e.state {
	case stateIdle:
		return nil

	case stateWatching:
		// Try YES first (match Python order), then NO.
		// enterConfirming may return a signal synchronously if the confirmation
		// tick is already buffered (happens after fallback).
		if !e.yesTried && e.yesFirstCrossIdx >= 0 {
			return e.enterConfirming(e.yesFirstCrossIdx, "yes")
		}
		if !e.noTried && e.noFirstCrossIdx >= 0 {
			return e.enterConfirming(e.noFirstCrossIdx, "no")
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
			// Confirmation failed — try the other side (Python fallback)
			return e.fallbackAfterFailedConfirm()
		}
		return nil

	case stateDone:
		return nil
	}
	return nil
}

// enterConfirming transitions to CONFIRMING state using the crossing at the
// given buffer index. Computes all T=0 features from snapshots up to crossIdx.
func (e *Engine) enterConfirming(crossIdx int, side string) *FlipSignal {
	// Mark this side as tried
	if side == "yes" {
		e.yesTried = true
	} else {
		e.noTried = true
	}

	crossSnap := e.snapBuffer[crossIdx]

	// Check minimum pre-snapshots (including crossing snap)
	nPre := crossIdx + 1 // snapshots up to and including crossing
	if nPre < e.cfg.MinPreSnaps {
		// Not enough history — try other side instead
		return e.fallbackAfterFailedConfirm()
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
		return e.fallbackAfterFailedConfirm()
	}

	e.pathEff = netMove / preRange

	// path_eff too low → trend unclear, veto
	if e.pathEff < 0.4 {
		return e.fallbackAfterFailedConfirm()
	}

	totalPathVal := TotalPath(prePrices)
	if netMove > 0 {
		e.noiseRatio = totalPathVal / netMove
	} else {
		e.noiseRatio = totalPathVal // pure oscillation
	}

	// high noise ratio → PM price unstable, veto
	if e.noiseRatio > 3.0 {
		return e.fallbackAfterFailedConfirm()
	}

	e.flips = CountFlips(prePrices)
	e.oscillating = IsOscillating(e.pathEff, e.noiseRatio, e.flips, e.cfg)

	// Range expansion (only when hist is ready)
	if e.histRange.IsReady() {
		e.rangeExpansion = RangeExpansion(crossSnap.CurrentPrice, openPrice, e.histRange.AvgRange())
	}

	// F0: Range expansion too large — real breakout, PM is right, veto
	if e.histRange.IsReady() && e.rangeExpansion >= e.cfg.RangeExpMax {
		return e.fallbackAfterFailedConfirm()
	}

	// Save crossing state
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
		// This side failed too — try fallback again
		return e.fallbackAfterFailedConfirm()
	}

	// Normal path: wait for the confirmation tick to arrive
	e.state = stateConfirming
	return nil
}

// fallbackAfterFailedConfirm implements the Python first_crossing_only fallback:
// after one side fails, try the other side's first crossing from the buffer.
// If the other side hasn't crossed yet, resume WATCHING to catch future crossings.
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
