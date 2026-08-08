package flip

import (
	"testing"

	"github.com/necklace/flip-signal/internal/lab"
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
	// Formula A: path_eff=0.3≤0.8, noise=8.0>1.5, flips=5>1 → oscillating
	osc := IsOscillating(0.3, 8.0, 5, cfg)
	if !osc {
		t.Error("expected oscillating=true (Formula A thresholds)")
	}
}

func TestIsOscillating_No_PathEff(t *testing.T) {
	cfg := DefaultConfig()
	// Formula A: path_eff=0.9 > 0.8 → not oscillating
	osc := IsOscillating(0.9, 8.0, 5, cfg)
	if osc {
		t.Error("expected oscillating=false (path_eff=0.9 > 0.8)")
	}
}

func TestIsOscillating_No_Noise(t *testing.T) {
	cfg := DefaultConfig()
	// Formula A: noise_ratio=1.0 ≤ 1.5 → not oscillating
	osc := IsOscillating(0.3, 1.0, 5, cfg)
	if osc {
		t.Error("expected oscillating=false (noise_ratio=1.0 ≤ 1.5)")
	}
}

func TestIsOscillating_No_Flips(t *testing.T) {
	cfg := DefaultConfig()
	// Formula A: flips=1 ≤ 1 → not oscillating (>1 means ≥2)
	osc := IsOscillating(0.3, 8.0, 1, cfg)
	if osc {
		t.Error("expected oscillating=false (flips=1 ≤ 1)")
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
		OtherDelta:     0.06, // >0.05 → +3 (Formula A top tier)
		IsOscillating:  true, // +2 (Formula A)
		EntryPrice:     0.15, // <0.20 → +1 (elif, no stacking)
		RangeExpansion: 0.3,  // <0.5 → +2
		BTCPosition:    0.25, // NO side, >0.1 → BTC diverges → +1
		HistReady:      true,
		Cfg:            cfg,
	}
	score, vetoed := ComputeFlipScore(params)
	if vetoed {
		t.Error("unexpected veto")
	}
	expected := 9 // 3+2+1+2+1 = 9 (Formula A max with these inputs)
	if score != expected {
		t.Errorf("expected %d, got %d", expected, score)
	}
}

func TestComputeFlipScore_MinTrigger(t *testing.T) {
	cfg := DefaultConfig()
	params := ScoreParams{
		Side:           "no",
		OtherDelta:     0.015,  // >0.01 → +1 (Formula A weak)
		IsOscillating:  false,  // 0
		EntryPrice:     0.22,   // <0.25 → +1 (elif)
		RangeExpansion: 0.8,    // not <0.5 → 0
		BTCPosition:    0.25,   // NO side, >0.1 → BTC diverges → +1
		HistReady:      true,
		Cfg:            cfg,
	}
	score, vetoed := ComputeFlipScore(params)
	if vetoed {
		t.Error("unexpected veto")
	}
	expected := 3 // 1+1+1 = 3 (Formula A: weak od + entry + btc divergence)
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
		OtherDelta:     0.05,   // >0.02 → +2 (Formula A: not >0.05 so falls to strong)
		IsOscillating:  true,   // +2 (Formula A)
		EntryPrice:     0.15,   // <0.20 → +1
		RangeExpansion: 2.5,
		BTCPosition:    0,
		HistReady:      false, // hist not ready → skip F0, F6, F7
		Cfg:            cfg,
	}
	score, vetoed := ComputeFlipScore(params)
	if vetoed {
		t.Error("F0 should not veto when hist not ready")
	}
	expected := 5 // 2+2+1 = 5 (Formula A: strong od + osc + entry, no F6/F7)
	if score != expected {
		t.Errorf("expected %d, got %d", expected, score)
	}
}

func TestComputeFlipScore_OtherDeltaExclusive(t *testing.T) {
	cfg := DefaultConfig()
	// Formula A: OtherDelta=0.04 > 0.02 (strong) → +2, doesn't also get weak +1
	params := ScoreParams{
		Side:       "yes",
		OtherDelta: 0.04, // >0.02 → +2, NOT >0.05
		EntryPrice: 1.0,  // too high for cheap entry
		Cfg:        cfg,
	}
	score, _ := ComputeFlipScore(params)
	if score != 2 {
		t.Errorf("expected 2 (Formula A strong tier), got %d", score)
	}
}

func TestComputeFlipScore_EntryPriceExclusive(t *testing.T) {
	cfg := DefaultConfig()
	params := ScoreParams{
		Side:       "yes",
		EntryPrice: 0.18, // Formula A: <0.20 (strong) → +1, elif skips weak
		Cfg:        cfg,
	}
	score, _ := ComputeFlipScore(params)
	if score != 1 {
		t.Errorf("expected 1 (Formula A: strong entry, no stacking), got %d", score)
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
	// After veto, engine falls back to WATCHING (other side may still cross)
	if eng.state != stateWatching {
		t.Errorf("expected stateWatching after failing MinPreSnaps (fallback), got %d", eng.state)
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

	// Feed 5 snapshots: BTC dips slightly below open (to trigger btc_extreme Formula A)
	// Open=50010, prices trending down: 50010, 50009, 50008, 50007, 50006
	// path_eff ≈ 1.0 (trending), noise_ratio ≈ 1.0 (low noise) → oscillating=false
	openPrice := 50010.0
	for i := 0; i < 5; i++ {
		price := openPrice - float64(i)*1 // 50010, 50009, 50008, 50007, 50006
		snap := makeTestSnap(0.5, 0.3, price, openPrice, 250-i*5)
		sig := eng.ProcessSnapshot(snap, 1)
		if sig != nil {
			t.Fatalf("unexpected signal at snap %d", i)
		}
	}

	// Crossing: YES>0.7, BTC at 50005 (below open → btc_pos=-5/40=-0.125 < -0.1 → btc_extreme)
	// range_exp = |50005-50010|/40 = 5/40 = 0.125 < 0.5 → F6 +2
	crossSnap := makeTestSnap(0.75, 0.15, 50005, openPrice, 230)
	sig := eng.ProcessSnapshot(crossSnap, 1)
	if sig != nil {
		t.Fatal("expected nil after crossing (should be in CONFIRMING)")
	}
	if eng.state != stateConfirming {
		t.Fatalf("expected stateConfirming, got %d", eng.state)
	}

	// Confirmation: NO moved up → other_delta=0.07 > 0.05 → +3 (Formula A top tier)
	confSnap := makeTestSnap(0.78, 0.22, 50005, openPrice, 225)
	sig = eng.ProcessSnapshot(confSnap, 1)
	if sig == nil {
		t.Fatal("expected signal after confirmation (Formula A)")
	}

	// Verify signal
	if sig.Side != "yes" {
		t.Errorf("expected side=yes, got %s", sig.Side)
	}
	// other_delta = 0.22 - 0.15 = 0.07 > 0.05 → Formula A top tier +3
	if sig.OtherDelta < 0.05 {
		t.Errorf("other_delta expected >=0.05, got %.4f", sig.OtherDelta)
	}
	// entry = NO price = 0.15 → <0.20 → +1 (Formula A: re-activated)
	if sig.EntryPrice != 0.15 {
		t.Errorf("entry expected 0.15, got %.4f", sig.EntryPrice)
	}
	// Score: od>0.05(+3) + osc=false(0) + entry<0.20(+1) + range_exp<0.5(+2) + btc_ext(+1) = 7 ≥ 5
	if sig.Score < 5 {
		t.Errorf("score expected >=5, got %d", sig.Score)
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

	// Feed pre-cross snapshots (trending → oscillating=false)
	for i := 0; i < 5; i++ {
		price := 50000.0 + float64(i)*10
		snap := makeTestSnap(0.5, 0.3, price, 50000, 250-i*5)
		eng.ProcessSnapshot(snap, 1)
	}

	// Crossing
	crossSnap := makeTestSnap(0.75, 0.20, 50050, 50000, 230)
	eng.ProcessSnapshot(crossSnap, 1)

	// Confirmation with opposing side dropping: other_delta=-0.03
	// Formula A: od hard filter disabled (ODHardFilter=-999)
	// Score: od≤0.01(0) + osc(0) + entry=0.20<0.25(+1) + range_exp=50/40=1.25(0) + btc(0) = 1 < 5
	// → signal should still be nil (fails by score)
	confSnap := makeTestSnap(0.78, 0.17, 50050, 50000, 225)
	sig := eng.ProcessSnapshot(confSnap, 1)
	if sig != nil {
		t.Errorf("expected nil (Formula A: score too low with negative other_delta), got sig with other_delta=%.4f score=%d", sig.OtherDelta, sig.Score)
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
	// After F0 veto, engine falls back to WATCHING (other side may still cross)
	if eng.state != stateWatching {
		t.Errorf("expected stateWatching after F0 veto (fallback), got %d", eng.state)
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

func TestEngine_SkipEarlyCrossing(t *testing.T) {
	// §2.1: crossings at remaining_sec >= MaxRemainingSec are invalid
	// (window too early, BTC path too short). Only crossings after the
	// window matures (rem < 260) should be tracked.
	cfg := DefaultConfig()
	ht := NewHistRangeTracker(18)
	eng := NewEngine(cfg, ht)
	eng.Reset(1)

	// Early crossing at rem=270 (>=260) — should NOT be tracked
	snapEarly := makeTestSnap(0.75, 0.30, 50000, 50000, 270)
	eng.ProcessSnapshot(snapEarly, 1)
	if eng.yesFirstCrossIdx != -1 {
		t.Errorf("early YES crossing (rem=270 >= %d) should not be tracked", cfg.MaxRemainingSec)
	}
	if eng.noFirstCrossIdx != -1 {
		t.Errorf("early NO crossing at rem=270: noPrice=0.30 < 0.7, should not be tracked anyway")
	}

	// Also verify early NO crossing is skipped
	snapEarlyNO := makeTestSnap(0.30, 0.75, 50000, 50000, 265)
	eng.ProcessSnapshot(snapEarlyNO, 1)
	if eng.noFirstCrossIdx != -1 {
		t.Errorf("early NO crossing (rem=265 >= %d) should not be tracked", cfg.MaxRemainingSec)
	}

	// Later crossing at rem=250 (<260) — SHOULD be tracked
	snapValid := makeTestSnap(0.80, 0.20, 50010, 50000, 250)
	eng.ProcessSnapshot(snapValid, 1)
	if eng.yesFirstCrossIdx < 0 {
		t.Error("valid YES crossing (rem=250 < 260) should be tracked")
	}
	if eng.noFirstCrossIdx != -1 {
		t.Error("noPrice=0.20 < 0.7, NO should not be tracked")
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
