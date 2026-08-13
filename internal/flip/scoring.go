package flip

// ═══════════════════════════════════════════════════════════════
// 复合评分 —— Formula B 精简公式（docs/flip_strategy_plan_2026-08-13.md）
// ═══════════════════════════════════════════════════════════════

// ComputeFlipScore 计算 Formula B 复合评分。
// 返回 (score, vetoed)。vetoed=true 表示否决（不下注）。
//
// 理论最高分: 3 + 2 = 5。
//
//	L0:  OrderBookLatency > MaxLatencyMs → veto（延迟风控，数据不可信）
//	F0:  RangeExpansion ≥ RangeExpMax → veto（真突破，PM 是对的）
//	B3:  OtherDelta > VStrong/Strong/Weak → +3/+2/+1（确认期对侧回归）
//	B2:  RangeExpansion < RangeExpThreshold → +2（BTC 没动 = PM 过度自信）
//
// B1 背离硬要求（DivergenceFloor 否决同向 + MinDivergence 可选强度）
// 由引擎在评分前硬过滤，不参与评分。
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

	// B3: 对面价格变化（T+N 确认）—— 三档评分
	if params.OtherDelta > params.Cfg.OtherDeltaVStrong {
		score += params.Cfg.WOtherD5VStrong
	} else if params.OtherDelta > params.Cfg.OtherDeltaStrong {
		score += params.Cfg.WOtherD5Strong
	} else if params.OtherDelta > params.Cfg.OtherDeltaWeak {
		score += params.Cfg.WOtherD5Weak
	}

	// B2: BTC 几乎未动 → PM 过度自信，翻转概率高
	if params.HistReady && params.RangeExpansion < params.Cfg.RangeExpThreshold {
		score += params.Cfg.WRangeExpansion
	}

	return score, false
}
