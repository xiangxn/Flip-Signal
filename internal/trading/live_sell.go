package trading

import (
	"fmt"
	"strings"
	"time"

	"github.com/tidwall/gjson"
	"github.com/xiangxn/go-polymarket-sdk/orders"

	"github.com/necklace/flip-signal/internal/flip"
)

// LiveSellExecutor 是 live 版 flip.SellExecutor（2026-10-03 决策 #34 的止损卖出腿）:
// **FAK 限价卖单** @ 触发时的持仓侧 bid（绝不低于它）→ 同步 POST → 解析即时结果。
//
// 为什么卖出用 FAK 而买入用 GTC（有意的不对称, 别顺手统一）:
//   - 买入 GTC 的理由是「保住盈亏比」——挂着的买单等来的都是会崩的窗, 但那是**入场**
//     的问题（用户 2026-09-19 决定的取舍）;
//   - 卖出是**离场**: 目标是「此刻按这个价立刻走掉」, 挂单等对手方只会让价格继续塌;
//     吃不到（FAK 余量即撤）就下一个 tick 按新的 bid 再来——这正是实盘证据里
//     「25/25 触发秒都有对手方」的场景, 不需要挂单。
//
// 一次 POST 即终态（无 resting 态、不进 FillTracker）: FAK 的余量由交易所立即撤销,
// 不存在「在簿等成交」的中间态——filled/partial/unfilled 都是终态, 调用方对
// unfilled 的处理是**下一 tick 重试**（不落盘不冻结）。
//
// 契约同 LiveExecutor: 永不返回 error, 一切失败折叠进 SellResult
// （rejected + Note; 结果未知时 Note 以 flip.ExecNoteUnknown 开头——调用方冻结该
// 仓位并交人工核对, **绝不重试**: 可能已成交, 重试就是卖两次）。
type LiveSellExecutor struct {
	client TradeClient
}

// NewLiveSellExecutor 构造卖出执行器（client 为 SdkClient 或测试 fake）。
func NewLiveSellExecutor(client TradeClient) *LiveSellExecutor {
	return &LiveSellExecutor{client: client}
}

// Sell 对一笔持仓发起 FAK 限价卖单（shares = 卖出股数, limitPrice = 快照 bid,
// 即有对手方的最高买价; 实际成交价只会 ≥ 它）。
//
// 同步阻塞至 POST 返回。永不返回 error——失败路径折叠进 SellResult:
//   - filled/partial: 交易所成交（sanity 校验通过）;
//   - unfilled: 明确未成交——FAK 无对价被撤（HTTP 400 "no orders found to match"）
//     或成功响应里成交量恒 0。调用方下一 tick 重试;
//   - rejected + Note（明确未受理: 参数非法/token 空/CreateOrder 失败/其它 4xx 拒单）;
//   - rejected + ExecNoteUnknown 前缀（传输错误/5xx/响应 schema 与假设不符:
//     订单可能已被受理并成交——冻结 + 人工核对）。
//
// ⚠️ 429 特判为 unfilled（可重试）而不是 rejected: 限流在 HTTP 层被挡下, 订单
// **确定**没被受理, 重试安全; 若按 rejected 处理会因一次限流把止损冻掉整窗。
func (t *LiveSellExecutor) Sell(tokenID string, shares float64, limitPrice float64) *flip.SellResult {
	start := time.Now()
	reject := func(note string) *flip.SellResult {
		return &flip.SellResult{Status: flip.ExecStatusRejected, Note: note}
	}
	unfilled := func(note string) *flip.SellResult {
		return &flip.SellResult{Status: flip.ExecStatusUnfilled, Note: note}
	}

	if !(shares > 0) || !(limitPrice > 0) || limitPrice > 1 {
		return reject(fmt.Sprintf("Sell: 参数非法（shares=%.4f limit=%.4f）", shares, limitPrice))
	}
	if tokenID == "" {
		return reject("Sell: tokenID 为空")
	}

	signed, err := t.client.CreateOrder(&orders.UserOrder{
		TokenID: tokenID,
		Price:   limitPrice,
		Size:    shares,
		Side:    orders.SELL,
	}, orders.CreateOrderOptions{})
	if err != nil {
		return reject(fmt.Sprintf("CreateOrder(SELL): %v", err))
	}

	resp, err := t.client.PostOrder(signed, orders.FAK, false)
	if err != nil {
		return t.sellPostErr(err, time.Since(start), unfilled)
	}
	orderID, success, errorMsg := ParsePostOrderResp(resp)
	if !success {
		// FAK 无对价的已知形态: CLOB 以 success=false/400 明确告知「没有可吃档位」,
		// 这不是拒单而是行情结果——按未成交处理（下一 tick 重试）。
		if isNoMatchMsg(errorMsg) {
			return unfilled(fmt.Sprintf("FAK 无对价（order=%s）: %s", orderID, errorMsg))
		}
		note := fmt.Sprintf("CLOB 拒绝(SELL): %s", errorMsg)
		if errorMsg == "" {
			note = "CLOB 拒绝(SELL)（success=false, 无 errorMsg）"
		}
		return reject(note)
	}
	return parseSellFill(resp, orderID, shares, limitPrice)
}

// sellPostErr 把 PostOrder 的传输层错误映射为「重试」或「未知/拒单」。
func (t *LiveSellExecutor) sellPostErr(err error, since time.Duration,
	unfilled func(string) *flip.SellResult) *flip.SellResult {
	msg := strings.TrimSpace(err.Error())
	// 限流: 确定未被受理, 重试安全（见 Sell doc）。
	if strings.Contains(msg, "status 429") {
		return unfilled("FAK 卖出被限流(429)（" + fmt.Sprintf("%dms", since.Milliseconds()) + "）")
	}
	if strings.HasPrefix(msg, "API request failed with status 4") {
		// FAK 无对价也可能从 400 走出来（响应体里带 error 文本）
		if isNoMatchMsg(msg) {
			return unfilled(shortMsg(msg, 300))
		}
		return &flip.SellResult{Status: flip.ExecStatusRejected, Note: shortMsg(msg, 400)}
	}
	return &flip.SellResult{
		Status: flip.ExecStatusRejected,
		Note:   flip.ExecNoteUnknown + ": POST(SELL) " + shortMsg(msg, 300) + fmt.Sprintf("（%dms）", since.Milliseconds()),
	}
}

// isNoMatchMsg 判断错误文本是否为 FAK/FOK 无对价的已知形态。
func isNoMatchMsg(s string) bool {
	return strings.Contains(strings.ToLower(s), "no orders found to match")
}

// parseSellFill 解析 FAK 卖单的响应（见 LiveExecutor 的 parseFill——**字段语义
// 与 BUY 相反**, 这是本函数唯一容易写错的地方）。
//
// 字段口径（SDK GetOrderRawAmounts: SELL → maker=股数、taker=股数×价格;
// py-clob-client 同源例证）: 复述一句——**己方 SELL 视角
// makingAmount = 成交股数、takingAmount = 收到 USDC**。
//
// 单位不做硬编码假设（同 parseFill 的不变量判别法）: 成交股数不可能超过下单量,
// 故 makingAmount 一旦 > 请求股数, 必是 1e6 基单位 ⇒ 两字段一起除。
//
// sanity 校验（兜语义颠倒/单位误判, 宁缺勿错 → 未知, 交人工核对）:
//  1. 0 < shares ≤ reqShares + 0.005（成交不超下单量）
//  2. cash > 0
//  3. `limit − 0.005 ≤ cash/shares ≤ 1.005`——下界 = 卖价不劣于限价（限价是**最低**
//     可接受价, 撮合只会 ≥）; 上界 = 每股兑付不可能超过 1 USDC。两字段记反时
//     cash/shares ≈ 1/limit（0.26 的限价 → 3.8）远超上界 ⇒ 语义颠倒被这一条兜住。
func parseSellFill(resp *gjson.Result, orderID string, reqShares, limitPrice float64) *flip.SellResult {
	unknown := func(why string) *flip.SellResult {
		note := flip.ExecNoteUnknown + ": 卖出成交解析 " + why + "（原始响应需人工核对: " + shortMsg(resp.Raw, 300) + "）"
		return &flip.SellResult{Status: flip.ExecStatusRejected, Note: note}
	}

	making, hasMaking := amountOf(resp, "makingAmount") // 己方 SELL: 成交股数
	taking, hasTaking := amountOf(resp, "takingAmount") // 己方 SELL: 收到 USDC
	st := resp.Get("status").String()

	// 明确 0 成交（FAK 被立即撤销）: 按未成交交给调用方重试; 字段缺失则不敢下结论。
	if hasMaking && hasTaking && making <= 0 && taking <= 0 {
		return &flip.SellResult{
			Status: flip.ExecStatusUnfilled,
			Note:   fmt.Sprintf("FAK 卖出即时 0 成交（status=%q, order=%s）", st, orderID),
		}
	}
	if !hasMaking || !hasTaking {
		return unknown(fmt.Sprintf("无成交量字段 status=%q order=%q", st, orderID))
	}

	shares, cash := making, taking
	if making > reqShares+0.005 {
		shares, cash = making/1e6, taking/1e6
	}
	if shares <= reqShares+0.005 && cash > 0 {
		px := cash / shares
		if px >= limitPrice-0.005 && px <= 1.005 {
			status := flip.ExecStatusPartial
			if shares >= reqShares-0.005 {
				status = flip.ExecStatusFilled
			}
			return &flip.SellResult{Status: status, Shares: shares, Price: px}
		}
	}
	return unknown(fmt.Sprintf("sanity 不过: making=%.6f taking=%.6f req=%.2f limit=%.3f", making, taking, reqShares, limitPrice))
}

// 编译期断言: 实盘卖出实现满足 flip.SellExecutor 契约（与 flip.PaperSellExecutor 同形）。
var _ flip.SellExecutor = (*LiveSellExecutor)(nil)
