package flip

import "math"

// ═══════════════════════════════════════════════════════════════
// 特征提取 —— 纯函数，与 backtest_flip_utils.py 对应
// ═══════════════════════════════════════════════════════════════

// PathEfficiency 计算价格从开盘到当前位置的路径效率。
// §2.2: path_eff = |lastPrice - openPrice| / (max(prices) - min(prices))
//   ~1.0 = 单边趋势（价格直走，无回调）
//   ~0.0 = 来回振荡（价格往返，净位移小）
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

// TotalPath 计算 tick 级累计路径长度。
// §2.3 辅助函数: Σ|p[i] - p[i-1]|
func TotalPath(prePrices []float64) float64 {
	var total float64
	for i := 1; i < len(prePrices); i++ {
		total += math.Abs(prePrices[i] - prePrices[i-1])
	}
	return total
}

// NoiseRatio 衡量价格运动的噪声程度。
// §2.3: noise_ratio = total_path / net_move
// 越高越振荡。振荡行情通常 >5，趋势行情通常 <3。
func NoiseRatio(prePrices []float64, netMove float64) float64 {
	totalPath := TotalPath(prePrices)
	if netMove == 0 {
		return totalPath // pure oscillation, net displacement = 0
	}
	return totalPath / netMove
}

// CountFlips 统计价格序列中的方向切换次数。
// §2.4: 忽略平盘 tick（d==0）。
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

// IsOscillating 判断价格是否处于振荡状态（三条件同时满足）。
// §2.5: path_eff ≤ 阈值 AND noise_ratio > 阈值 AND flips > 阈值。
func IsOscillating(pathEff, noiseRatio float64, flips int, cfg FlipConfig) bool {
	return pathEff <= cfg.PathEffOscillating &&
		noiseRatio > cfg.NoiseRatioOscillating &&
		flips > cfg.FlipsOscillating
}

// RangeExpansion 衡量 BTC 位移相对于历史平均振幅的倍数。
// §2.6: range_expansion = |price - openPrice| / histAvgRange
// Tick 无关 —— 适用于任意采样频率。
func RangeExpansion(price, openPrice, histAvgRange float64) float64 {
	if histAvgRange == 0 {
		return 0
	}
	return math.Abs(price-openPrice) / histAvgRange
}

// BTCPosition 以历史平均振幅为单位，表达 BTC 相对开盘价的位移。
// §2.8: btc_position = (price - openPrice) / histAvgRange
// +1.0 = BTC 涨 1 倍历史振幅，-1.0 = BTC 跌 1 倍历史振幅。
func BTCPosition(price, openPrice, histAvgRange float64) float64 {
	if histAvgRange == 0 {
		return 0
	}
	return (price - openPrice) / histAvgRange
}
