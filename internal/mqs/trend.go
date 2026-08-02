package mqs

import (
	"math"

	"github.com/necklace/lasttrading/internal/snapshot"
)

// ComputeTrendScore calculates the Trend Score (§3).
// Weight: 30%
// Sub-indicators: Efficiency Ratio (0.4), Trend R² (0.35), Direction Consistency (0.25)
func ComputeTrendScore(window []*snapshot.Snapshot) float64 {
	if len(window) < 2 {
		return 0
	}

	er := computeEfficiencyRatio(window)
	r2 := computeTrendR2(window)
	dc := computeDirectionConsistency(window)

	return er*0.4 + r2*0.35 + dc*0.25
}

// computeEfficiencyRatio calculates |price_now - price_30s_ago| / Σ|per-second price changes|
func computeEfficiencyRatio(window []*snapshot.Snapshot) float64 {
	n := len(window)
	if n < 2 {
		return 0
	}

	// Use up to 30 samples
	start := 0
	if n > 30 {
		start = n - 30
	}
	slice := window[start:]

	firstPrice := slice[0].Price
	lastPrice := slice[len(slice)-1].Price

	netMove := math.Abs(lastPrice - firstPrice)

	var totalPath float64
	for i := 1; i < len(slice); i++ {
		totalPath += math.Abs(slice[i].Price - slice[i-1].Price)
	}

	if totalPath == 0 {
		return 0
	}

	er := netMove / totalPath

	// Score mapping (§3.1)
	if er >= 0.7 {
		return 100
	} else if er >= 0.5 {
		return 70
	} else if er >= 0.3 {
		return 40
	}
	return 0
}

// computeTrendR2 performs linear regression price = a*time + b and returns R² score (§3.2)
func computeTrendR2(window []*snapshot.Snapshot) float64 {
	n := len(window)
	if n < 2 {
		return 0
	}

	// Use up to 30 samples
	start := 0
	if n > 30 {
		start = n - 30
	}
	slice := window[start:]
	m := len(slice)

	// x: index 0..m-1, y: price
	var sumX, sumY, sumXY, sumX2, sumY2 float64
	for i, s := range slice {
		x := float64(i)
		y := s.Price
		sumX += x
		sumY += y
		sumXY += x * y
		sumX2 += x * x
		sumY2 += y * y
	}

	denom := float64(m)*sumX2 - sumX*sumX
	if denom == 0 {
		return 0
	}

	// Pearson correlation coefficient
	num := float64(m)*sumXY - sumX*sumY
	r := num / math.Sqrt(denom*(float64(m)*sumY2-sumY*sumY))

	r2 := r * r

	// Score mapping (§3.2)
	if r2 > 0.8 {
		return 100
	} else if r2 >= 0.6 {
		return 70
	} else if r2 >= 0.4 {
		return 40
	}
	return 0
}

// computeDirectionConsistency counts positive vs total 1-second returns (§3.3)
func computeDirectionConsistency(window []*snapshot.Snapshot) float64 {
	n := len(window)
	if n < 2 {
		return 0
	}

	// Use up to 30 samples
	start := 0
	if n > 30 {
		start = n - 30
	}
	slice := window[start:]

	positive := 0
	total := 0
	for i := 1; i < len(slice); i++ {
		total++
		if slice[i].Price >= slice[i-1].Price {
			positive++
		}
	}

	if total == 0 {
		return 0
	}

	ratio := float64(positive) / float64(total) * 100

	// Score mapping (§3.3)
	if ratio > 75 {
		return 100
	} else if ratio >= 60 {
		return 70
	} else if ratio >= 50 {
		return 40
	}
	return 0
}
