package flip

// ═══════════════════════════════════════════════════════════════
// 复合评分 —— §2.10 / backtest_flip_scoring.py check_signal
// ═══════════════════════════════════════════════════════════════

// ComputeFlipScore 计算多特征复合评分（Formula A）。
// 返回 (score, vetoed)。vetoed=true 表示否决（不下注）。
//
// 理论最高分: 3+2+1+2+1+2+1 = 12（此前为 9）。
//
//	L0:  OrderBookLatency > MaxLatencyMs → veto（延迟风控，数据不可信）
//	F0:  RangeExpansion ≥ 2.0 → veto   (hist ready only, 真突破)
//	F1v: OtherDelta > 0.05 → +3 (Formula A: new top tier)
//	F1:  OtherDelta > 0.02 → +2 (Formula A: was 0.03 +4)
//	F2:  OtherDelta > 0.01 → +1 (Formula A: was +2)
//	F3:  IsOscillating     → +2 (Formula A: was +1, pe≤0.8 nr>1.5 flips>1)
//	F4:  EntryPrice < 0.20 → +1 (Formula A: re-activated, elif — no stacking)
//	F5:  EntryPrice < 0.25 → +1
//	F6:  RangeExpansion < 0.5 → +2 (hist ready only)
//	F7:  BTC diverges from PM → +1 (Formula A: widened, hist ready only)
func ComputeFlipScore(params ScoreParams) (score int, vetoed bool) {
	// L0: 订单簿延迟过大 → 数据不可信，一票否决。
	if params.Cfg.MaxLatencyMs > 0 && params.OrderBookLatency > params.Cfg.MaxLatencyMs {
		return 0, true
	}

	// F0: 振幅扩张过大 → 真突破，PM 判断正确，不宜反向下注。
	if params.HistReady && params.RangeExpansion >= params.Cfg.RangeExpMax {
		return 0, true
	}

	score = 0

	// F1v/F1/F2: 对面价格变化（T+5s 确认）—— Formula A 三级评分
	if params.OtherDelta > params.Cfg.OtherDeltaVStrong {
		score += params.Cfg.WOtherD5VStrong
	} else if params.OtherDelta > params.Cfg.OtherDeltaStrong {
		score += params.Cfg.WOtherD5Strong
	} else if params.OtherDelta > params.Cfg.OtherDeltaWeak {
		score += params.Cfg.WOtherD5Weak
	}

	// F3: 振荡形态（趋势衰竭）—— Formula A: +2
	if params.IsOscillating {
		score += params.Cfg.WOscillating
	}

	// F4/F5: 低价入场（高赔率）—— elif 互斥，不叠加
	if params.EntryPrice < params.Cfg.EntryCheapStrong {
		score += params.Cfg.WCheapEntryStr
	} else if params.EntryPrice < params.Cfg.EntryCheapWeak {
		score += params.Cfg.WCheapEntryWeak
	}

	// F6: BTC 几乎未动 → PM 过度自信，翻转概率高
	if params.HistReady && params.RangeExpansion < params.Cfg.RangeExpThreshold {
		score += params.Cfg.WRangeExpansion
	}

	// F7: BTC 方向与 PM 背离 → 翻转机会（Formula A: 放宽）
	// BTCExtreme 由引擎根据 side 特定阈值预先计算。
	if params.HistReady && params.BTCExtreme {
		score += params.Cfg.WBtcExtreme
	}

	return score, false
}
