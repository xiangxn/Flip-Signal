package flip

import (
	"math"
	"time"

	"github.com/necklace/lasttrading/internal/lab"
)

// Engine detects flip signals from a stream of ResearchSnapshots.
// It implements a state machine: WATCHING → CONFIRMING → DONE.
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

	snapBuffer   []*lab.ResearchSnapshot // snapshots up to and including crossing
	crossSnap    *lab.ResearchSnapshot   // snapshot at crossing moment
	crossSide    string                  // "yes" or "no"
	confirmCount int                     // ticks waited in CONFIRMING state
	generation   int64
	doneThisGen  bool

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
		cfg:       cfg,
		histRange: histRange,
		state:     stateIdle,
	}
}

// Reset prepares the engine for a new market cycle.
func (e *Engine) Reset(generation int64) {
	e.state = stateWatching
	e.snapBuffer = e.snapBuffer[:0]
	e.crossSnap = nil
	e.crossSide = ""
	e.confirmCount = 0
	e.generation = generation
	e.doneThisGen = false

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
func (e *Engine) ProcessSnapshot(snap *lab.ResearchSnapshot, gen int64) *FlipSignal {
	if gen != e.generation || e.doneThisGen {
		return nil
	}

	switch e.state {
	case stateIdle:
		return nil

	case stateWatching:
		// Append first, then check — so snapBuffer includes the crossing snap.
		e.snapBuffer = append(e.snapBuffer, snap)

		if snap.YesPrice > e.cfg.TriggerThreshold {
			return e.onCrossing(snap, "yes")
		}
		if snap.NoPrice > e.cfg.TriggerThreshold {
			return e.onCrossing(snap, "no")
		}
		return nil

	case stateConfirming:
		// Do NOT append to snapBuffer here — post-cross snapshots are only
		// used for other_delta computation.
		e.confirmCount++
		if e.confirmCount >= e.cfg.ConfirmDelayTicks {
			return e.onConfirmed(snap)
		}
		return nil

	case stateDone:
		return nil
	}
	return nil
}

// onCrossing is called when a >0.7 crossing is detected.
// It computes all T=0 features and either vetoes (F0) or transitions to CONFIRMING.
func (e *Engine) onCrossing(snap *lab.ResearchSnapshot, side string) *FlipSignal {
	// Check minimum pre-snapshots
	if len(e.snapBuffer) < e.cfg.MinPreSnaps {
		e.state = stateDone
		return nil
	}

	// Extract pre-prices from snapBuffer (includes crossing snap at end)
	prePrices := make([]float64, len(e.snapBuffer))
	for i, s := range e.snapBuffer {
		prePrices[i] = s.CurrentPrice
	}

	openPrice := snap.OpenPrice

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
		e.state = stateDone
		return nil
	}

	e.pathEff = netMove / preRange

	// OPT#7: path_eff too low → trend unclear, veto
	if e.pathEff < 0.4 {
		e.state = stateDone
		return nil
	}
	totalPathVal := TotalPath(prePrices)
	if netMove > 0 {
		e.noiseRatio = totalPathVal / netMove
	} else {
		e.noiseRatio = totalPathVal // pure oscillation
	}

	// OPT#3: high noise ratio → PM price unstable, veto
	if e.noiseRatio > 3.0 {
		e.state = stateDone
		return nil
	}
	e.flips = CountFlips(prePrices)
	e.oscillating = IsOscillating(e.pathEff, e.noiseRatio, e.flips, e.cfg)

	// Range expansion (only when hist is ready)
	if e.histRange.IsReady() {
		e.rangeExpansion = RangeExpansion(snap.CurrentPrice, openPrice, e.histRange.AvgRange())
	}

	// F0: Range expansion too large — real breakout, PM is right, veto
	if e.histRange.IsReady() && e.rangeExpansion >= e.cfg.RangeExpMax {
		e.state = stateDone
		return nil
	}

	// Save crossing state and enter confirmation phase
	e.crossSnap = snap
	e.crossSide = side
	e.confirmCount = 0
	e.state = stateConfirming
	return nil
}

// onConfirmed is called after the confirmation delay (ConfirmDelayTicks).
// It computes the T+5s confirmation signal and the final score.
func (e *Engine) onConfirmed(snap *lab.ResearchSnapshot) *FlipSignal {
	e.state = stateDone
	e.doneThisGen = true

	// Compute other_delta — opposite-side price change
	var otherDelta float64
	if e.crossSide == "yes" {
		// YES>0.7, opposite is NO
		otherDelta = snap.NoPrice - e.crossSnap.NoPrice
	} else {
		// NO>0.7, opposite is YES
		otherDelta = snap.YesPrice - e.crossSnap.YesPrice
	}

	// OPT#1: Hard filter — other_delta < 0.03 never wins
	if otherDelta < 0.03 {
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
		// OPT#2: BTC divergence from PM = flip edge. PM and BTC agree = real trend.
		if e.crossSide == "yes" {
			// YES>0.7 (PM bullish), BTC slightly down → PM overreacting
			e.btcExtreme = e.btcPosition > e.cfg.BTCPosMin && e.btcPosition < 0
		} else {
			// NO>0.7 (PM bearish), BTC slightly up → PM overreacting
			e.btcExtreme = e.btcPosition > 0 && e.btcPosition < e.cfg.BTCPosMax
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
