package flip

import (
	"testing"

	"github.com/necklace/lasttrading/internal/lab"
)

// ═══════════════════════════════════════════════════════════════
// Feature extraction tests
// ═══════════════════════════════════════════════════════════════

func TestPathEfficiency_Trending(t *testing.T) {
	// Price goes straight up: 100 → 101 → 102 → 103 → 104
	// net_move = abs(104-100) = 4, pre_range = 104-100 = 4
	// path_eff = 4/4 = 1.0
	prices := []float64{100, 101, 102, 103, 104}
	eff := PathEfficiency(prices, 100)
	if eff != 1.0 {
		t.Errorf("expected 1.0, got %.4f", eff)
	}
}

func TestPathEfficiency_Oscillating(t *testing.T) {
	// Price oscillates: 100 → 103 → 100 → 103 → 100
	// net_move = abs(100-100) = 0, pre_range = 103-100 = 3
	// path_eff = 0/3 = 0.0
	prices := []float64{100, 103, 100, 103, 100}
	eff := PathEfficiency(prices, 100)
	if eff != 0.0 {
		t.Errorf("expected 0.0, got %.4f", eff)
	}
}

func TestPathEfficiency_Mixed(t *testing.T) {
	// Price goes up with pullback: 100 → 102 → 101 → 103 → 104
	// net_move = abs(104-100) = 4, pre_range = 104-100 = 4
	// path_eff = 4/4 = 1.0 (range captures extremes)
	prices := []float64{100, 102, 101, 103, 104}
	eff := PathEfficiency(prices, 100)
	if eff != 1.0 {
		t.Errorf("expected 1.0, got %.4f", eff)
	}
}

func TestTotalPath(t *testing.T) {
	prices := []float64{100, 102, 101, 103}
	// |102-100| + |101-102| + |103-101| = 2 + 1 + 2 = 5
	tp := TotalPath(prices)
	if tp != 5.0 {
		t.Errorf("expected 5.0, got %.4f", tp)
	}
}

func TestNoiseRatio(t *testing.T) {
	// Trending: 100 → 101 → 102 → 103 → 104
	// total_path = 1+1+1+1 = 4, net_move = 4 → ratio = 1.0
	prices := []float64{100, 101, 102, 103, 104}
	nr := NoiseRatio(prices, 4.0)
	if nr != 1.0 {
		t.Errorf("expected 1.0, got %.4f", nr)
	}
}

func TestNoiseRatio_ZeroNetMove(t *testing.T) {
	// Pure oscillation returning to start
	prices := []float64{100, 103, 100, 103, 100}
	// total_path = 3+3+3+3 = 12
	nr := NoiseRatio(prices, 0)
	if nr != 12.0 {
		t.Errorf("expected 12.0, got %.4f", nr)
	}
}

func TestCountFlips(t *testing.T) {
	// 100 → 102 ↑, 102 → 101 ↓, 101 → 103 ↑ → 2 flips
	prices := []float64{100, 102, 101, 103}
	flips := CountFlips(prices)
	if flips != 2 {
		t.Errorf("expected 2, got %d", flips)
	}
}

func TestCountFlips_FlatIgnored(t *testing.T) {
	// 100 → 102 ↑, 102 → 102 (flat, ignored → d1=0 skip), 102 → 101 ↓
	// At i=2: d1=0 (102-102), skip → 0 flips. Flat ticks absorb both surrounding moves.
	// Python behavior is identical.
	prices := []float64{100, 102, 102, 101}
	flips := CountFlips(prices)
	if flips != 0 {
		t.Errorf("expected 0 (flat tick absorbs surrounding direction changes), got %d", flips)
	}
}

func TestCountFlips_TooShort(t *testing.T) {
	prices := []float64{100, 101}
	flips := CountFlips(prices)
	if flips != 0 {
		t.Errorf("expected 0 for short slice, got %d", flips)
	}
}

func TestIsOscillating_Yes(t *testing.T) {
	cfg := DefaultConfig()
	// path_eff=0.3, noise=8.0, flips=5 → should be oscillating
	osc := IsOscillating(0.3, 8.0, 5, cfg)
	if !osc {
		t.Error("expected oscillating=true")
	}
}

func TestIsOscillating_No_PathEff(t *testing.T) {
	cfg := DefaultConfig()
	osc := IsOscillating(0.6, 8.0, 5, cfg) // path_eff > 0.5
	if osc {
		t.Error("expected oscillating=false (path_eff too high)")
	}
}

func TestIsOscillating_No_Noise(t *testing.T) {
	cfg := DefaultConfig()
	osc := IsOscillating(0.3, 3.0, 5, cfg) // noise_ratio <= 5
	if osc {
		t.Error("expected oscillating=false (noise_ratio too low)")
	}
}

func TestIsOscillating_No_Flips(t *testing.T) {
	cfg := DefaultConfig()
	osc := IsOscillating(0.3, 8.0, 2, cfg) // flips <= 2
	if osc {
		t.Error("expected oscillating=false (flips too few)")
	}
}

func TestRangeExpansion(t *testing.T) {
	// BTC moved $50 from open, hist avg = $100 → 0.5
	re := RangeExpansion(50100, 50050, 100)
	if re != 0.5 {
		t.Errorf("expected 0.5, got %.4f", re)
	}
}

func TestRangeExpansion_ZeroHist(t *testing.T) {
	re := RangeExpansion(50100, 50050, 0)
	if re != 0 {
		t.Errorf("expected 0, got %.4f", re)
	}
}

func TestBTCPosition(t *testing.T) {
	// BTC up $25 from open, hist avg = $100 → 0.25
	pos := BTCPosition(50075, 50050, 100)
	if pos != 0.25 {
		t.Errorf("expected 0.25, got %.4f", pos)
	}
}

func TestBTCPosition_ZeroHist(t *testing.T) {
	pos := BTCPosition(50075, 50050, 0)
	if pos != 0 {
		t.Errorf("expected 0, got %.4f", pos)
	}
}

// ═══════════════════════════════════════════════════════════════
// Scoring tests
// ═══════════════════════════════════════════════════════════════

func TestComputeFlipScore_MaxScore(t *testing.T) {
	cfg := DefaultConfig()
	params := ScoreParams{
		Side:           "no",
		OtherDelta:     0.05,  // >0.03 → +4
		IsOscillating:  true,  // +1
		EntryPrice:     0.22,  // <0.25 (weak) → +1 (OPT#5: strong bonus removed)
		RangeExpansion: 0.3,   // <0.5 → +2
		BTCPosition:    0.25,  // OPT#2: no side, 0<0.25<0.5 → BTC divergence → +1
		HistReady:      true,
		Cfg:            cfg,
	}
	score, vetoed := ComputeFlipScore(params)
	if vetoed {
		t.Error("unexpected veto")
	}
	expected := 9 // 4+1+1+2+1 = 9 (max after OPT#5 removes strong entry bonus)
	if score != expected {
		t.Errorf("expected %d, got %d", expected, score)
	}
}

func TestComputeFlipScore_MinTrigger(t *testing.T) {
	cfg := DefaultConfig()
	params := ScoreParams{
		Side:           "no",
		OtherDelta:     0.02,  // >0.01 → +2
		IsOscillating:  false, // 0
		EntryPrice:     0.22,  // <0.25 → +1 (OPT#5: strong threshold=0, falls through)
		RangeExpansion: 0.8,   // not <0.5 → 0
		BTCPosition:    0.25,  // OPT#2: no side, 0<0.25<0.5 → divergence → +1
		HistReady:      true,
		Cfg:            cfg,
	}
	score, vetoed := ComputeFlipScore(params)
	if vetoed {
		t.Error("unexpected veto")
	}
	expected := 4 // 2+1+1 = 4 (OPT#2 adds F7 since BTC diverges from PM)
	if score != expected {
		t.Errorf("expected %d, got %d", expected, score)
	}
}

func TestComputeFlipScore_F0Veto(t *testing.T) {
	cfg := DefaultConfig()
	params := ScoreParams{
		Side:           "yes",
		OtherDelta:     0.05,
		IsOscillating:  true,
		EntryPrice:     0.15,
		RangeExpansion: 2.5, // ≥2.0 → veto
		BTCPosition:    0.25,
		HistReady:      true,
		Cfg:            cfg,
	}
	_, vetoed := ComputeFlipScore(params)
	if !vetoed {
		t.Error("expected F0 veto for range_expansion >= 2.0")
	}
}

func TestComputeFlipScore_F0NoVetoIfHistNotReady(t *testing.T) {
	cfg := DefaultConfig()
	params := ScoreParams{
		Side:           "yes",
		OtherDelta:     0.05,
		IsOscillating:  true,
		EntryPrice:     0.15,
		RangeExpansion: 2.5,
		BTCPosition:    0,
		HistReady:      false, // hist not ready → skip F0, F6, F7
		Cfg:            cfg,
	}
	score, vetoed := ComputeFlipScore(params)
	if vetoed {
		t.Error("F0 should not veto when hist not ready")
	}
	expected := 6 // 4+1+1 = 6 (OPT#5: strong→weak, no F6, F7)
	if score != expected {
		t.Errorf("expected %d, got %d", expected, score)
	}
}

func TestComputeFlipScore_OtherDeltaExclusive(t *testing.T) {
	cfg := DefaultConfig()
	// OtherDelta=0.04 matches both strong and weak → only strong applies
	params := ScoreParams{
		Side:  "yes",
		OtherDelta: 0.04, // >0.03 → +4, doesn't also get +2
		EntryPrice: 1.0,  // too high for cheap entry
		Cfg:       cfg,
	}
	score, _ := ComputeFlipScore(params)
	// Only F1 should trigger
	if score != 4 {
		t.Errorf("expected 4, got %d", score)
	}
}

func TestComputeFlipScore_EntryPriceExclusive(t *testing.T) {
	cfg := DefaultConfig()
	params := ScoreParams{
		Side:       "yes",
		EntryPrice: 0.18, // OPT#5: EntryCheapStrong=0, falls through to weak <0.25 → +1
		Cfg:        cfg,
	}
	score, _ := ComputeFlipScore(params)
	if score != 1 {
		t.Errorf("expected 1, got %d", score)
	}
}

// ═══════════════════════════════════════════════════════════════
// Engine state machine tests
// ═══════════════════════════════════════════════════════════════

func makeTestSnap(yesPrice, noPrice, price, openPrice float64, remainingSec int) *lab.ResearchSnapshot {
	return &lab.ResearchSnapshot{
		CurrentPrice: price,
		OpenPrice:    openPrice,
		YesPrice:     yesPrice,
		NoPrice:      noPrice,
		RemainingSec: remainingSec,
	}
}

func TestEngine_SkipWhenIdle(t *testing.T) {
	cfg := DefaultConfig()
	ht := NewHistRangeTracker(18)
	eng := NewEngine(cfg, ht)

	snap := makeTestSnap(0.8, 0.2, 50000, 50000, 250)
	sig := eng.ProcessSnapshot(snap, 0)
	if sig != nil {
		t.Error("expected nil when engine is idle")
	}
}

func TestEngine_NoCrossing(t *testing.T) {
	cfg := DefaultConfig()
	ht := NewHistRangeTracker(18)
	eng := NewEngine(cfg, ht)
	eng.Reset(1)

	// Price below trigger
	snap := makeTestSnap(0.5, 0.3, 50000, 50000, 250)
	sig := eng.ProcessSnapshot(snap, 1)
	if sig != nil {
		t.Error("expected nil when no crossing")
	}
	if eng.state != stateWatching {
		t.Errorf("expected stateWatching, got %d", eng.state)
	}
}

func TestEngine_TooFewPreSnaps(t *testing.T) {
	cfg := DefaultConfig()
	ht := NewHistRangeTracker(18)
	eng := NewEngine(cfg, ht)
	eng.Reset(1)

	// Crossing on second snapshot (< MinPreSnaps=5)
	for i := 0; i < 3; i++ {
		snap := makeTestSnap(0.5, 0.3, 50000+float64(i)*10, 50000, 250-i*5)
		eng.ProcessSnapshot(snap, 1)
	}

	// 4th snapshot crosses but still too few (only 4)
	snap := makeTestSnap(0.8, 0.2, 50030, 50000, 235)
	sig := eng.ProcessSnapshot(snap, 1)
	if sig != nil {
		t.Error("expected nil when too few pre-snapshots")
	}
	if eng.state != stateDone {
		t.Errorf("expected stateDone after failing MinPreSnaps, got %d", eng.state)
	}
}

func TestEngine_CrossingWithConfirm(t *testing.T) {
	cfg := DefaultConfig()
	cfg.MinPreSnaps = 2 // lower for test
	ht := NewHistRangeTracker(18)
	// Make hist ready with some dummy data
	ht.AddRange(100, 150) // range=50
	ht.AddRange(100, 130) // range=30
	ht.AddRange(100, 140) // range=40 → avg=40

	eng := NewEngine(cfg, ht)
	eng.Reset(1)

	// Feed 5 snapshots with gentle uptrend
	// Price goes: 50000, 50010, 50020, 50030, 50040
	// Open=50000, path_eff = abs(50040-50000)/(50040-50000) = 1.0 (trending)
	// noise_ratio will be low
	for i := 0; i < 5; i++ {
		price := 50000.0 + float64(i)*10
		snap := makeTestSnap(0.5+float64(i)*0.05, 0.3, price, 50000, 250-i*5)
		sig := eng.ProcessSnapshot(snap, 1)
		if sig != nil {
			t.Fatalf("unexpected signal at snap %d", i)
		}
	}

	// Now crossing: YES=0.75 > 0.7
	crossSnap := makeTestSnap(0.75, 0.15, 50050, 50000, 230)
	sig := eng.ProcessSnapshot(crossSnap, 1)
	if sig != nil {
		t.Fatal("expected nil after crossing (should be in CONFIRMING)")
	}
	if eng.state != stateConfirming {
		t.Fatalf("expected stateConfirming, got %d", eng.state)
	}

	// Confirmation snapshot: YES stayed high, NO moved up (favorable)
	confSnap := makeTestSnap(0.78, 0.20, 50050, 50000, 225)
	sig = eng.ProcessSnapshot(confSnap, 1)
	if sig == nil {
		t.Fatal("expected signal after confirmation")
	}

	// Verify signal
	if sig.Side != "yes" {
		t.Errorf("expected side=yes, got %s", sig.Side)
	}
	// other_delta = 0.20 - 0.15 = 0.05 > 0.03 → +4
	if sig.OtherDelta < 0.03 {
		t.Errorf("other_delta expected >=0.03, got %.4f", sig.OtherDelta)
	}
	// entry = NO price = 0.15 → <0.25 (weak) → +1 (OPT#5: strong removed)
	if sig.EntryPrice != 0.15 {
		t.Errorf("entry expected 0.15, got %.4f", sig.EntryPrice)
	}
	// At least 4+1 = 5 score
	if sig.Score < 5 {
		t.Errorf("score expected >=5, got %d", sig.Score)
	}
	// Not trending (path_eff=1.0 > 0.5) → oscillating=false
	if sig.IsOscillating {
		t.Error("not oscillating (trending)")
	}
	if sig.Shares != 1 {
		t.Errorf("expected 1 share, got %d", sig.Shares)
	}
}

func TestEngine_CrossingWithUnfavorableOtherDelta(t *testing.T) {
	cfg := DefaultConfig()
	cfg.MinPreSnaps = 2
	ht := NewHistRangeTracker(18)
	ht.AddRange(100, 150)
	ht.AddRange(100, 130)
	ht.AddRange(100, 140)

	eng := NewEngine(cfg, ht)
	eng.Reset(1)

	// Feed pre-cross snapshots
	for i := 0; i < 5; i++ {
		price := 50000.0 + float64(i)*10
		snap := makeTestSnap(0.5, 0.3, price, 50000, 250-i*5)
		eng.ProcessSnapshot(snap, 1)
	}

	// Crossing
	crossSnap := makeTestSnap(0.75, 0.20, 50050, 50000, 230)
	eng.ProcessSnapshot(crossSnap, 1)

	// Confirmation with opposing side dropping → hard filter
	confSnap := makeTestSnap(0.78, 0.17, 50050, 50000, 225)
	sig := eng.ProcessSnapshot(confSnap, 1)
	if sig != nil {
		t.Errorf("expected nil when other_delta < 0.03 (OPT#1), got sig with other_delta=%.4f", sig.OtherDelta)
	}
}

func TestEngine_F0Veto_RealBreakout(t *testing.T) {
	cfg := DefaultConfig()
	cfg.MinPreSnaps = 2
	ht := NewHistRangeTracker(18)
	ht.AddRange(100, 120) // range=20
	ht.AddRange(100, 130) // range=30
	ht.AddRange(100, 140) // range=40 → avg=30, ready=true

	eng := NewEngine(cfg, ht)
	eng.Reset(1)

	// Feed snapshots with big BTC move: 50000 → 50100 （$100 = 3.3x hist range of ~$30）
	for i := 0; i < 5; i++ {
		price := 50000.0 + float64(i)*25 // 0, 25, 50, 75, 100
		snap := makeTestSnap(0.6, 0.3, price, 50000, 250-i*5)
		eng.ProcessSnapshot(snap, 1)
	}

	// Crossing with big BTC move → F0 veto
	crossSnap := makeTestSnap(0.75, 0.20, 50100, 50000, 230)
	sig := eng.ProcessSnapshot(crossSnap, 1)
	if sig != nil {
		t.Error("expected F0 veto for range_expansion >= 2.0")
	}
	if eng.state != stateDone {
		t.Errorf("expected stateDone after F0 veto, got %d", eng.state)
	}
}

func TestEngine_OnlyFirstCrossing(t *testing.T) {
	cfg := DefaultConfig()
	cfg.MinPreSnaps = 2
	ht := NewHistRangeTracker(18)
	ht.AddRange(100, 150)
	ht.AddRange(100, 130)
	ht.AddRange(100, 140)

	eng := NewEngine(cfg, ht)
	eng.Reset(1)

	// Feed pre-cross
	for i := 0; i < 5; i++ {
		snap := makeTestSnap(0.3, 0.3, 50000, 50000, 250-i*5)
		eng.ProcessSnapshot(snap, 1)
	}

	// First crossing: YES>0.7
	cross := makeTestSnap(0.75, 0.15, 50010, 50000, 230)
	eng.ProcessSnapshot(cross, 1)
	// Confirm
	conf := makeTestSnap(0.78, 0.20, 50010, 50000, 225)
	sig1 := eng.ProcessSnapshot(conf, 1)
	if sig1 == nil {
		t.Fatal("expected first signal")
	}

	// Next snapshot should not trigger (doneThisGen)
	next := makeTestSnap(0.80, 0.18, 50010, 50000, 220)
	sig2 := eng.ProcessSnapshot(next, 1)
	if sig2 != nil {
		t.Error("expected nil after doneThisGen")
	}
}

func TestEngine_ResetForNewCycle(t *testing.T) {
	cfg := DefaultConfig()
	cfg.MinPreSnaps = 2
	ht := NewHistRangeTracker(18)

	eng := NewEngine(cfg, ht)
	eng.Reset(1)

	// Complete a cycle
	for i := 0; i < 5; i++ {
		snap := makeTestSnap(0.3, 0.3, 50000, 50000, 250-i*5)
		eng.ProcessSnapshot(snap, 1)
	}
	cross := makeTestSnap(0.75, 0.15, 50010, 50000, 230)
	eng.ProcessSnapshot(cross, 1)
	// (skip confirmation for this test)

	// Reset for new cycle
	eng.Reset(2)

	if eng.state != stateWatching {
		t.Errorf("expected stateWatching after reset, got %d", eng.state)
	}
	if len(eng.snapBuffer) != 0 {
		t.Errorf("snapBuffer should be empty after reset, got %d", len(eng.snapBuffer))
	}
	if eng.doneThisGen {
		t.Error("doneThisGen should be false after reset")
	}
}

// ═══════════════════════════════════════════════════════════════
// HistRangeTracker tests
// ═══════════════════════════════════════════════════════════════

func TestHistRangeTracker_NotReadyInitially(t *testing.T) {
	ht := NewHistRangeTracker(18)
	if ht.IsReady() {
		t.Error("should not be ready with 0 ranges")
	}
	if ht.AvgRange() != 0 {
		t.Errorf("avg should be 0, got %.4f", ht.AvgRange())
	}
}

func TestHistRangeTracker_AddAndAvg(t *testing.T) {
	ht := NewHistRangeTracker(18)

	ht.AddRange(100, 110) // range=10
	ht.AddRange(100, 120) // range=20

	if ht.IsReady() {
		t.Error("should not be ready with only 2 ranges")
	}

	ht.AddRange(100, 130) // range=30

	if !ht.IsReady() {
		t.Error("should be ready with 3 ranges")
	}

	avg := ht.AvgRange()
	expected := (10.0 + 20.0 + 30.0) / 3.0
	if avg != expected {
		t.Errorf("expected avg %.4f, got %.4f", expected, avg)
	}
}

func TestHistRangeTracker_FIFOEviction(t *testing.T) {
	ht := NewHistRangeTracker(3) // small window

	ht.AddRange(100, 110) // 10
	ht.AddRange(100, 120) // 20
	ht.AddRange(100, 130) // 30 → avg=20
	ht.AddRange(100, 140) // 40 → evict 10, avg=(20+30+40)/3=30

	avg := ht.AvgRange()
	expected := (20.0 + 30.0 + 40.0) / 3.0
	if avg != expected {
		t.Errorf("expected avg %.4f, got %.4f", expected, avg)
	}
}
