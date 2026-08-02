package mqs

import (
	"math"

	"github.com/necklace/lasttrading/internal/snapshot"
)

// ComputeFlowScore calculates the Flow Score (§6).
// Weight: 15%.
// Sub-indicators: Buy Ratio (0.5), Signed Flow Trend (0.5)
func ComputeFlowScore(window []*snapshot.Snapshot) float64 {
	if len(window) < 2 {
		return 0
	}

	buyRatio := computeBuyRatio(window)
	flowTrend := computeSignedFlowTrend(window)

	return buyRatio*0.5 + flowTrend*0.5
}

// computeBuyRatio calculates BuyVolume / (BuyVolume + SellVolume) over the window (§6.1)
func computeBuyRatio(window []*snapshot.Snapshot) float64 {
	// Use last 10 seconds
	n := len(window)
	start := 0
	if n > 10 {
		start = n - 10
	}
	slice := window[start:]

	var buySum, sellSum float64
	for _, s := range slice {
		buySum += s.BuyVolume1s
		sellSum += s.SellVolume1s
	}

	total := buySum + sellSum
	if total == 0 {
		return 40 // neutral
	}

	ratio := buySum / total * 100

	// Score mapping (§6.1)
	if ratio > 65 {
		return 100
	} else if ratio >= 55 {
		return 70
	} else if ratio >= 50 {
		return 40
	}
	return 0
}

// computeSignedFlowTrend checks direction consistency of SignedFlow over 30s (§6.2)
// PRD did not fully specify this — we use: if >80% same sign → 100, 60-80% → 60, else 0
func computeSignedFlowTrend(window []*snapshot.Snapshot) float64 {
	n := len(window)
	if n < 5 {
		return 50
	}

	// Use up to 30 samples
	start := 0
	if n > 30 {
		start = n - 30
	}
	slice := window[start:]

	positive := 0
	total := 0
	for _, s := range slice {
		total++
		if s.SignedFlow10s > 0 {
			positive++
		} else if math.Abs(s.SignedFlow10s) < 1e-9 {
			// Near-zero flow — treat as neutral, count as majority direction
			// Use whichever direction is currently dominant
			if positive > total/2 {
				positive++
			}
		}
	}

	if total == 0 {
		return 0
	}

	ratio := float64(positive) / float64(total) * 100
	// Normalize: we want the majority percentage (e.g., if 20% positive, 80% negative → 80%)
	if ratio < 50 {
		ratio = 100 - ratio
	}

	// Score mapping
	if ratio > 80 {
		return 100
	} else if ratio >= 60 {
		return 60
	}
	return 0
}
