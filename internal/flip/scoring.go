package flip

// ═══════════════════════════════════════════════════════════════
// Composite scoring — §2.10 / backtest_flip_scoring.py check_signal
// ═══════════════════════════════════════════════════════════════

// ComputeFlipScore computes the multi-feature composite score (Formula A).
// Returns (score, vetoed). vetoed=true means F0 veto (real breakout, don't trade).
//
// Max achievable score: 3+2+1+2+1+2+1 = 12 (up from 9).
//
//	F1v: OtherDelta > 0.05 → +3 (Formula A: new top tier)
//	F1:  OtherDelta > 0.02 → +2 (Formula A: was 0.03 +4)
//	F2:  OtherDelta > 0.01 → +1 (Formula A: was +2)
//	F3:  IsOscillating     → +2 (Formula A: was +1, pe≤0.8 nr>1.5 flips>1)
//	F4:  EntryPrice < 0.20 → +1 (Formula A: re-activated, elif — no stacking)
//	F5:  EntryPrice < 0.25 → +1
//	F6:  RangeExpansion < 0.5 → +2 (hist ready only)
//	F7:  BTC diverges from PM → +1 (Formula A: widened, hist ready only)
//	F0:  RangeExpansion ≥ 2.0 → veto   (hist ready only)
func ComputeFlipScore(params ScoreParams) (score int, vetoed bool) {
	// F0: Range expansion too large — real breakout, PM is right, don't bet against.
	if params.HistReady && params.RangeExpansion >= params.Cfg.RangeExpMax {
		return 0, true
	}

	score = 0

	// F1v/F1/F2: Opposite-side price change (T+5s confirmation) — Formula A 3-tier
	if params.OtherDelta > params.Cfg.OtherDeltaVStrong {
		score += params.Cfg.WOtherD5VStrong
	} else if params.OtherDelta > params.Cfg.OtherDeltaStrong {
		score += params.Cfg.WOtherD5Strong
	} else if params.OtherDelta > params.Cfg.OtherDeltaWeak {
		score += params.Cfg.WOtherD5Weak
	}

	// F3: Oscillation pattern (trend exhaustion) — Formula A: +2
	if params.IsOscillating {
		score += params.Cfg.WOscillating
	}

	// F4/F5: Cheap entry price (high payout ratio) — elif, no stacking
	if params.EntryPrice < params.Cfg.EntryCheapStrong {
		score += params.Cfg.WCheapEntryStr
	} else if params.EntryPrice < params.Cfg.EntryCheapWeak {
		score += params.Cfg.WCheapEntryWeak
	}

	// F6: BTC barely moved — PM overconfident, likely flip
	if params.HistReady && params.RangeExpansion < params.Cfg.RangeExpThreshold {
		score += params.Cfg.WRangeExpansion
	}

	// F7: BTC direction diverges from PM → flip edge (Formula A: widened)
	if params.HistReady {
		var extreme bool
		if params.Side == "yes" {
			// YES>0.7 (PM bullish), BTC down → PM overreacting
			extreme = params.BTCPosition < params.Cfg.BTCPosMin // btc_pos < -0.1
		} else {
			// NO>0.7 (PM bearish), BTC up → PM overreacting
			extreme = params.BTCPosition > params.Cfg.BTCPosMax // btc_pos > 0.1
		}
		if extreme {
			score += params.Cfg.WBtcExtreme
		}
	}

	return score, false
}
