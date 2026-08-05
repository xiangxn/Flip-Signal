package flip

import "math"

// ═══════════════════════════════════════════════════════════════
// Feature extraction — pure functions matching backtest_flip_utils.py
// ═══════════════════════════════════════════════════════════════

// PathEfficiency computes how directly price moved from open to current.
// §2.2: path_eff = abs(lastPrice - openPrice) / (max(prices) - min(prices))
//   ~1.0 = trending (price went straight, no pullback)
//   ~0.0 = oscillating (price went back and forth, small net move)
func PathEfficiency(prePrices []float64, openPrice float64) float64 {
	if len(prePrices) == 0 {
		return 0
	}
	lastPrice := prePrices[len(prePrices)-1]
	netMove := math.Abs(lastPrice - openPrice)

	preHigh := prePrices[0]
	preLow := prePrices[0]
	for _, p := range prePrices {
		if p > preHigh {
			preHigh = p
		}
		if p < preLow {
			preLow = p
		}
	}
	preRange := preHigh - preLow
	if preRange == 0 {
		return 0
	}
	return netMove / preRange
}

// TotalPath computes the cumulative tick-level path length.
// §2.3 helper: Σ|p[i] - p[i-1]|
func TotalPath(prePrices []float64) float64 {
	var total float64
	for i := 1; i < len(prePrices); i++ {
		total += math.Abs(prePrices[i] - prePrices[i-1])
	}
	return total
}

// NoiseRatio measures how choppy price movement is.
// §2.3: noise_ratio = total_path / net_move
// Higher = more oscillation. Oscillation typically >5, trending typically <3.
func NoiseRatio(prePrices []float64, netMove float64) float64 {
	totalPath := TotalPath(prePrices)
	if netMove == 0 {
		return totalPath // pure oscillation, net displacement = 0
	}
	return totalPath / netMove
}

// CountFlips counts direction changes in the price series.
// §2.4: ignores flat ticks (d==0).
func CountFlips(prePrices []float64) int {
	if len(prePrices) < 3 {
		return 0
	}
	flips := 0
	for i := 2; i < len(prePrices); i++ {
		d1 := prePrices[i-1] - prePrices[i-2]
		d2 := prePrices[i] - prePrices[i-1]
		if d1 != 0 && d2 != 0 && (d1 > 0) != (d2 > 0) {
			flips++
		}
	}
	return flips
}

// IsOscillating checks whether price action is oscillating (all three conditions).
// §2.5: path_eff ≤ threshold AND noise_ratio > threshold AND flips > threshold.
func IsOscillating(pathEff, noiseRatio float64, flips int, cfg FlipConfig) bool {
	return pathEff <= cfg.PathEffOscillating &&
		noiseRatio > cfg.NoiseRatioOscillating &&
		flips > cfg.FlipsOscillating
}

// RangeExpansion measures how far BTC has moved relative to historical average range.
// §2.6: range_expansion = abs(price - openPrice) / histAvgRange
// Tick-independent — works with any sampling frequency.
func RangeExpansion(price, openPrice, histAvgRange float64) float64 {
	if histAvgRange == 0 {
		return 0
	}
	return math.Abs(price-openPrice) / histAvgRange
}

// BTCPosition expresses BTC displacement from open in units of historical avg range.
// §2.8: btc_position = (price - openPrice) / histAvgRange
// +1.0 = BTC up 1x hist range, -1.0 = BTC down 1x hist range.
func BTCPosition(price, openPrice, histAvgRange float64) float64 {
	if histAvgRange == 0 {
		return 0
	}
	return (price - openPrice) / histAvgRange
}
