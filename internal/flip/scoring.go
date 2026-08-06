package flip

// ═══════════════════════════════════════════════════════════════
// Composite scoring — §2.10 / backtest_flip_scoring.py check_signal
// ═══════════════════════════════════════════════════════════════

// ComputeFlipScore computes the 7-feature composite score.
// Returns (score, vetoed). vetoed=true means F0 veto (real breakout, don't trade).
//
// Max achievable score: 9 points (4+1+1+2+1). F4 is disabled (OPT#5).
//
//	F1: OtherDelta > 0.03 → +4
//	F2: OtherDelta > 0.01 → +2 (exclusive with F1)
//	F3: IsOscillating    → +1
//	F4: EntryPrice < 0.20 → +2 (DISABLED — OPT#5: EntryCheapStrong=0)
//	F5: EntryPrice < 0.25 → +1
//	F6: RangeExpansion < 0.5 → +2 (hist ready only)
//	F7: BTC divergence from PM → +1 (hist ready only, OPT#2: reversed)
//	F0: RangeExpansion ≥ 2.0 → veto   (hist ready only)
func ComputeFlipScore(params ScoreParams) (score int, vetoed bool) {
	// F0: Range expansion too large — real breakout, PM is right, don't bet against.
	if params.HistReady && params.RangeExpansion >= params.Cfg.RangeExpMax {
		return 0, true
	}

	score = 0

	// F1/F2: Opposite-side price change (T+5s confirmation)
	if params.OtherDelta > params.Cfg.OtherDeltaStrong {
		score += params.Cfg.WOtherD5Strong
	} else if params.OtherDelta > params.Cfg.OtherDeltaWeak {
		score += params.Cfg.WOtherD5Weak
	}

	// F3: Oscillation pattern (trend exhaustion)
	if params.IsOscillating {
		score += params.Cfg.WOscillating
	}

	// F4/F5: Cheap entry price (high payout ratio)
	if params.EntryPrice < params.Cfg.EntryCheapStrong {
		score += params.Cfg.WCheapEntryStr
	} else if params.EntryPrice < params.Cfg.EntryCheapWeak {
		score += params.Cfg.WCheapEntryWeak
	}

	// F6: BTC barely moved — PM overconfident, likely flip
	if params.HistReady && params.RangeExpansion < params.Cfg.RangeExpThreshold {
		score += params.Cfg.WRangeExpansion
	}

	// F7: BTC direction diverges from PM → flip edge (OPT#2: reversed)
	if params.HistReady {
		var extreme bool
		if params.Side == "yes" {
			// YES>0.7 (PM bullish), BTC slightly down → PM overreacting
			extreme = params.BTCPosition > params.Cfg.BTCPosMin && params.BTCPosition < 0
		} else {
			// NO>0.7 (PM bearish), BTC slightly up → PM overreacting
			extreme = params.BTCPosition > 0 && params.BTCPosition < params.Cfg.BTCPosMax
		}
		if extreme {
			score += params.Cfg.WBtcExtreme
		}
	}

	return score, false
}
