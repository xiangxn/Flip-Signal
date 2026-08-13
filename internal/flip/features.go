package flip

import "math"

// ═══════════════════════════════════════════════════════════════
// 特征提取 —— 纯函数，与 backtest_flip_utils.py 对应（Formula B）
// ═══════════════════════════════════════════════════════════════

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

// Divergence 计算 BTC 与 PM 的背离度（方向校正后）。
// 正 = BTC 与 PM 反向（flip edge）；负 = BTC 与 PM 同向。
// YES 侧触发（PM 看涨）取 -btcPosition（BTC 跌 = 背离）；
// NO 侧触发（PM 看跌）取 +btcPosition（BTC 涨 = 背离）。
func Divergence(side string, btcPosition float64) float64 {
	if side == "yes" {
		return -btcPosition
	}
	return btcPosition
}
