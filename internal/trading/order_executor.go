package trading

import (
	"fmt"

	"github.com/xiangxn/go-polymarket-sdk/orders"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// ── 下单策略常量 ──

const (
	StrategyGTC = "GTC" // 挂单等待成交
	StrategyFAK = "FAK" // 即刻市价成交，剩余取消
)

// OrderBookToSummary 将 MarketMonitor WebSocket 返回的 *OrderBook
// 转为 CreateMarketOrder 所需的 *OrderBookSummary（仅复制 Bids/Asks，
// 这两个字段是 CalculateMarketPrice 唯一用到的）。
func OrderBookToSummary(ob *sdk.OrderBook) *sdk.OrderBookSummary {
	if ob == nil {
		return nil
	}
	return &sdk.OrderBookSummary{
		Bids: ob.Bids,
		Asks: ob.Asks,
	}
}

// ── 下单构建函数 ──

// BuildGtcOrder 创建 GTC 限价单（签名但不提交）。
//
// 按 maxPrice 计算精确 shares，订单挂在 CLOB 订单簿上等待对手方成交。
// 返回已签名订单和精确股数。
func BuildGtcOrder(client TradeClient, tokenID string, maxPrice, stakeUSDC float64) (*orders.SignedOrder, float64, error) {
	size := ComputeShares(stakeUSDC, maxPrice)
	uo := &orders.UserOrder{
		TokenID: tokenID,
		Price:   maxPrice,
		Size:    size,
		Side:    orders.BUY,
	}
	signedOrder, err := client.CreateOrder(uo, orders.CreateOrderOptions{})
	if err != nil {
		return nil, 0, fmt.Errorf("CreateOrder(GTC): %w", err)
	}
	return signedOrder, size, nil
}

// BuildFakOrder 创建 FAK 市价单（签名但不提交）。
//
// 用 USDC 金额表达意图，传 Price 作为滑点上限避免 SDK 内部调
// CalculateMarketPrice；book 来自 MarketMonitor 订单簿，可传 nil（Price 已设置时不用）。
// 返回已签名订单和估算股数（实际成交股数由 TradeMonitor 事后更新）。
func BuildFakOrder(client TradeClient, tokenID string, maxPrice, stakeUSDC float64, book *sdk.OrderBookSummary) (*orders.SignedOrder, float64, error) {
	price := maxPrice // 必须传：跳过 CalculateMarketPrice 网络调用
	umo := &orders.UserMarketOrder{
		TokenID:   tokenID,
		Price:     &price,
		Amount:    stakeUSDC,
		Side:      orders.BUY,
		OrderType: orders.MARKET_FAK,
	}
	signedOrder, err := client.CreateMarketOrder(umo, orders.CreateOrderOptions{}, book)
	if err != nil {
		return nil, 0, fmt.Errorf("CreateMarketOrder(FAK): %w", err)
	}
	// FAK 市价单按 USDC 金额表达，最终股数由市场决定；
	// 用 maxPrice 估算作为记录初值，实际成交后由 TradeMonitor 覆盖。
	estShares := ComputeShares(stakeUSDC, maxPrice)
	return signedOrder, estShares, nil
}

// BuildOrder 按策略字符串分发到对应的下单构建函数。
//
// book 来自 MarketMonitor 订单簿（*sdk.OrderBookSummary），可传 nil。
//
// 返回：
//   - signedOrder: 已签名订单
//   - orderType: PostOrder 时使用的 OrderType（GTC / FAK）
//   - shares: 精确或估算股数（用于 OrderRecord 初始值）
func BuildOrder(client TradeClient, strategy string, tokenID string, maxPrice, stakeUSDC float64, book *sdk.OrderBookSummary) (*orders.SignedOrder, orders.OrderType, float64, error) {
	switch strategy {
	case StrategyFAK:
		signedOrder, shares, err := BuildFakOrder(client, tokenID, maxPrice, stakeUSDC, book)
		if err != nil {
			return nil, "", 0, err
		}
		return signedOrder, orders.FAK, shares, nil
	default: // StrategyGTC 及未知值均回退到 GTC
		signedOrder, shares, err := BuildGtcOrder(client, tokenID, maxPrice, stakeUSDC)
		if err != nil {
			return nil, "", 0, err
		}
		return signedOrder, orders.GTC, shares, nil
	}
}
