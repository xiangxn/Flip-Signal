package mqs

import (
	"math"

	"github.com/necklace/lasttrading/internal/snapshot"
)

// ComputeHealthScore calculates the Health Score (§5).
// Weight: 20%.
// Sub-indicators: Momentum Decay (0.4), Volume Support (0.3), Retracement (0.3)
func ComputeHealthScore(window []*snapshot.Snapshot) float64 {
	if len(window) < 20 {
		return 50 // neutral when insufficient data
	}

	momentum := computeMomentumDecay(window)
	volume := computeVolumeSupport(window)
	retracement := computeRetracement(window)

	return momentum*0.4 + volume*0.3 + retracement*0.3
}

// computeMomentumDecay compares last 10s momentum vs previous 10s momentum (§5.1)
func computeMomentumDecay(window []*snapshot.Snapshot) float64 {
	n := len(window)
	if n < 20 {
		return 50
	}

	// Last 10 seconds
	recent := window[n-10:]
	// Previous 10 seconds
	prev := window[n-20 : n-10]

	recentMomentum := calcMomentum(recent)
	prevMomentum := calcMomentum(prev)

	if prevMomentum == 0 {
		if recentMomentum == 0 {
			return 100 // no momentum either way = no decay
		}
		return 100 // momentum appeared = good
	}

	// For momentum: positive momentum is bullish, negative is bearish
	// Decay means the absolute momentum is decreasing
	if math.Abs(prevMomentum) == 0 {
		return 50
	}

	decay := (math.Abs(prevMomentum) - math.Abs(recentMomentum)) / math.Abs(prevMomentum) * 100

	// Score mapping (§5.1): decay < 30% means momentum sustained
	if decay < 30 {
		return 100
	} else if decay <= 60 {
		return 60
	}
	return 0
}

// calcMomentum calculates the total price change over a slice.
func calcMomentum(slice []*snapshot.Snapshot) float64 {
	if len(slice) < 2 {
		return 0
	}
	first := slice[0].Price
	last := slice[len(slice)-1].Price
	if first == 0 {
		return 0
	}
	return (last - first) / first
}

// computeVolumeSupport compares Volume_last10 / Volume_prev10 (§5.2)
func computeVolumeSupport(window []*snapshot.Snapshot) float64 {
	n := len(window)
	if n < 20 {
		return 50
	}

	// Sum Buy+Sell volume for last 10 seconds
	recentVol := sumVolume(window[n-10:])
	prevVol := sumVolume(window[n-20 : n-10])

	if prevVol == 0 {
		if recentVol > 0 {
			return 100
		}
		return 60
	}

	ratio := recentVol / prevVol

	// Score mapping (§5.2)
	if ratio > 1.2 {
		return 100
	} else if ratio >= 0.8 {
		return 60
	}
	return 0
}

func sumVolume(slice []*snapshot.Snapshot) float64 {
	var total float64
	for _, s := range slice {
		total += s.BuyVolume1s + s.SellVolume1s
	}
	return total
}

// computeRetracement calculates (high - price) / (high - open) for uptrends (§5.3)
func computeRetracement(window []*snapshot.Snapshot) float64 {
	n := len(window)
	if n < 2 {
		return 50
	}

	last := window[n-1]
	open := window[0].OpenPrice
	if open == 0 {
		return 50
	}

	// Find high from open price
	high := open
	for _, s := range window {
		if s.Price > high {
			high = s.Price
		}
	}

	highFromOpen := high - open
	if highFromOpen <= 0 {
		// No uptrend or flat/down —— no retracement concern
		return 100
	}

	retracement := (high - last.Price) / highFromOpen * 100

	// Score mapping (§5.3)
	if retracement < 30 {
		return 100
	} else if retracement <= 60 {
		return 50
	}
	return 0
}
