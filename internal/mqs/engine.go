package mqs

import (
	"github.com/necklace/lasttrading/internal/snapshot"
)

// Engine holds configuration for MQS computation.
type Engine struct {
	// No state needed currently — all computation is stateless given a window.
}

// NewEngine creates a new MQS Engine.
func NewEngine() *Engine {
	return &Engine{}
}

// ComputeMQS calculates the full MarketQuality from a snapshot window.
// The window should contain at least 30 seconds of data for reliable scores.
// Newest snapshot should be the last element.
func (e *Engine) ComputeMQS(window []*snapshot.Snapshot) MarketQuality {
	if len(window) == 0 {
		return MarketQuality{}
	}

	trend := ComputeTrendScore(window)
	noise := ComputeNoiseScore(window)
	health := ComputeHealthScore(window)
	flow := ComputeFlowScore(window)
	liquidity := ComputeLiquidityScore(window)

	// Final weighted formula (§8):
	// Total = Trend×0.30 + (100-Noise)×0.25 + Health×0.20 + Flow×0.15 + Liquidity×0.10
	total := trend*0.30 +
		(100-noise)*0.25 +
		health*0.20 +
		flow*0.15 +
		liquidity*0.10

	return MarketQuality{
		TrendScore:     clamp(trend, 0, 100),
		NoiseScore:     clamp(noise, 0, 100),
		HealthScore:    clamp(health, 0, 100),
		FlowScore:      clamp(flow, 0, 100),
		LiquidityScore: clamp(liquidity, 0, 100),
		Total:          clamp(total, 0, 100),
	}
}

// ComputeMQS is a convenience function equivalent to NewEngine().ComputeMQS(window).
func ComputeMQS(window []*snapshot.Snapshot) MarketQuality {
	return NewEngine().ComputeMQS(window)
}
