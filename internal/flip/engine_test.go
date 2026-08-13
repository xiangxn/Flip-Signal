package flip

import (
	"testing"

	"github.com/necklace/flip-signal/internal/lab"
)

// ═══════════════════════════════════════════════════════════════
// Feature extraction tests（Formula B）
// ═══════════════════════════════════════════════════════════════

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

func TestDivergence(t *testing.T) {
	// YES 侧触发（PM 看涨）：BTC 跌 = 背离（取 -btc_pos）
	if d := Divergence("yes", -0.2); d != 0.2 {
		t.Errorf("expected 0.2 for YES side, got %.4f", d)
	}
	if d := Divergence("yes", 0.2); d != -0.2 {
		t.Errorf("expected -0.2 for YES side (BTC up = 同向), got %.4f", d)
	}
	// NO 侧触发（PM 看跌）：BTC 涨 = 背离（取 +btc_pos）
	if d := Divergence("no", 0.2); d != 0.2 {
		t.Errorf("expected 0.2 for NO side, got %.4f", d)
	}
	if d := Divergence("no", -0.2); d != -0.2 {
		t.Errorf("expected -0.2 for NO side (BTC down = 同向), got %.4f", d)
	}
}

// ═══════════════════════════════════════════════════════════════
// Scoring tests（Formula B：B3 三档 + B2 过度自信，最高 5 分）
// ═══════════════════════════════════════════════════════════════

func TestComputeFlipScore_MaxScore(t *testing.T) {
	cfg := DefaultConfig()
	params := ScoreParams{
		OtherDelta:     0.06, // >0.05 → +3（B3 最高档）
		RangeExpansion: 0.3,  // <0.5 → +2（B2）
		HistReady:      true,
		Cfg:            cfg,
	}
	score, vetoed := ComputeFlipScore(params)
	if vetoed {
		t.Error("unexpected veto")
	}
	if score != 5 {
		t.Errorf("expected 5 (Formula B max), got %d", score)
	}
}

func TestComputeFlipScore_BelowEntry(t *testing.T) {
	cfg := DefaultConfig()
	params := ScoreParams{
		OtherDelta:     0.015, // >0.01 → +1（B3 弱档）
		RangeExpansion: 1.2,   // ≥1.0 → B2 不参与
		HistReady:      true,
		Cfg:            cfg,
	}
	score, vetoed := ComputeFlipScore(params)
	if vetoed {
		t.Error("unexpected veto")
	}
	if score != 1 {
		t.Errorf("expected 1 (below ScoreEntry=2), got %d", score)
	}
}

func TestComputeFlipScore_F0Veto(t *testing.T) {
	cfg := DefaultConfig()
	params := ScoreParams{
		OtherDelta:     0.06,
		RangeExpansion: 2.5, // ≥1.5 → 真突破否决
		HistReady:      true,
		Cfg:            cfg,
	}
	_, vetoed := ComputeFlipScore(params)
	if !vetoed {
		t.Error("expected F0 veto for range_expansion >= 1.5")
	}
}

func TestComputeFlipScore_F0NoVetoIfHistNotReady(t *testing.T) {
	cfg := DefaultConfig()
	params := ScoreParams{
		OtherDelta:     0.04, // >0.02 → +2（B3 中档）
		RangeExpansion: 2.5,
		HistReady:      false, // hist 未就绪 → F0/B2 不参与
		Cfg:            cfg,
	}
	score, vetoed := ComputeFlipScore(params)
	if vetoed {
		t.Error("F0 should not veto when hist not ready")
	}
	if score != 2 {
		t.Errorf("expected 2 (B3 strong only), got %d", score)
	}
}

func TestComputeFlipScore_OtherDeltaExclusive(t *testing.T) {
	cfg := DefaultConfig()
	// OtherDelta=0.04 > 0.02（中档）→ +2，不叠加弱档 +1
	params := ScoreParams{
		OtherDelta: 0.04,
		Cfg:        cfg,
	}
	score, _ := ComputeFlipScore(params)
	if score != 2 {
		t.Errorf("expected 2 (B3 strong tier), got %d", score)
	}
}

func TestComputeFlipScore_RangeOnly(t *testing.T) {
	cfg := DefaultConfig()
	// 纯 B2 信号：od 无贡献，range_exp < 0.5 → +2 ≥ ScoreEntry
	params := ScoreParams{
		OtherDelta:     -0.02, // 对侧回落，无确认
		RangeExpansion: 0.2,
		HistReady:      true,
		Cfg:            cfg,
	}
	score, vetoed := ComputeFlipScore(params)
	if vetoed {
		t.Error("unexpected veto")
	}
	if score != 2 {
		t.Errorf("expected 2 (B2 only), got %d", score)
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

// readyHist 构造就绪的历史振幅追踪器（3 段：50/30/40 → avg=40）。
func readyHist() *HistRangeTracker {
	ht := NewHistRangeTracker(18)
	ht.AddRange(100, 150)
	ht.AddRange(100, 130)
	ht.AddRange(100, 140)
	return ht
}

func TestEngine_SkipWhenIdle(t *testing.T) {
	cfg := DefaultConfig()
	eng := NewEngine(cfg, readyHist())

	snap := makeTestSnap(0.8, 0.2, 50000, 50000, 250)
	if sig := eng.ProcessSnapshot(snap, 0); sig != nil {
		t.Error("expected nil when engine is idle")
	}
}

func TestEngine_NoCrossing(t *testing.T) {
	cfg := DefaultConfig()
	eng := NewEngine(cfg, readyHist())
	eng.Reset(1)

	// Price below trigger
	snap := makeTestSnap(0.5, 0.3, 50000, 50000, 250)
	if sig := eng.ProcessSnapshot(snap, 1); sig != nil {
		t.Error("expected nil when no crossing")
	}
	if eng.state != stateWatching {
		t.Errorf("expected stateWatching, got %d", eng.state)
	}
}

func TestEngine_TooFewPreSnaps(t *testing.T) {
	cfg := DefaultConfig()
	eng := NewEngine(cfg, readyHist())
	eng.Reset(1)

	// Crossing on second snapshot (< MinPreSnaps=5)
	for i := 0; i < 3; i++ {
		snap := makeTestSnap(0.5, 0.3, 50000-float64(i)*10, 50000, 250-i*5)
		eng.ProcessSnapshot(snap, 1)
	}

	// 4th snapshot crosses but still too few (only 4)
	snap := makeTestSnap(0.8, 0.2, 49970, 50000, 235)
	if sig := eng.ProcessSnapshot(snap, 1); sig != nil {
		t.Error("expected nil when too few pre-snapshots")
	}
	if eng.state != stateWatching {
		t.Errorf("expected stateWatching after failing MinPreSnaps, got %d", eng.state)
	}
}

func TestEngine_CrossingWithConfirm(t *testing.T) {
	cfg := DefaultConfig()
	cfg.MinPreSnaps = 2
	cfg.ConfirmDelayTicks = 1
	eng := NewEngine(cfg, readyHist())
	eng.Reset(1)

	// Feed 5 pre-cross snapshots: BTC trending down（YES 侧触发需背离 = BTC 跌）
	// Open=50010, prices: 50010 → 50005 → btc_pos=-5/40=-0.125, div=+0.125
	openPrice := 50010.0
	for i := 0; i < 5; i++ {
		price := openPrice - float64(i)*1
		snap := makeTestSnap(0.5, 0.3, price, openPrice, 250-i*5)
		if sig := eng.ProcessSnapshot(snap, 1); sig != nil {
			t.Fatalf("unexpected signal at snap %d", i)
		}
	}

	// 穿越: YES>0.7，BTC=50005（低于 open → 背离 ✓）
	// range_exp = 5/40 = 0.125 < 1.0 → B2 +2
	crossSnap := makeTestSnap(0.75, 0.15, 50005, openPrice, 230)
	if sig := eng.ProcessSnapshot(crossSnap, 1); sig != nil {
		t.Fatal("expected nil after crossing (should be in CONFIRMING)")
	}
	if eng.state != stateConfirming {
		t.Fatalf("expected stateConfirming, got %d", eng.state)
	}

	// 确认: NO 从 0.15 涨到 0.22 → other_delta=+0.07 > 0.05 → +3
	// score = 3 + 2(B2) = 5 ≥ 2 → 信号
	confSnap := makeTestSnap(0.78, 0.22, 50005, openPrice, 225)
	sig := eng.ProcessSnapshot(confSnap, 1)
	if sig == nil {
		t.Fatal("expected signal after confirmation")
	}
	if sig.Side != "yes" {
		t.Errorf("expected side=yes, got %s", sig.Side)
	}
	if sig.Score != 5 {
		t.Errorf("expected score 5 (B3+3 + B2+2), got %d", sig.Score)
	}
	// FillPrice = 确认时刻对侧 ASK = 1 - YES bid(0.78) ≈ 0.22（回测 ask 口径）
	if diff := sig.FillPrice - 0.22; diff > 1e-9 || diff < -1e-9 {
		t.Errorf("expected fill_price ~0.22 (ask at confirm), got %.4f", sig.FillPrice)
	}
	if sig.BtcDivergence < 0.05 {
		t.Errorf("expected btc_divergence >= 0.05, got %.4f", sig.BtcDivergence)
	}
	if sig.OtherDelta != 0.07 {
		t.Errorf("other_delta expected 0.07, got %.4f", sig.OtherDelta)
	}
	if sig.Shares != 0 {
		t.Errorf("expected 0 shares (caller sets it), got %.0f", sig.Shares)
	}
}

func TestEngine_NoScoreWithoutCoreSignals(t *testing.T) {
	// od 无贡献且 B2 不成立 → score 0 < 2 → 无信号，回到 Watching
	cfg := DefaultConfig()
	cfg.MinPreSnaps = 2
	cfg.ConfirmDelayTicks = 1
	eng := NewEngine(cfg, readyHist())
	eng.Reset(1)

	// BTC 下行（YES 侧背离），穿越时 range_exp = 40/40 = 1.0 ≥ 1.0 → B2 不参与
	openPrice := 50000.0
	for i := 0; i < 5; i++ {
		price := openPrice - float64(i)*8
		snap := makeTestSnap(0.5, 0.3, price, openPrice, 250-i*5)
		eng.ProcessSnapshot(snap, 1)
	}

	// 穿越: YES=0.75/NO=0.20 at 49960（div=+1.0 ✓, range=1.0 → 无 B2）
	if sig := eng.ProcessSnapshot(makeTestSnap(0.75, 0.20, 49960, openPrice, 230), 1); sig != nil {
		t.Fatal("expected nil after crossing")
	}

	// 确认: 对侧回落 other_delta=-0.03 → B3 无贡献 → score=0 < 2 → 无信号
	sig := eng.ProcessSnapshot(makeTestSnap(0.78, 0.17, 49960, openPrice, 225), 1)
	if sig != nil {
		t.Errorf("expected nil (no core signal), got score=%d", sig.Score)
	}
	if eng.state != stateWatching {
		t.Errorf("expected stateWatching after score fail, got %d", eng.state)
	}
}

func TestEngine_DivergenceVeto(t *testing.T) {
	// B1: BTC 与 PM 同向的穿越必须被否决（YES 侧触发 + BTC 上涨 = 同向）
	cfg := DefaultConfig()
	cfg.MinPreSnaps = 2
	cfg.ConfirmDelayTicks = 1
	eng := NewEngine(cfg, readyHist())
	eng.Reset(1)

	// BTC 上行（与 YES 同向）
	openPrice := 50000.0
	for i := 0; i < 5; i++ {
		price := openPrice + float64(i)*5
		snap := makeTestSnap(0.5, 0.3, price, openPrice, 250-i*5)
		eng.ProcessSnapshot(snap, 1)
	}

	// 穿越: YES>0.7 且 BTC=50030（高于 open → btc_pos=+0.75 → div=-0.75 < floor 0）
	if sig := eng.ProcessSnapshot(makeTestSnap(0.75, 0.15, 50030, openPrice, 230), 1); sig != nil {
		t.Fatal("expected B1 veto at crossing (BTC aligned with PM)")
	}
	if eng.state != stateWatching {
		t.Errorf("expected stateWatching after B1 veto, got %d", eng.state)
	}

	// BTC 回落到 open 之下 → 再穿越则背离成立，应进入 Confirming
	eng.ProcessSnapshot(makeTestSnap(0.6, 0.4, 49980, openPrice, 225), 1)
	if sig := eng.ProcessSnapshot(makeTestSnap(0.76, 0.30, 49975, openPrice, 220), 1); sig != nil {
		t.Fatal("expected nil after crossing (in confirming)")
	}
	if eng.state != stateConfirming {
		t.Errorf("expected stateConfirming after diverging crossing, got %d", eng.state)
	}
}

func TestEngine_HistNotReadyVeto(t *testing.T) {
	// B1 依赖历史振幅；hist 未就绪时无法计算背离度 → 否决
	// （与 Python 回测一致：无 hist_avg_range 的事件整体跳过）
	cfg := DefaultConfig()
	cfg.MinPreSnaps = 2
	eng := NewEngine(cfg, NewHistRangeTracker(18))
	eng.Reset(1)

	for i := 0; i < 5; i++ {
		snap := makeTestSnap(0.5, 0.3, 50000-float64(i), 50000, 250-i*5)
		eng.ProcessSnapshot(snap, 1)
	}
	if sig := eng.ProcessSnapshot(makeTestSnap(0.75, 0.15, 49995, 50000, 230), 1); sig != nil {
		t.Fatal("expected veto when hist not ready (B1 unavailable)")
	}
	if eng.state != stateWatching {
		t.Errorf("expected stateWatching, got %d", eng.state)
	}
}

func TestEngine_MaxEntryPriceVeto(t *testing.T) {
	// 确认时刻对侧 ASK > max_entry_price → 信号无效（盈亏比已恶化）
	// ASK 口径：对侧 ask = 1 - 触发侧 bid。本用例确认时刻 YES bid=0.60
	// → NO ask=0.40 > 0.35 → 否决。旧口径看 NO bid=0.30 ≤ 0.35 会放行。
	cfg := DefaultConfig()
	cfg.MaxEntryPrice = 0.35
	cfg.MinPreSnaps = 2
	cfg.ConfirmDelayTicks = 1
	eng := NewEngine(cfg, readyHist())
	eng.Reset(1)

	openPrice := 50010.0
	for i := 0; i < 5; i++ {
		snap := makeTestSnap(0.5, 0.3, openPrice-float64(i), openPrice, 250-i*5)
		if sig := eng.ProcessSnapshot(snap, 1); sig != nil {
			t.Fatalf("unexpected signal at snap %d", i)
		}
	}

	// 穿越: YES>0.7，对面 NO=0.15，BTC 低于 open（背离 ✓）
	if sig := eng.ProcessSnapshot(makeTestSnap(0.75, 0.15, 50005, openPrice, 230), 1); sig != nil {
		t.Fatal("expected nil after crossing")
	}

	// 确认: YES bid 回落到 0.60 → NO ask = 0.40 > 0.35 → 价格失效 gate，无信号
	// （NO bid 仅 0.30，旧口径会放行；若无 gate，该信号将得 5 分并发出）
	if sig := eng.ProcessSnapshot(makeTestSnap(0.60, 0.30, 50005, openPrice, 225), 1); sig != nil {
		t.Fatalf("expected nil after confirm (ask price gate), got signal side=%s", sig.Side)
	}
}

func TestEngine_MaxEntryPriceAskPass(t *testing.T) {
	// ASK 口径放行用例：对侧 ask ≤ max_entry_price 但对侧 bid > max_entry_price
	// 时，新口径必须放行（旧口径按 bid 判 gate 会误杀）。
	// 确认时刻 YES bid=0.75 → NO ask=0.25 ≤ 0.35 放行；NO bid=0.36 > 0.35。
	cfg := DefaultConfig()
	cfg.MaxEntryPrice = 0.35
	cfg.MinPreSnaps = 2
	cfg.ConfirmDelayTicks = 1
	eng := NewEngine(cfg, readyHist())
	eng.Reset(1)

	openPrice := 50010.0
	for i := 0; i < 5; i++ {
		snap := makeTestSnap(0.5, 0.3, openPrice-float64(i), openPrice, 250-i*5)
		if sig := eng.ProcessSnapshot(snap, 1); sig != nil {
			t.Fatalf("unexpected signal at snap %d", i)
		}
	}

	// 穿越: YES>0.7，对面 NO=0.15，BTC 低于 open（背离 ✓）
	if sig := eng.ProcessSnapshot(makeTestSnap(0.75, 0.15, 50005, openPrice, 230), 1); sig != nil {
		t.Fatal("expected nil after crossing")
	}

	// 确认: YES bid 0.75 → NO ask = 0.25 ≤ 0.35，gate 放行 → 信号发出
	sig := eng.ProcessSnapshot(makeTestSnap(0.75, 0.36, 50005, openPrice, 225), 1)
	if sig == nil {
		t.Fatal("expected signal (ask 0.25 within limit), got nil")
	}
}

func TestEngine_MaxEntryPriceDisabled(t *testing.T) {
	// max_entry_price=0 → gate 禁用，确认后正常产生信号
	cfg := DefaultConfig()
	cfg.MaxEntryPrice = 0
	cfg.MinPreSnaps = 2
	cfg.ConfirmDelayTicks = 1
	eng := NewEngine(cfg, readyHist())
	eng.Reset(1)

	openPrice := 50010.0
	for i := 0; i < 5; i++ {
		eng.ProcessSnapshot(makeTestSnap(0.5, 0.3, openPrice-float64(i), openPrice, 250-i*5), 1)
	}
	eng.ProcessSnapshot(makeTestSnap(0.75, 0.15, 50005, openPrice, 230), 1)
	sig := eng.ProcessSnapshot(makeTestSnap(0.78, 0.40, 50005, openPrice, 225), 1)
	if sig == nil {
		t.Fatal("expected signal when max_entry_price disabled")
	}
}

func TestEngine_F0Veto_RealBreakout(t *testing.T) {
	cfg := DefaultConfig()
	cfg.MinPreSnaps = 2
	ht := NewHistRangeTracker(18)
	ht.AddRange(100, 120) // range=20
	ht.AddRange(100, 130) // range=30
	ht.AddRange(100, 140) // range=40 → avg=30
	eng := NewEngine(cfg, ht)
	eng.Reset(1)

	// Feed snapshots with big BTC move: 50000 → 50100（$100 = 3.3x hist range）
	for i := 0; i < 5; i++ {
		price := 50000.0 + float64(i)*25
		snap := makeTestSnap(0.6, 0.3, price, 50000, 250-i*5)
		eng.ProcessSnapshot(snap, 1)
	}

	// Crossing with big BTC move → F0 veto
	if sig := eng.ProcessSnapshot(makeTestSnap(0.75, 0.20, 50100, 50000, 230), 1); sig != nil {
		t.Error("expected F0 veto for range_expansion >= 1.5")
	}
	if eng.state != stateWatching {
		t.Errorf("expected stateWatching after F0 veto, got %d", eng.state)
	}
}

func TestEngine_OneSignalPerCycle(t *testing.T) {
	cfg := DefaultConfig()
	cfg.MinPreSnaps = 2
	cfg.ConfirmDelayTicks = 1
	eng := NewEngine(cfg, readyHist())
	eng.Reset(1)

	// Feed pre-cross（BTC 下行，YES 侧背离）
	for i := 0; i < 5; i++ {
		snap := makeTestSnap(0.3, 0.3, 50000-float64(i)*5, 50000, 250-i*5)
		eng.ProcessSnapshot(snap, 1)
	}

	// 穿越 + 确认（od=+0.05 → +2；range=25/40=0.625 < 1.0 → B2 +2；score=4 ≥2 → 信号）
	eng.ProcessSnapshot(makeTestSnap(0.75, 0.15, 49975, 50000, 230), 1)
	sig1 := eng.ProcessSnapshot(makeTestSnap(0.78, 0.20, 49975, 50000, 225), 1)
	if sig1 == nil {
		t.Fatal("expected first signal")
	}

	// 后续 snapshot 不再触发（doneThisGen）
	if sig2 := eng.ProcessSnapshot(makeTestSnap(0.80, 0.18, 49975, 50000, 220), 1); sig2 != nil {
		t.Error("expected nil after doneThisGen")
	}
}

func TestEngine_ResetForNewCycle(t *testing.T) {
	cfg := DefaultConfig()
	cfg.MinPreSnaps = 2
	eng := NewEngine(cfg, readyHist())
	eng.Reset(1)

	// Complete a cycle
	for i := 0; i < 5; i++ {
		snap := makeTestSnap(0.3, 0.3, 50000, 50000, 250-i*5)
		eng.ProcessSnapshot(snap, 1)
	}
	eng.ProcessSnapshot(makeTestSnap(0.75, 0.15, 49990, 50000, 230), 1)
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
	// crossings at remaining_sec >= MaxRemainingSec are invalid
	// (window too early, BTC path too short)
	cfg := DefaultConfig()
	eng := NewEngine(cfg, readyHist())
	eng.Reset(1)

	// Early crossing at rem=270 (>=260) — should NOT set wasAbove
	eng.ProcessSnapshot(makeTestSnap(0.75, 0.30, 50000, 50000, 270), 1)
	if eng.yesWasAbove {
		t.Errorf("early YES crossing (rem=270 >= %d) should not set yesWasAbove", cfg.MaxRemainingSec)
	}

	// Early NO crossing also skipped
	eng.ProcessSnapshot(makeTestSnap(0.30, 0.75, 50000, 50000, 265), 1)
	if eng.noWasAbove {
		t.Errorf("early NO crossing (rem=265 >= %d) should not set noWasAbove", cfg.MaxRemainingSec)
	}

	// Valid crossing at rem=250 — SHOULD be detected as rising edge
	eng.ProcessSnapshot(makeTestSnap(0.80, 0.20, 50010, 50000, 250), 1)
	if !eng.yesWasAbove {
		t.Error("valid YES crossing (rem=250 < 260) should set yesWasAbove")
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

// ═══════════════════════════════════════════════════════════════
// Fallback path tests（多穿越重试）
// ═══════════════════════════════════════════════════════════════

func TestEngine_YESFailedFallbackToNO(t *testing.T) {
	cfg := DefaultConfig()
	cfg.MinPreSnaps = 2
	cfg.ConfirmDelayTicks = 1
	cfg.RangeExpMax = 2.0 // 放宽测试用（默认 1.5 会在 btc_pos=1.25 附近误触发）
	eng := NewEngine(cfg, readyHist())
	eng.Reset(1)

	// Feed pre-cross snapshots（BTC 下行 → YES 侧背离）
	openPrice := 50000.0
	for i := 0; i < 5; i++ {
		price := openPrice - float64(i)*10
		snap := makeTestSnap(0.3, 0.3, price, openPrice, 250-i*5)
		eng.ProcessSnapshot(snap, 1)
	}

	// YES 穿越: YES=0.75/NO=0.30，BTC=49950（div=+1.25 ✓, range=1.25 → 无 B2）
	eng.ProcessSnapshot(makeTestSnap(0.75, 0.30, 49950, openPrice, 230), 1)
	if eng.state != stateConfirming {
		t.Fatalf("expected stateConfirming for YES crossing, got %d", eng.state)
	}

	// 确认: NO 从 0.30 跌到 0.25 → od=-0.05 → score=0 → YES 失败
	sig := eng.ProcessSnapshot(makeTestSnap(0.78, 0.25, 49950, openPrice, 225), 1)
	if sig != nil {
		t.Fatal("YES should fail without core signal")
	}
	if eng.state != stateWatching {
		t.Fatalf("expected stateWatching after YES fail, got %d", eng.state)
	}

	// NO 穿越: NO=0.75/YES=0.15，BTC 反弹到 50020（div=+0.5 ✓, range=0.5 < 1.0 → B2 +2）
	eng.ProcessSnapshot(makeTestSnap(0.15, 0.75, 50020, openPrice, 220), 1)
	if eng.state != stateConfirming {
		t.Fatalf("expected stateConfirming for NO crossing, got %d", eng.state)
	}

	// 确认: YES 0.15 → 0.22 → od=+0.07 → +3 ≥2 → 信号
	sig = eng.ProcessSnapshot(makeTestSnap(0.22, 0.78, 50020, openPrice, 215), 1)
	if sig == nil {
		t.Fatal("expected signal from NO side after YES fallback")
	}
	if sig.Side != "no" {
		t.Errorf("expected side=no, got %s", sig.Side)
	}
}

func TestEngine_BothSidesFailResumeWatching(t *testing.T) {
	cfg := DefaultConfig()
	cfg.MinPreSnaps = 2
	cfg.ConfirmDelayTicks = 1
	cfg.RangeExpMax = 2.0
	eng := NewEngine(cfg, readyHist())
	eng.Reset(1)

	openPrice := 50000.0
	for i := 0; i < 5; i++ {
		price := openPrice - float64(i)*10
		snap := makeTestSnap(0.3, 0.3, price, openPrice, 250-i*5)
		eng.ProcessSnapshot(snap, 1)
	}

	// YES 穿越（背离 ✓）→ 确认失败
	eng.ProcessSnapshot(makeTestSnap(0.75, 0.30, 49950, openPrice, 230), 1)
	eng.ProcessSnapshot(makeTestSnap(0.78, 0.25, 49950, openPrice, 225), 1)
	if eng.state != stateWatching {
		t.Fatalf("expected stateWatching after YES fail, got %d", eng.state)
	}

	// NO 穿越（BTC 反弹到 50045，div=+1.125 ✓, range=1.125 ≥ 1.0 → 无 B2）→ 确认无核心信号
	eng.ProcessSnapshot(makeTestSnap(0.30, 0.75, 50045, openPrice, 220), 1)
	sig := eng.ProcessSnapshot(makeTestSnap(0.30, 0.75, 50045, openPrice, 215), 1)
	if sig != nil {
		t.Fatal("expected nil when both sides fail")
	}
	if eng.state != stateWatching {
		t.Fatalf("expected stateWatching after both sides fail, got %d", eng.state)
	}
}

func TestEngine_PendingCrossingEvaluatedAfterConfirm(t *testing.T) {
	// 盲窗期间记录的穿越（pendingCrossings）在确认数据到齐后被评估。
	// 时序: YES 穿越 → 盲窗内 NO 穿越（记入 pending）→ YES 确认失败（score=0）
	// → NO 回落到 0.7 以下（fallback 无对象）→ 下一 tick NO 确认数据到齐
	// → pending 队列同步评估 → od 达标 → 信号。
	cfg := DefaultConfig()
	cfg.MinPreSnaps = 2
	cfg.ConfirmDelayTicks = 2
	cfg.RangeExpMax = 2.0
	eng := NewEngine(cfg, readyHist())
	eng.Reset(1)

	openPrice := 50000.0
	for i := 0; i < 5; i++ {
		price := openPrice - float64(i)*10
		snap := makeTestSnap(0.3, 0.3, price, openPrice, 250-i*5)
		eng.ProcessSnapshot(snap, 1)
	}

	// snap5: YES 穿越（div=+1.25 ✓, range=1.25 → 无 B2）
	eng.ProcessSnapshot(makeTestSnap(0.75, 0.30, 49950, openPrice, 235), 1)

	// snap6: 盲窗内 NO 向上穿越（BTC 反弹到 50020 → div=+0.5 ✓）→ pending
	eng.ProcessSnapshot(makeTestSnap(0.20, 0.75, 50020, openPrice, 230), 1)

	// snap7: YES 确认 tick — NO 回落到 0.30（od=0 → score=0）→ YES 失败
	// 此时 NO=0.30 < 0.7，fallback 无对象 → 回到 Watching（pending 保留）
	eng.ProcessSnapshot(makeTestSnap(0.60, 0.30, 49950, openPrice, 225), 1)
	if eng.state != stateWatching {
		t.Fatalf("expected stateWatching after YES fail, got %d", eng.state)
	}

	// snap8: pending NO 穿越的确认数据到齐 → Watching 状态优先评估
	// od = 0.28-0.20 = +0.08 > 0.05 → +3 ≥2 → 信号
	sig := eng.ProcessSnapshot(makeTestSnap(0.28, 0.75, 50020, openPrice, 220), 1)
	if sig == nil {
		t.Fatal("expected signal from pending NO crossing")
	}
	if sig.Side != "no" {
		t.Errorf("expected side=no, got %s", sig.Side)
	}
}

func TestEngine_MinPreSnapsNotEnough_RetriesOnNextTick(t *testing.T) {
	// 首个穿越前置数据不足时引擎回到 Watching；后续穿越仍会重试。
	cfg := DefaultConfig()
	cfg.MinPreSnaps = 5
	cfg.RangeExpMax = 2.0
	eng := NewEngine(cfg, readyHist())
	eng.Reset(1)

	openPrice := 50000.0

	// 仅 3 个前置 snapshot（nPre=4 < MinPreSnaps=5）；BTC 下行（YES 侧背离）
	for i := 0; i < 3; i++ {
		price := openPrice - float64(i)*10
		snap := makeTestSnap(0.5, 0.3, price, openPrice, 250-i*5)
		eng.ProcessSnapshot(snap, 1)
	}

	// 早期穿越 at snapIdx=3 → nPre=4 < 5 → 忽略
	if sig := eng.ProcessSnapshot(makeTestSnap(0.75, 0.20, 49970, openPrice, 235), 1); sig != nil {
		t.Error("expected nil when MinPreSnaps not met")
	}
	if eng.state != stateWatching {
		t.Errorf("expected stateWatching after MinPreSnaps fail, got %d", eng.state)
	}

	// 补充前置数据后再次穿越 → 应进入 Confirming
	for i := 0; i < 3; i++ {
		price := openPrice - float64(i+3)*10
		snap := makeTestSnap(0.6, 0.3, price, openPrice, 220-i*5)
		eng.ProcessSnapshot(snap, 1)
	}
	// BTC 继续下行至 49940（div=+1.5 ✓, range=1.5 < RangeExpMax=2.0 → 无 B2）
	eng.ProcessSnapshot(makeTestSnap(0.80, 0.30, 49940, openPrice, 215), 1)
	if eng.state != stateConfirming {
		t.Errorf("expected stateConfirming on retry crossing, got %d", eng.state)
	}
}
