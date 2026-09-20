package trading

import (
	"github.com/tidwall/gjson"
	"github.com/xiangxn/go-polymarket-sdk/orders"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// TradeClient 是 CLOB 下单能力的最小接口，便于单元测试 mock。
// v4 需要: 限价单签名（CreateOrder）+ 提交（PostOrder）+ 订单查询（GetOpenOrders）
// + 撤单（CancelOrder）——后两个是 GTC 挂单终态跟踪的两条腿，见 FillTracker。
// 市价单不在范围内（本策略只挂限价）。
type TradeClient interface {
	// CreateOrder 创建并签名一个限价单（不提交）。price/size 须符合 tick/步进
	// 约束（CreateOrder 内部 ResolveTickSize + GetNegRisk，依赖 Prefetch 预热）。
	CreateOrder(userOrder *orders.UserOrder, options orders.CreateOrderOptions) (*orders.SignedOrder, error)

	// PostOrder 提交已签名订单到 CLOB。orderType 决定执行语义（GTC=限价挂单:
	// 即时能吃的吃掉、余量留在簿上等对手方，绝不超价；成交量在 POST 之后仍可能
	// 增长，故返回的是即时结果，终态须另行查询）。
	PostOrder(order *orders.SignedOrder, orderType orders.OrderType, deferExec bool) (*gjson.Result, error)

	// GetOpenOrders 按 id/市场/token 查询订单（GET /data/orders）。两个用处:
	//   - FillTracker: 按 id 查 GTC 挂单的 size_matched/status（终态跟踪）;
	//   - 真盘三方对账（POST 响应 ↔ SizeMatched ↔ UI 持仓）与未知结果人工核对。
	GetOpenOrders(params *orders.OpenOrderParams, onlyFirstPage bool, nextCursor *string) ([]orders.OpenOrder, error)

	// CancelOrder 撤销一笔挂单（DELETE /order，L2 签名）。FillTracker 在
	// rem ≤ 策略时间腿时用它将未成交余量撤走（回测前提是「触发瞬间必成交」，
	// 更晚的成交不是本策略要的样本，见 FillTracker doc）。
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

// PostOrder 委托给 SDK。
func (s *SdkClient) PostOrder(order *orders.SignedOrder, orderType orders.OrderType, deferExec bool) (*gjson.Result, error) {
	return s.Client.PostOrder(order, orderType, deferExec)
}

// GetOpenOrders 委托给 SDK。
func (s *SdkClient) GetOpenOrders(params *orders.OpenOrderParams, onlyFirstPage bool, nextCursor *string) ([]orders.OpenOrder, error) {
	return s.Client.GetOpenOrders(params, onlyFirstPage, nextCursor)
}

// CancelOrder 委托给 SDK（DELETE /order）。
func (s *SdkClient) CancelOrder(payload *orders.OrderPayload) (*gjson.Result, error) {
	return s.Client.CancelOrder(payload)
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
