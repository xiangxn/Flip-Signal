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
// **GTC 限价买单** @ 触发 ask（绝不超价）→ 同步 POST → 解析即时结果回 ExecResult。
// 两阶段落盘/风控闸由 cmd/flip main 编排, 本类型只负责「构造订单 → 提交 →
// 解析 POST 响应」, 不含记录器与重试。
//
// ⚠️ GTC 与 FAK 的语义差别（2026-09-19 换单型, 用户决定「保住盈亏比」）:
//   - FAK = taker-limit: POST 那一刻吃掉 ≤ 限价的档位, 余量立即撤销——一次即终态。
//     代价是**脆弱**: tick 采样到 POST 到达有 1~3s 延迟, 触发那一瞬挂在 0.20 的
//     卖单常常已被吃走/撤走, FAK 只能吃个零头（或整单落空）。
//   - GTC = 限价挂单: 即时能吃的照吃, **吃不完的留在簿上继续等对手方**——延迟
//     不再是致命的, 谁在窗口内砸出来我们就接住谁。挂着等 = 用逆向选择买成交率
//     （下方「样本偏差」段）, 故**不等整窗**: 到 rem ≤ 策略时间腿（RemMin =
//     180s）就把未成交余量撤掉, 由 trading.FillTracker 发 DELETE /order
//     （2026-09-19 用户口径, 取代同日早些时候的「不撤单」——回测前提是触发瞬间
//     必成交, rem ≤ 180 之后的成交不是这条策略要的样本）。
//   - 代价是**成交量不再于 POST 那一刻定稿**: 仓位可能在 POST 返回后任意时刻
//     继续变大, 直到撤单生效。故 GTC 的 POST 响应只回即时结果, 置 resting
//     （ExecStatusResting）交 trading.FillTracker 跟踪到撤单确认/闭市定稿。
//
// 为什么不用 GTD（交易所侧到期自动撤）: ① CLOB 规则下 GTD 的 expiration 前 60s
// 就被安全阈值提前撤掉, 且 expiration 必须 ≥ now+180s ⇒ 最小有效挂单期 ~2 分钟,
// 对 5 分钟窗口（触发点 rem 只剩 3 分钟）几乎等于挂到闭市; ② SDK 的 PostOrder 把
// expiration 硬编码为 "0"（orders.OrderToDTO 的最后一个实参）, 且它不在 EIP-712
// 签名结构里——想在客户端用上 GTD 得改 SDK。故撤单由我们自己发。
//
// 一个已知的样本偏差（写在这里以免被当成免费午餐）: 挂单越久, 成交样本越偏向
// 「价格继续下探」的那批——价格从 0.20 反弹回去的单子挂单根本不会成交。挂单换来
// 的成交率是用逆向选择买的, 只有闭市前的实盘样本能给出真实答案（回测的
// WR 24.6% 对应「触发瞬间拿到位置」, 挂单成交的子样本会系统性更差一些）。
type LiveExecutor struct {
	client TradeClient
}

// NewLiveExecutor 构造执行器（client 为 SdkClient 或测试 fake）。
func NewLiveExecutor(client TradeClient) *LiveExecutor {
	return &LiveExecutor{client: client}
}

// Execute 对一个 ok 观测发起一笔 GTC 限价买单（买狗侧 token 本身:
// obs.Side=yes → 买 UP/yes token, tokenID 由调用方按窗口传入）。
// 同步阻塞至 POST 返回（SDK 10s HTTP 超时 + 429 退避重试预算内）。
// 永不返回 error——一切失败路径都折叠进 ExecResult:
//   - rejected + Note（明确未受理: 规格错/token 空/CreateOrder 失败/HTTP 4xx 拒单）
//   - rejected + Note 以 flip.ExecNoteUnknown 开头（POST 传输错误/超时/5xx:
//     订单可能已被受理——重启扫描人工核对, 不自动补单）
//   - resting（挂单在簿: 即时成交 0 或只吃下一部分, 余量留在簿上——**这不是终态**,
//     成交量要等 FillTracker 在撤单确认时定稿; order_id 必填, 否则转人工核对）
//   - filled（即时全额成交, sanity 校验通过——已是终态, 无需跟踪）
//
// 「未成交（unfilled）」不再是 POST 能给出的结论: GTC 吃不到就挂着等, 只有定稿
// （rem ≤ RemMin 撤单后拿到 size_matched）才知道它是 0 成交。
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

	// 同步提交（GTC = 限价挂单: 即时能吃的吃掉, 余量留在簿上等对手方; 到
	// rem ≤ RemMin 由 FillTracker 撤掉未成交余量——见类型 doc）
	resp, err := t.client.PostOrder(signed, orders.GTC, false)
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

// parseFill 解析 PostOrder 成功响应（success=true）的**即时**成交量。
//
// GTC 语义（2026-09-19）: 这个响应只描述 POST 那一瞬——`status` 为 live 表示
// 订单已挂上簿（可能一点没吃到）, taking/making 是当下已成交的部分。**终态不
// 在这里**, 由 FillTracker 在 rem ≤ RemMin 撤单时读到 size_matched 定稿。产出:
//   - filled: 即时全额成交（sanity 全过且 shares ≈ 请求量）——已是终态;
//   - resting: 其余一切「订单进了簿」的情形（即时 0 成交 / 只吃下一部分 /
//     status=unmatched）——交 FillTracker;
//   - rejected + ExecNoteUnknown: 响应与其 schema 假设不符, 无法判断订单状态
//     （可能已成交）——转人工核对, 绝不错记。
//
// 口径证据（2026-09-10 架构审阅, 本实现唯一需真盘验证点）: 响应 makingAmount/
// takingAmount 是己方订单成交部分的 maker/taker 金额——己方 BUY 视角
// takingAmount=成交股数、makingAmount=花费 USDC。证据: SDK 订单侧约定
// (orders.GetOrderRawAmounts: BUY → maker=size×price USDC, taker=size 股) +
// 两个独立真盘例证自洽 (py-clob-client#311: BUY 5 股@0.29 → taking '5'/
// making '1.45'; polygolem walkthrough: SELL → making=股数)。同一请求管线
// 下两字段比例 = 成交均价, 与限价同 tick 格点。
//
// 真盘验证 SOP（2026-09-10 用户确认; 2026-09-19 按 GTC 改）: 首笔实盘 1U 试单,
// **等该窗定稿（撤单确认）后**三方对账——POST 响应（即时）↔ GetOpenOrders(Id) 的
// SizeMatched/Price（终态, 即 FillTracker 读数）↔ UI 持仓, 三方一致才确认解析
// 与跟踪口径可信并放开正常单; 口径在真盘证伪前保持 sanity 自动判别（下方 sanity
// 全过才记 filled, 否则转 resting 或人工核对, 绝不错记）。
//
// 单位不做硬编码假设: CLOB 返回的金额可能是原始小数（py 例证）也可能被请求
// 管线换算成 1e6 基单位（SDK ParseUnits 方向）。刻度判别用**不变量**「成交股数
// 不可能超过下单量」（GTC 起必须如此）: taking > 请求股数 ⇒ 必是基单位（原始
// 小数下 taking = 成交股数 ≤ 请求量, 恒不越界）。旧法用 taking/请求股数的量级比
// 判 ≈1 还是 ≈1e6, 部分成交会落进「既非 1 也非 1e6」的灰区被误判人工核对——
// GTC 下部分成交是常态, 故换掉。
//
// sanity 校验（兜语义颠倒/单位误判, 宁缺勿错）: 全过才按成交记账, 任一不过 →
// rejected + ExecNoteUnknown（金额在但不可信, 与 submitting 同属人工核对类）:
//  1. 0 < shares ≤ reqShares + 0.005（成交不超请求）
//  2. cost > 0
//  3. |cost/shares − price| ≤ 0.005 —— 触发价为当时最优 ask, 盘口同价档吃满;
//     成交均价偏离超过半分钱即不可能。这条正是语义颠倒的兜底: 两字段记反时
//     cost/shares ≈ 1/price ≈ 5.26, 远超半分钱 → 不会把「花 1.9U 买 10 股」
//     错记成「花 10U 买 1.9 股」
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
	st := resp.Get("status").String()

	// 即时 0 成交: GTC 下这是「订单在簿等对手方」, 不是 FAK 时代的「无对价即撤」。
	// order_id 拿到就交 FillTracker 跟踪（撤单时查到的 size_matched 才是终值）。
	if !hasTaking || !hasMaking || (taking <= 0 && making <= 0) {
		if orderID == "" {
			return unknown(fmt.Sprintf("无 orderID 无法跟踪挂单（status=%q）", st))
		}
		switch st {
		case "live", "delayed", "unmatched", "":
			return &flip.ExecResult{
				Status:  flip.ExecStatusResting,
				OrderID: orderID,
				Note:    fmt.Sprintf("GTC 挂单在簿（status=%q 即时 0 成交, 等 FillTracker 撤单时定稿）", st),
			}
		}
		// 无成交量字段且 status 也不认识: 可能是响应 schema 与假设不符（实际已
		// 成交）——宁缺勿错, 转人工核对（不按 0 成交记）
		return unknown(fmt.Sprintf("无成交量字段 status=%q", st))
	}

	// 刻度判别（部分成交也适用——见 doc）: 成交股数不可能超过下单量, 故 taking
	// 一旦大于请求股数, 它必是 1e6 基单位。
	shares, cost := taking, making
	if taking > reqShares+0.005 {
		shares, cost = taking/1e6, making/1e6
	}

	if shares <= reqShares+0.005 && cost > 0 && abs(cost/shares-price) <= 0.005 {
		if shares >= reqShares-0.005 {
			// 即时全额成交 = 终态, 无需跟踪（部分成交则相反: 余量还在簿上变得更多）
			return &flip.ExecResult{
				Status:    flip.ExecStatusFilled,
				OrderID:   orderID,
				FillPrice: cost / shares,
				Shares:    shares,
				Cost:      cost,
			}
		}
		if orderID == "" {
			return unknown(fmt.Sprintf("部分成交但无 orderID 无法跟踪（shares=%.4f/%.4f）", shares, reqShares))
		}
		return &flip.ExecResult{
			Status:  flip.ExecStatusResting,
			OrderID: orderID,
			Note:    fmt.Sprintf("GTC 即时成交 %.2f/%.2f 股 @%.4f, 余量挂单在簿（等 FillTracker 撤单时定稿）", shares, reqShares, cost/shares),
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
