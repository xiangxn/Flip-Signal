package mqs

import (
	"math"

	"github.com/necklace/lasttrading/internal/snapshot"
)

// ComputeNoiseScore calculates the Noise Score (§4).
//
// IMPORTANT (PRD §4):
//   - "这个分越高代表：越乱" — Higher NoiseScore = noisier market.
//   - The sub-indicator tables in the PRD score "quality" (100 = good condition).
//   - We internally compute noiseQuality (0-100, 100 = clean), then invert:
//     NoiseScore = 100 - noiseQuality
//   - The final MQS formula (§8) uses (100 - NoiseScore) = noiseQuality.
//
// Sub-indicators: Direction Flip (0.4), Path Noise Ratio (0.4), Volatility Stability (0.2)
func ComputeNoiseScore(window []*snapshot.Snapshot) float64 {
	if len(window) < 2 {
		return 50 // neutral when insufficient data
	}

	// Sub-indicators return quality scores: 100 = best condition, 0 = worst
	flipQuality := computeDirectionFlipQuality(window)
	pathQuality := computeReturnNoiseQuality(window)
	volQuality := computeVolatilityStabilityQuality(window)

	// noiseQuality: 100 = perfectly clean market
	noiseQuality := flipQuality*0.4 + pathQuality*0.4 + volQuality*0.2

	// Invert: NoiseScore = 100 - noiseQuality (100 = very noisy, 0 = clean)
	return 100 - noiseQuality
}

// computeDirectionFlipQuality scores direction flip count (§4.1).
// PRD table: 0-2 flips → 100, 3-5 → 50, >5 → 0
// This is a QUALITY score (fewer flips = better = higher score).
func computeDirectionFlipQuality(window []*snapshot.Snapshot) float64 {
	n := len(window)
	if n < 2 {
		return 50
	}

	start := 0
	if n > 30 {
		start = n - 30
	}
	slice := window[start:]

	flips := 0
	var prevDir int // 1=up, -1=down, 0=flat/unknown

	for i := 1; i < len(slice); i++ {
		diff := slice[i].Price - slice[i-1].Price
		var dir int
		if diff > 0 {
			dir = 1
		} else if diff < 0 {
			dir = -1
		} else {
			dir = prevDir
		}

		if prevDir != 0 && dir != 0 && dir != prevDir {
			flips++
		}
		if dir != 0 {
			prevDir = dir
		}
	}

	// PRD §4.1 — fewer flips = better
	if flips <= 2 {
		return 100
	} else if flips <= 5 {
		return 50
	}
	return 0
}

// computeReturnNoiseQuality scores path noise ratio (§4.2).
// PRD table: Noise <1.5 → 100, 1.5-3 → 60, >3 → 0
// This is a QUALITY score (lower noise ratio = better = higher score).
func computeReturnNoiseQuality(window []*snapshot.Snapshot) float64 {
	n := len(window)
	if n < 2 {
		return 50
	}

	start := 0
	if n > 30 {
		start = n - 30
	}
	slice := window[start:]

	firstPrice := slice[0].Price
	lastPrice := slice[len(slice)-1].Price

	netMove := math.Abs(lastPrice - firstPrice)

	var pathLen float64
	for i := 1; i < len(slice); i++ {
		pathLen += math.Abs(slice[i].Price - slice[i-1].Price)
	}

	if netMove == 0 {
		if pathLen == 0 {
			return 100 // No movement = clean
		}
		return 0 // Oscillated but ended flat = noisy
	}

	noiseRatio := pathLen / netMove

	// PRD §4.2 — lower ratio = better
	if noiseRatio < 1.5 {
		return 100
	} else if noiseRatio <= 3 {
		return 60
	}
	return 0
}

// computeVolatilityStabilityQuality scores volatility burst detection (§4.3).
// PRD table: Vol10s/Vol60s 0.8-1.5 → 100, 1.5-2 → 50, >2 → 0
// This is a QUALITY score (stable volatility = better = higher score).
func computeVolatilityStabilityQuality(window []*snapshot.Snapshot) float64 {
	n := len(window)
	if n < 10 {
		return 50
	}

	vol10 := computeVolatilityFromWindow(window, 10)
	vol60 := computeVolatilityFromWindow(window, 60)

	if vol60 == 0 {
		if vol10 == 0 {
			return 100 // No volatility = stable
		}
		return 0 // Volatility appeared = burst
	}

	ratio := vol10 / vol60

	// PRD §4.3 — ratio near 1.0 = stable = best
	if ratio >= 0.8 && ratio <= 1.5 {
		return 100
	} else if ratio <= 2 {
		return 50
	}
	return 0
}

// computeVolatilityFromWindow calculates realized volatility over a lookback in seconds.
func computeVolatilityFromWindow(window []*snapshot.Snapshot, lookbackSec int) float64 {
	n := len(window)
	start := 0
	if n > lookbackSec {
		start = n - lookbackSec
	}
	slice := window[start:]

	if len(slice) < 2 {
		return 0
	}

	returns := make([]float64, 0, len(slice)-1)
	for i := 1; i < len(slice); i++ {
		if slice[i-1].Price > 0 {
			r := (slice[i].Price - slice[i-1].Price) / slice[i-1].Price
			returns = append(returns, r)
		}
	}

	if len(returns) == 0 {
		return 0
	}

	var sum float64
	for _, r := range returns {
		sum += r
	}
	mean := sum / float64(len(returns))

	var sumSqDiff float64
	for _, r := range returns {
		diff := r - mean
		sumSqDiff += diff * diff
	}

	return math.Sqrt(sumSqDiff / float64(len(returns)))
}
