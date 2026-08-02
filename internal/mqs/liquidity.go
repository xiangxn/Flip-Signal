package mqs

import (
	"math"

	"github.com/necklace/lasttrading/internal/snapshot"
)

// ComputeLiquidityScore calculates the Liquidity Score (§7).
// Weight: 10%.
// Sub-indicators: Depth Imbalance (0.4), Depth Stability (0.4), Spread (0.2)
func ComputeLiquidityScore(window []*snapshot.Snapshot) float64 {
	if len(window) < 2 {
		return 50
	}

	imbalance := computeDepthImbalance(window)
	stability := computeDepthStability(window)
	spread := computeSpread(window)

	return imbalance*0.4 + stability*0.4 + spread*0.2
}

// computeDepthImbalance calculates Bid / (Bid + Ask) (§7.1)
// Best when close to 0.5 (balanced).
func computeDepthImbalance(window []*snapshot.Snapshot) float64 {
	last := window[len(window)-1]

	totalBid := last.BidDepth5
	totalAsk := last.AskDepth5
	total := totalBid + totalAsk

	if total == 0 {
		return 50
	}

	ratio := totalBid / total

	// Score: best near 0.5
	deviation := math.Abs(ratio - 0.5)
	if deviation <= 0.1 {
		return 100
	} else if deviation <= 0.2 {
		return 50
	}
	return 0
}

// computeDepthStability compares current depth vs 10s ago depth change (§7.2)
// PRD did not fully specify — we use: change <20% → 100, 20-50% → 50, >50% → 0
func computeDepthStability(window []*snapshot.Snapshot) float64 {
	n := len(window)
	last := window[n-1]

	// Find snapshot from 10s ago
	prevIdx := n - 11
	if prevIdx < 0 {
		prevIdx = 0
	}
	prev := window[prevIdx]

	curDepth := last.BidDepth5 + last.AskDepth5
	prevDepth := prev.BidDepth5 + prev.AskDepth5

	if prevDepth == 0 {
		if curDepth == 0 {
			return 100
		}
		return 50
	}

	change := math.Abs(curDepth-prevDepth) / prevDepth * 100

	// Score mapping
	if change < 20 {
		return 100
	} else if change <= 50 {
		return 50
	}
	return 0
}

// computeSpread calculates polymarket YES/NO spread (§7.3)
// Spread = YesPrice + NoPrice - 1. Smaller is better.
func computeSpread(window []*snapshot.Snapshot) float64 {
	last := window[len(window)-1]

	if last.YesPrice <= 0 || last.NoPrice <= 0 {
		return 50
	}

	spread := math.Abs(last.YesPrice + last.NoPrice - 1)

	// Score mapping
	if spread < 0.02 {
		return 100
	} else if spread <= 0.05 {
		return 50
	}
	return 0
}
