package trading

import (
	"github.com/tidwall/gjson"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
	"github.com/xiangxn/go-polymarket-sdk/orders"
)

// TradeClient 是 CLOB 交易能力的最小接口，便于单元测试 mock。
type TradeClient interface {
	// CreateOrder 创建并签名一个限价单（不提交）。
	CreateOrder(userOrder *orders.UserOrder, options orders.CreateOrderOptions) (*orders.SignedOrder, error)

	// CreateMarketOrder 创建并签名一个市价单（不提交）。
	CreateMarketOrder(userMarketOrder *orders.UserMarketOrder, options orders.CreateOrderOptions, book *sdk.OrderBookSummary) (*orders.SignedOrder, error)

	// PostOrder 提交已签名订单到 CLOB。orderType 决定执行语义（GTC/FOK/FAK 等）。
	PostOrder(order *orders.SignedOrder, orderType orders.OrderType, deferExec bool) (*gjson.Result, error)

	// GetOpenOrders 查询当前挂单 / 近期订单状态。
	GetOpenOrders(params *orders.OpenOrderParams, onlyFirstPage bool, nextCursor *string) ([]orders.OpenOrder, error)

	// CancelOrder 取消指定订单。
	CancelOrder(payload *orders.OrderPayload) (*gjson.Result, error)
}

// SdkClient 将 *sdk.PolymarketClient 适配为 TradeClient。
type SdkClient struct {
	Client *sdk.PolymarketClient
}

// CreateOrder 委托给 SDK。
func (s *SdkClient) CreateOrder(u *orders.UserOrder, o orders.CreateOrderOptions) (*orders.SignedOrder, error) {
	return s.Client.CreateOrder(u, o)
}

// CreateMarketOrder 委托给 SDK。
func (s *SdkClient) CreateMarketOrder(u *orders.UserMarketOrder, o orders.CreateOrderOptions, book *sdk.OrderBookSummary) (*orders.SignedOrder, error) {
	return s.Client.CreateMarketOrder(u, o, book)
}

// PostOrder 委托给 SDK。
func (s *SdkClient) PostOrder(order *orders.SignedOrder, orderType orders.OrderType, deferExec bool) (*gjson.Result, error) {
	return s.Client.PostOrder(order, orderType, deferExec)
}

// GetOpenOrders 委托给 SDK。
func (s *SdkClient) GetOpenOrders(params *orders.OpenOrderParams, onlyFirstPage bool, nextCursor *string) ([]orders.OpenOrder, error) {
	return s.Client.GetOpenOrders(params, onlyFirstPage, nextCursor)
}

// CancelOrder 委托给 SDK。
func (s *SdkClient) CancelOrder(payload *orders.OrderPayload) (*gjson.Result, error) {
	return s.Client.CancelOrder(payload)
}

// ParsePostOrderResp 解析 PostOrder 返回的 gjson 响应。
//
// 返回 orderID、success、errorMsg。
func ParsePostOrderResp(resp *gjson.Result) (orderID string, success bool, errorMsg string) {
	orderID = resp.Get("orderID").String()
	success = resp.Get("success").Bool()
	errorMsg = resp.Get("errorMsg").String()
	return
}
