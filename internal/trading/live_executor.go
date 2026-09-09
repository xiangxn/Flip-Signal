package trading

import (
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/tidwall/gjson"
	"github.com/xiangxn/go-polymarket-sdk/orders"

	"github.com/necklace/flip-signal/internal/flip"
)

// LiveExecutor 是 live 版 flip.Executor（纸面/实盘同一契约, 见 internal/flip/
// exec.go——main 单点经接口调用, 本实现与 flip.PaperExecutor 互换）:
// FAK 限价单 @ 触发 ask（绝不超价）→ 同步 POST → 解析成交结果回 ExecResult。
// 两阶段落盘/风控闸由 cmd/flip main 编排, 本类型只负责「构造订单 → 提交 →
// 解析终态」, 不含记录器与重试（FAK 一次即终态）。
type LiveExecutor struct {
	client TradeClient
}

// NewLiveExecutor 构造执行器（client 为 SdkClient 或测试 fake）。
func NewLiveExecutor(client TradeClient) *LiveExecutor {
	return &LiveExecutor{client: client}
}

// Execute 对一个 ok 观测发起一笔 FAK 限价买单（买狗侧 token 本身:
// obs.Side=yes → 买 UP/yes token, tokenID 由调用方按窗口传入）。
// 同步阻塞至 POST 返回（SDK 10s HTTP 超时 + 429 退避重试预算内）。
// 永不返回 error——一切失败路径都折叠进 ExecResult:
//   - rejected + Note（明确未受理: 规格错/token 空/CreateOrder 失败/HTTP 4xx 拒单）
//   - rejected + Note 以 flip.ExecNoteUnknown 开头（POST 传输错误/超时/5xx:
//     订单可能已被受理——重启扫描人工核对, 不自动补单）
//   - unfilled（status 确认 0 成交, order_id 留档）
//   - filled/partial（sanity 校验通过的成交, Shares/Cost/FillPrice 为实际值）
func (t *LiveExecutor) Execute(o *flip.Observation, tokenID string, stake float64) *flip.ExecResult {
	start := time.Now()
	reject := func(note string) *flip.ExecResult {
		return &flip.ExecResult{Status: flip.ExecStatusRejected, Note: note}
	}

	price, shares, err := OrderSpecForObs(o, stake)
	if err != nil {
		return reject(err.Error())
	}
	if tokenID == "" {
		return reject("Execute: tokenID 为空")
	}

	// 签名构造。CreateOrder 内部 ResolveTickSize + GetNegRisk 依赖每窗 Prefetch
	// 预热（见 PrefetchTokenInfo）——预热缺失时此处会退回网调并可能失败。
	signed, err := t.client.CreateOrder(&orders.UserOrder{
		TokenID: tokenID,
		Price:   price,
		Size:    shares,
		Side:    orders.BUY,
	}, orders.CreateOrderOptions{})
	if err != nil {
		return reject(fmt.Sprintf("CreateOrder: %v", err))
	}

	// 同步提交（FAK = 部分成交即撤, taker-limit 不吃挂）
	resp, err := t.client.PostOrder(signed, orders.FAK, false)
	if err != nil {
		return t.postErr(err, time.Since(start))
	}
	orderID, success, errorMsg := ParsePostOrderResp(resp)
	if !success {
		note := fmt.Sprintf("CLOB 拒绝: %s", errorMsg)
		if errorMsg == "" {
			note = "CLOB 拒绝（success=false, 无 errorMsg）"
		}
		return &flip.ExecResult{Status: flip.ExecStatusRejected, OrderID: orderID, Note: note}
	}
	return parseFill(resp, orderID, shares, price)
}

// postErr 把 PostOrder 的传输层错误映射为 rejected。
// SDK Post 对 HTTP ≥400 返回 "API request failed with status 4xx: <body>"——
// 服务端明确拒绝, 订单不可能被受理, 记 rejected（note 带状态与 body 片段）;
// 其余（超时/断连/5xx 无 body 语义）订单是否受理未知 → ExecNoteUnknown 标记,
// 载入/重启扫描判据（main 侧 NeedsReconcile 汇总, 人工核对）。
func (t *LiveExecutor) postErr(err error, since time.Duration) *flip.ExecResult {
	msg := strings.TrimSpace(err.Error())
	if strings.HasPrefix(msg, "API request failed with status 4") {
		return &flip.ExecResult{Status: flip.ExecStatusRejected, Note: shortMsg(msg, 400)}
	}
	return &flip.ExecResult{
		Status: flip.ExecStatusRejected,
		Note:   flip.ExecNoteUnknown + ": POST " + shortMsg(msg, 300) + fmt.Sprintf("（%dms）", since.Milliseconds()),
	}
}

// ── 成交解析 ──

// parseFill 解析 PostOrder 成功响应（success=true）的实际成交量。
//
// 口径证据（2026-09-10 架构审阅, 本实现唯一需真盘验证点）: 响应 makingAmount/
// takingAmount 是己方订单成交部分的 maker/taker 金额——己方 BUY 视角
// takingAmount=成交股数、makingAmount=花费 USDC。证据: SDK 订单侧约定
// (orders.GetOrderRawAmounts: BUY → maker=size×price USDC, taker=size 股) +
// 两个独立真盘例证自洽 (py-clob-client#311: BUY 5 股@0.29 → taking '5'/
// making '1.45'; polygolem walkthrough: SELL → making=股数)。同一请求管线
// 下两字段比例 = 成交均价, 与限价同 tick 格点。
//
// 真盘验证 SOP（2026-09-10 用户确认）: 首笔实盘 1U 试单, 同一 order_id 三方
// 对账——POST 响应 taking/making ↔ GetOpenOrders(Id) 读 SizeMatched/Price ↔
// UI 持仓, 三方一致才确认本解析可信并放开正常单; 口径在真盘证伪前保持
// sanity 自动判别（下方 sanity 全过才记账, 否则转未知结果人工核对, 绝不错记）。
//
// 单位不做硬编码假设: CLOB 返回的金额可能是原始小数（py 例证）也可能被请求
// 管线换算成 1e6 基单位（SDK ParseUnits 方向）——两字段比例不受刻度影响,
// 刻度由 taking ≈ 请求股数的量级自动判别（≈1 或 ≈1e6）, 其余值判异常。
//
// sanity 校验（兜语义颠倒/单位误判, 宁缺勿错）: 全过才按成交记账, 任一不过 →
// rejected + ExecNoteUnknown（金额在但不可信, 与 submitting 同属人工核对类）:
//  1. 0 < shares ≤ reqShares + 0.005（成交不超请求）
//  2. cost > 0
//  3. |cost/shares − price| ≤ 0.005 —— 触发价为当时最优 ask, 盘口同价档吃满;
//     成交均价偏离超过半分钱即不可能（若两字段语义记反, 比值 ≈ 1/price ≫ 1,
//     必然失败 → 不会把「花 1.9U 买 10 股」错记成「花 10U 买 1.9 股」）
func parseFill(resp *gjson.Result, orderID string, reqShares, price float64) *flip.ExecResult {
	unknown := func(why string) *flip.ExecResult {
		return &flip.ExecResult{
			Status:  flip.ExecStatusRejected,
			OrderID: orderID,
			Note:    flip.ExecNoteUnknown + ": 成交解析 " + why + "（原始响应需人工核对: " + shortMsg(resp.Raw, 300) + "）",
		}
	}

	taking, hasTaking := amountOf(resp, "takingAmount")
	making, hasMaking := amountOf(resp, "makingAmount")
	matched := resp.Get("status").String()
	if !hasTaking || !hasMaking || (taking <= 0 && making <= 0) {
		if matched == "unmatched" {
			// 服务端明确无对价即撤 → 0 成交（明确终态, order_id 留档）
			msg := "无对价未成交"
			if em := resp.Get("errorMsg").String(); em != "" {
				msg = "未成交: " + shortMsg(em, 200)
			}
			return &flip.ExecResult{Status: flip.ExecStatusUnfilled, OrderID: orderID, Note: msg}
		}
		// 无成交量字段且非明确 unmatched: 可能是响应 schema 与假设不符（实际已
		// 成交）——宁缺勿错, 转人工核对（不按 0 成交记）
		return unknown(fmt.Sprintf("无成交量字段 status=%q", matched))
	}

	// 刻度判别: taking 与请求股数的量级比 ≈1（原始小数）或 ≈1e6（基单位）
	scale := taking / reqShares
	if scale > 500_000 {
		taking, making = taking/1e6, making/1e6
	} else if scale > 1000 {
		return unknown(fmt.Sprintf("taking/请求 = %.0f 不在 {1, 1e6} 刻度域", scale))
	}

	shares, cost := taking, making
	if shares <= reqShares+0.005 && cost > 0 && abs(cost/shares-price) <= 0.005 {
		status := flip.ExecStatusFilled
		if shares < reqShares-0.005 {
			status = flip.ExecStatusPartial // 深度不足, 按实际成交记录
		}
		return &flip.ExecResult{
			Status:    status,
			OrderID:   orderID,
			FillPrice: cost / shares,
			Shares:    shares,
			Cost:      cost,
		}
	}
	// 金额在但越界: 语义或刻度理解错误, 转人工
	return unknown(fmt.Sprintf("sanity 不过: taking=%.6f making=%.6f req=%.2f price=%.3f", taking, making, reqShares, price))
}

// amountOf 取响应金额字段（字符串/数值皆可, 原样返回不换算）。
func amountOf(resp *gjson.Result, key string) (float64, bool) {
	v := resp.Get(key)
	if !v.Exists() {
		return 0, false
	}
	if v.Type == gjson.Number {
		return v.Float(), true
	}
	s := v.String()
	if s == "" {
		return 0, false
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return 0, false
	}
	return f, true
}

func abs(x float64) float64 {
	if x < 0 {
		return -x
	}
	return x
}

// shortMsg 截断错误/响应文本（日志/note 长度控制）。
func shortMsg(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}

// SdkClient 编译期接口断言（确保适配器实现 TradeClient）。
var _ TradeClient = (*SdkClient)(nil)

// 编译期断言: LiveExecutor 满足 flip.Executor 契约（与 flip.PaperExecutor 同形,
// main 经接口单点调用 Execute, paper/live 不散落两条调用路径）。
var _ flip.Executor = (*LiveExecutor)(nil)
