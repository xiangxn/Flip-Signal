package trading

import (
	"github.com/tidwall/gjson"
	"github.com/xiangxn/go-polymarket-sdk/orders"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// TradeClient 是 CLOB 下单能力的最小接口，便于单元测试 mock。
// v4 只需 FAK 限价单路径（CreateOrder 签名 + PostOrder 提交）+ 挂单/近期订单
// 查询（首单真盘对账、未知结果兜底核对用）；GTC/市价单/撤单不属于首版范围，
// 需要时从 eth 分支历史恢复对应方法。
type TradeClient interface {
	// CreateOrder 创建并签名一个限价单（不提交）。price/size 须符合 tick/步进
	// 约束（CreateOrder 内部 ResolveTickSize + GetNegRisk，依赖 Prefetch 预热）。
	CreateOrder(userOrder *orders.UserOrder, options orders.CreateOrderOptions) (*orders.SignedOrder, error)

	// PostOrder 提交已签名订单到 CLOB。orderType 决定执行语义（FAK=部分成交
	// 即撤单——taker-limit：只吃 ≤ 限价的 resting 档，绝不超价）。
	PostOrder(order *orders.SignedOrder, orderType orders.OrderType, deferExec bool) (*gjson.Result, error)

	// GetOpenOrders 按 id/市场/token 查询订单（首单对账与未知结果核对通道）。
	GetOpenOrders(params *orders.OpenOrderParams, onlyFirstPage bool, nextCursor *string) ([]orders.OpenOrder, error)
}

// SdkClient 将 *sdk.PolymarketClient 适配为 TradeClient。
type SdkClient struct {
	Client *sdk.PolymarketClient
}

// CreateOrder 委托给 SDK。
func (s *SdkClient) CreateOrder(u *orders.UserOrder, o orders.CreateOrderOptions) (*orders.SignedOrder, error) {
	return s.Client.CreateOrder(u, o)
}

// PostOrder 委托给 SDK。
func (s *SdkClient) PostOrder(order *orders.SignedOrder, orderType orders.OrderType, deferExec bool) (*gjson.Result, error) {
	return s.Client.PostOrder(order, orderType, deferExec)
}

// GetOpenOrders 委托给 SDK。
func (s *SdkClient) GetOpenOrders(params *orders.OpenOrderParams, onlyFirstPage bool, nextCursor *string) ([]orders.OpenOrder, error) {
	return s.Client.GetOpenOrders(params, onlyFirstPage, nextCursor)
}

// ParsePostOrderResp 解析 PostOrder 返回的 gjson 响应。
//
// 返回 orderID、success、errorMsg（HTTP <400 的响应体字段；CLOB 拒绝订单通常
// 以 success=false + errorMsg 表达，HTTP ≥400 则由 SDK Post 直接返回 error）。
func ParsePostOrderResp(resp *gjson.Result) (orderID string, success bool, errorMsg string) {
	orderID = resp.Get("orderID").String()
	success = resp.Get("success").Bool()
	errorMsg = resp.Get("errorMsg").String()
	return
}
