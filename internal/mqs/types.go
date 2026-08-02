package mqs

// MarketQuality contains the MQS scores for a single evaluation point.
// All scores are in range 0-100. Corresponds to PRD §2.
type MarketQuality struct {
	TrendScore     float64 `json:"trend"`     // 趋势质量
	NoiseScore     float64 `json:"noise"`     // 噪声（越高越乱）
	HealthScore    float64 `json:"health"`    // 趋势健康度
	FlowScore      float64 `json:"flow"`      // 订单流
	LiquidityScore float64 `json:"liquidity"` // 流动性
	Total          float64 `json:"total"`     // 加权综合评分 0-100
}

// score returns a score based on a threshold mapping.
// thresholds is a list of [value, score] pairs sorted descending by value.
// The first threshold where value >= threshold gets the corresponding score.
// Returns 0 if below all thresholds.
func thresholdScore(value float64, thresholds ...[2]float64) float64 {
	for _, t := range thresholds {
		if value >= t[0] {
			return t[1]
		}
	}
	return 0
}

// inverseScore is like thresholdScore but for metrics where lower is better.
func inverseThresholdScore(value float64, thresholds ...[2]float64) float64 {
	for _, t := range thresholds {
		if value <= t[0] {
			return t[1]
		}
	}
	return 0
}

// clamp ensures a value is within [min, max].
func clamp(v, min, max float64) float64 {
	if v < min {
		return min
	}
	if v > max {
		return max
	}
	return v
}
