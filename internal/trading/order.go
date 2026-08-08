package trading

import (
	"fmt"
	"math"

	"github.com/necklace/flip-signal/internal/flip"
)

// SignalToOrder 将 FlipSignal 映射为 GTC 订单参数（纯函数）。
//
// 方向映射：
//
//	side=="yes"（YES>0.7，过度看涨）→ 买入 NO token → tokenSide="no"（赌 DOWN）
//	side=="no" （NO>0.7，过度看跌）→ 买入 YES token → tokenSide="yes"（赌 UP）
//
// 返回：目标 tokenID、持仓方向、错误。
func SignalToOrder(sig *flip.FlipSignal, yesTokenID, noTokenID string) (tokenID string, tokenSide string, err error) {
	switch sig.Side {
	case "yes":
		if noTokenID == "" {
			return "", "", fmt.Errorf("NO token ID 为空")
		}
		return noTokenID, "no", nil
	case "no":
		if yesTokenID == "" {
			return "", "", fmt.Errorf("YES token ID 为空")
		}
		return yesTokenID, "yes", nil
	default:
		return "", "", fmt.Errorf("未知的信号方向: %s", sig.Side)
	}
}

// CalcMaxPrice 计算 GTC 限价单的限价（滑点保护）。
//
//	maxPrice = entryPrice × (1 + maxSlippage)
//
// 返回 NaN 时调用方应拒绝。
func CalcMaxPrice(entryPrice, maxSlippage float64) float64 {
	if entryPrice <= 0 || maxSlippage < 0 {
		return math.NaN()
	}
	return entryPrice * (1.0 + maxSlippage)
}
