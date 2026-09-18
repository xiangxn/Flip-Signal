package trading

import (
	"context"
	"fmt"
	"log"
	"strings"
	"sync"
	"time"

	"github.com/xiangxn/go-polymarket-sdk/orders"

	"github.com/necklace/flip-signal/internal/flip"
)

// ── GTC 挂单跟踪（2026-09-19 起 live 下单为 GTC, 见 live_executor.go 类型 doc）──
//
// 为什么需要它: GTC 的 POST 响应只是「此刻」——订单挂在簿上等对手方, 成交可以在
// POST 返回之后任意时刻继续发生。仓位大小（结算 P&L 的分母）因此不能像 FAK 那样在
// POST 那一刻定稿, 必须等挂单走完。本类型只做两件事: 到点撤单, 把累计成交
// （CLOB size_matched）查清楚回填终态。
//
// 谁负责撤单: **我们自己**, 在 rem ≤ 策略时间腿（flip.Config.RemMin = 180s）那一刻
// 撤掉未成交的余量（2026-09-19 用户口径, 取代同日早些时候的「不撤单」）。为什么不
// 挂到闭市: 回测的前提是「触发瞬间必成交」——成交价记的就是触发那一刻的 ask; 一笔
// rem ≤ 180 之后才成交的挂单已经不属于这条策略了, 它换来的是策略明确不要的那批
// 逆向选择成交（砸到 0.2 后一路拖到窗口尾盘才被吃掉的子样本）。PM 侧的 GTD 借不上
// 力（5 分钟事件里最小有效期 ~2 分钟, SDK 还把 expiration 硬编码成 "0"——见
// live_executor.go 的 GTD 段）, 所以撤单只能自己发。
//
// 与 ResolutionPoller 同形（长驻 goroutine + 周期轮询 + 回调交还结果）:
//   - Register 有两个调用时机（都在 cmd/flip）: ① POST 返回 resting（挂单在簿）;
//     ② 重启扫描磁盘发现 resting 遗留行（进程死在挂单期间）——接管后走同一条路;
//   - 轮询 GET /data/orders?id=<orderID>（SDK GetOpenOrders, 一次签名请求）;
//   - 撤单: 到 rem ≤ RemMin 且订单可能仍在簿上 → DELETE /order（见 cancel）;
//   - 终态判据（任一命中即定稿）:
//     ① status 变 MATCHED/CANCELED;  ② 挂单表里查不到该单（被撤/闭市撤回）;
//     ③ 累计成交已达请求股数;        ④ 撤单后再见该单且确认宽限用尽;
//     ⑤ 闭市宽限 + 最后一次查询用尽（硬截止, 兜撤单失败的底）。
//   - 定稿结果经 onFinal 交回 cmd/flip（落盘 + 按最终 shares/cost 注册结算）。
//
// 已知取舍（写在代码里以免被当成漏洞）:
//   - **成交价按限价记**（cost = shares × 限价）: 挂单等来的成交必然是限价
//     （我们是 maker, 成交价 = 挂单价）, 只有 POST 那一瞬的 taker 吃单可能优于
//     限价——故成本至多略微高估、P&L 略微低估, 方向保守（不会虚增）。精确到笔
//     的成交明细在 CLOB 只有 associate_trades（本 SDK 未暴露 trades 端点）。
//   - **末次观测即终值**: 成交只在挂单活着时发生, 撤单生效/闭市后 size_matched
//     冻结, 故定稿用「最靠近撤单确认的那次观测」；暴露的偏差 = 末次查询（≤
//     fillPollInterval）到撤单生效之间的成交, 仅当端点不返回已终态订单时才会漏。
//   - **撤单是尽力而为, 不是保证**: 撤单请求失败会每 2s 重试到成功或硬截止（活着
//     的挂单会一直吃进 rem ≤ 180 之后的成交）——定稿时若撤单没成功, 行照常落盘,
//     只是 note 里没有「余量已撤」, 该窗口的成交样本按实际发生记账（宁可多记一笔
//     真成交, 不臆造）。
//   - **查不到 ≠ 0 成交**: 从未观测到该单（重启接管/接口一直失败）时保持
//     resting + ExecNoteUnknown 标记, 交人工核对——绝不把真实成交记成未成交。
type FillTracker struct {
	client       TradeClient
	onFinal      func(flip.FillFinal)
	pollInterval time.Duration
	closeGrace   time.Duration
	cancelLead   time.Duration // 撤单提前量 = 策略时间腿（rem ≤ 它即撤未成交余量）

	mu     sync.Mutex
	orders map[string]*trackedOrder // key = orderID
}

// trackedOrder 一笔在跟踪的挂单（仅内存; 崩溃后由启动扫描从磁盘 resting 行重建）。
type trackedOrder struct {
	conditionID string
	slug        string
	orderID     string
	limit       float64   // 挂单限价 = 触发 ask（Observation.Fill; 成交价按它记）
	reqShares   float64   // 目标股数 floor2(stake/limit) = 下单量
	cancelAt    time.Time // 撤单点 = 闭市 − cancelLead（即 rem ≤ RemMin 那一刻）
	deadline    time.Time // 硬截止 = 闭市 + closeGrace（撤单失败的兜底）
	lastMatched float64   // 末次观测到的累计成交股数（终值来源）
	sighted     bool      // 是否至少成功观测到该单一次（区分「没成交」与「没查到」）
	adopted     bool      // 重启接管: 无本进程 POST 背书, 「查不到」不能推定闭市撤回
	cancelSent  bool      // 撤单已成功返回（未成交余量已撤; 幂等, 不重复撤）
	cancelTried bool      // 撤单已尝试过（失败时: 见过该单才继续重试, 见 pollOne）
	canceledAt  time.Time // 撤单成功时刻（撤单确认宽限 fillCancelGrace 的起点）
	lastErrLog  time.Time
	lastCnclLog time.Time
}

// fillPollInterval 挂单查询间隔。2s = 末次观测与真相的最大时差。
const fillPollInterval = 2 * time.Second

// fillCloseGrace 闭市宽限: 窗口边界 + 该时长为硬截止点（**兜底**, 不是正常路径）。
// 正常路径在 rem ≤ RemMin 撤单后就定稿了; 这个截止只用来收尾两类例外: 撤单一直
// 失败（挂单可能挂到闭市）, 与重启接管的遗留行（窗口早已过去）。为什么不取边界
// 那一刻: Polymarket 的闭市标记有秒级延迟, 边界后立刻查可能还是 LIVE, 宽限期内
// 多查几次让「闭市前最后 2s 的成交」也落进末次观测。
const fillCloseGrace = 60 * time.Second

// fillCancelGrace 撤单确认宽限: 撤单成功后 CLOB 未必立刻把订单移出挂单表, 再等
// 这么久仍见到该单就按末次观测定稿（note 标注撤单未确认, 不静默）。
const fillCancelGrace = 15 * time.Second

// fillCancelLead 撤单提前量默认值（= 策略时间腿 flip.Config.RemMin）。
// 只在构造参数未给/非正时兜底, 正常由 cmd/flip 从配置传入。
const fillCancelLead = 180 * time.Second

// fillErrLogEvery 查询/撤单错误日志节流（接口长时间不通时不刷屏）。
const fillErrLogEvery = 30 * time.Second

// NewFillTracker 构造挂单跟踪器。cancelLead = 窗口结束前多久撤未成交余量, 取
// **策略时间腿** flip.Config.RemMin（cmd/flip 传入; 回测里触发要求 rem > 它, 故
// rem ≤ 它之后的成交都不属于本策略）。onFinal 在跟踪 goroutine 内被调用（须自行
// 保证线程安全——flip.ExecState.ApplyFillFinal 与 ResolutionPoller.Register 都是）。
func NewFillTracker(client TradeClient, cancelLead time.Duration, onFinal func(flip.FillFinal)) *FillTracker {
	if cancelLead <= 0 {
		log.Printf("⚠️ [FillTracker] 撤单提前量 %v 非正, 回退默认 %v（应为 flip.Config.RemMin）", cancelLead, fillCancelLead)
		cancelLead = fillCancelLead
	}
	return &FillTracker{
		client:       client,
		onFinal:      onFinal,
		pollInterval: fillPollInterval,
		closeGrace:   fillCloseGrace,
		cancelLead:   cancelLead,
		orders:       make(map[string]*trackedOrder),
	}
}

// Register 登记一笔 GTC 挂单（POST 返回 resting 时; adopted=true = 重启扫描接管
// 磁盘上的 resting 遗留行）。windowEnd 为本窗市场结束时刻（闭市点）。
// 同一 orderID 重复登记幂等。缺 order_id/限价/目标股数（= 无法跟踪）仅告警跳过。
func (t *FillTracker) Register(rec *flip.Record, windowEnd time.Time, adopted bool) {
	if rec == nil {
		return
	}
	if rec.OrderID == "" || rec.Fill <= 0 || rec.Stake <= 0 {
		log.Printf("⚠️ [FillTracker] 无法跟踪挂单（缺 order_id/限价/投入）: window=%s order=%q fill=%.4f stake=%.2f",
			rec.ConditionID, rec.OrderID, rec.Fill, rec.Stake)
		return
	}
	reqShares := floor2(rec.Stake / rec.Fill)
	cancelAt := windowEnd.Add(-t.cancelLead)
	deadline := windowEnd.Add(t.closeGrace)

	t.mu.Lock()
	defer t.mu.Unlock()
	if _, dup := t.orders[rec.OrderID]; dup {
		return
	}
	t.orders[rec.OrderID] = &trackedOrder{
		conditionID: rec.ConditionID,
		slug:        rec.Slug,
		orderID:     rec.OrderID,
		limit:       rec.Fill,
		reqShares:   reqShares,
		cancelAt:    cancelAt,
		deadline:    deadline,
		adopted:     adopted,
	}
	how := "POST"
	if adopted {
		how = "重启接管"
	}
	log.Printf("[FillTracker] 📋 跟踪挂单 %s（%s）: 限价 %.3f 目标 %.2f 股, 撤单点 %s（rem≤%.0fs）, 硬截止 %s",
		rec.OrderID, how, rec.Fill, reqShares,
		cancelAt.UTC().Format("15:04:05Z"), t.cancelLead.Seconds(), deadline.UTC().Format("15:04:05Z"))
}

// Count 当前在跟踪的挂单数（诊断/日志用）。
func (t *FillTracker) Count() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	return len(t.orders)
}

// Run 启动轮询循环，阻塞直到 ctx 取消。
func (t *FillTracker) Run(ctx context.Context) {
	ticker := time.NewTicker(t.pollInterval)
	defer ticker.Stop()

	log.Printf("[FillTracker] 🚀 启动挂单跟踪（间隔 %v, 撤单点 rem≤%.0fs, 闭市宽限 %v）",
		t.pollInterval, t.cancelLead.Seconds(), t.closeGrace)
	t.pollAll()

	for {
		select {
		case <-ctx.Done():
			if n := t.Count(); n > 0 {
				// 不阻塞退出: 未定稿的行以 resting 留在磁盘上, 下次启动扫描自动接管
				log.Printf("[FillTracker] 停止跟踪，%d 笔挂单未定稿（磁盘 resting 行, 重启后接管）", n)
			}
			return
		case <-ticker.C:
			t.pollAll()
		}
	}
}

// ── 内部 ──

// pollAll 对所有在跟踪的挂单跑一轮查询。defer recover 兜底: 单轮 panic 不让跟踪
// goroutine 退出（否则挂单永远定不了稿）。
func (t *FillTracker) pollAll() {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("[FillTracker] ⚠️ pollAll 崩溃已恢复: %v", r)
		}
	}()

	t.mu.Lock()
	if len(t.orders) == 0 {
		t.mu.Unlock()
		return
	}
	snapshot := make([]*trackedOrder, 0, len(t.orders))
	for _, o := range t.orders {
		snapshot = append(snapshot, o)
	}
	t.mu.Unlock()

	now := time.Now()
	for _, o := range snapshot {
		fin, done := t.pollOne(o, now)
		if !done {
			continue
		}
		t.mu.Lock()
		delete(t.orders, o.orderID)
		t.mu.Unlock()
		if t.onFinal != nil {
			t.onFinal(fin)
		}
	}
}

// pollOne 查询一笔挂单并判定是否定稿（done=true 时会携带 FillFinal）。
// 顺序要点:
//   - **先查（并归一成交量）, 再决定撤不撤, 再判终态**: 本轮查询已给出结论的不
//     发废请求（满额成交没余量可撤, 单子已不在簿上没得撤）; **查询失败时照撤**
//     ——漏撤一次的代价是余量继续挂着吃进策略不要的成交, 远大于一次废请求。
//   - **先查再判截止**: 硬截止那次也要用查询结果, 否则重启接管（deadline 早已
//     过去）会跳过唯一一次拿到真实成交量的机会。
func (t *FillTracker) pollOne(o *trackedOrder, now time.Time) (flip.FillFinal, bool) {
	overdue := !now.Before(o.deadline)

	list, err := t.client.GetOpenOrders(&orders.OpenOrderParams{Id: &o.orderID}, true, nil)
	var oo *orders.OpenOrder
	matched := 0.0
	bad := false // size_matched 语义可疑（越界）: 不能当成交量用, 但撤单照发
	if err == nil && len(list) > 0 {
		oo = &list[0]
		matched = normalizeMatched(oo.SizeMatched, o.reqShares)
		bad = matched > o.reqShares+0.005
	}

	// 撤单: 到 rem ≤ RemMin（cancelAt）就把未成交余量撤走。
	// 重试规则: 见过该单在簿（sighted）说明撤单失败是真失败, 每轮继续重试; 从没
	// 观测到过（索引延迟/POST 后订单根本没上簿）则只试一次, 不刷接口——真挂着的
	// 单只要查询后来成功, sighted 就会置上, 撤单随即恢复重试。
	doneForSure := !bad && oo != nil && matched >= o.reqShares-0.005 // 满额成交: 无余量可撤
	gone := err == nil && len(list) == 0 && (o.sighted || o.adopted) // 已不在簿: 没得撤
	if !o.cancelSent && !now.Before(o.cancelAt) && !doneForSure && !gone && (o.sighted || !o.cancelTried) {
		t.cancel(o, now)
	}

	switch {
	case err != nil:
		if overdue {
			return t.finalize(o, "查询截止（末次查询失败）"), true
		}
		if now.Sub(o.lastErrLog) >= fillErrLogEvery {
			o.lastErrLog = now
			log.Printf("[FillTracker] ⚠️ 挂单查询失败 %s: %v（%v 后重试）", o.orderID, err, t.pollInterval)
		}
		return flip.FillFinal{}, false

	case len(list) == 0:
		if !o.sighted && !o.adopted {
			// 从没观测到过这单: 「查不到」也可能只是索引延迟——不能据此判 0 成交
			if overdue {
				return t.finalize(o, "查询截止（从未观测到该单）"), true
			}
			return flip.FillFinal{}, false
		}
		// 挂单表里没有了: 我们撤单生效（或闭市被 PM 撤回）——末次观测即终值
		why := "闭市撤回（挂单表已无此单）"
		if o.cancelSent {
			why = "撤单生效（挂单表已无此单）"
		}
		return t.finalize(o, why), true
	}

	if bad {
		if !overdue && now.Before(o.cancelAt) {
			// 语义可疑但还没到撤单点: 先别放弃跟踪——定稿即移出跟踪表, 而这张单
			// 还活在簿上（可能继续吃进我们不认的成交）。留到 cancelAt, 把它从簿上
			// 摘下来再交人工。
			return flip.FillFinal{}, false
		}
		// 越界且不是已知的 1e6 刻度: 字段语义不明, 宁缺勿错——截断会凭空造出
		// 一个仓位, 故保持 resting 交人工核对（见类型 doc 的「查不到 ≠ 0 成交」）
		return t.finalizeSuspect(o, fmt.Sprintf("size_matched=%.4f 超过请求 %.2f 股且非 1e6 刻度, 字段语义不明", oo.SizeMatched, o.reqShares), true), true
	}
	o.sighted = true
	if matched > o.lastMatched {
		log.Printf("[FillTracker] 🎯 挂单成交 %s +%.2f 股 → 累计 %.2f/%.2f（status=%s）",
			o.orderID, matched-o.lastMatched, matched, o.reqShares, oo.Status)
		o.lastMatched = matched
	}

	switch strings.ToUpper(oo.Status) {
	case "MATCHED", "CANCELED":
		return t.finalize(o, "status="+strings.ToUpper(oo.Status)), true
	}
	if o.lastMatched >= o.reqShares-0.005 {
		return t.finalize(o, "累计成交达下单量"), true
	}
	// 撤单成功却仍见该单: 给 CLOB 一点处理时间（撤单标记有秒级延迟）, 宽限用尽即
	// 按末次观测定稿——note 会因「已撤单但查到仍在簿」而显得微妙, 但成交量本身
	// 是实测值, 不臆造。
	if o.cancelSent && !overdue && now.Sub(o.canceledAt) >= fillCancelGrace {
		return t.finalize(o, "撤单后仍见挂单（确认宽限用尽, 取末次观测）"), true
	}
	if overdue {
		return t.finalize(o, "查询截止（闭市后仍见挂单, 取末次观测）"), true
	}
	return flip.FillFinal{}, false
}

// cancel 发一次撤单请求（DELETE /order）。
// 只有成功返回才置 cancelSent——失败保持「未撤」, 下一轮（pollInterval）继续
// 重试: 一个活着但没撤掉的挂单会继续吃进 rem ≤ RemMin 之后的成交, 那是策略不要
// 的样本（见类型 doc）。日志节流, 但每次都重试。
func (t *FillTracker) cancel(o *trackedOrder, now time.Time) {
	o.cancelTried = true
	if _, err := t.client.CancelOrder(&orders.OrderPayload{OrderID: o.orderID}); err != nil {
		if now.Sub(o.lastCnclLog) >= fillErrLogEvery {
			o.lastCnclLog = now
			log.Printf("⚠️ [FillTracker] 撤单失败 %s（已成交 %.2f/%.2f 股, %v 后重试）: %v",
				o.orderID, o.lastMatched, o.reqShares, t.pollInterval, err)
		}
		return
	}
	o.cancelSent = true
	o.canceledAt = now
	log.Printf("[FillTracker] 🛑 已撤未成交余量 %s（rem ≤ %.0fs）: 累计成交 %.2f/%.2f 股",
		o.orderID, t.cancelLead.Seconds(), o.lastMatched, o.reqShares)
}

// finalize 定稿一笔挂单（末次观测即终值; 成交价按限价, 见类型 doc）。
func (t *FillTracker) finalize(o *trackedOrder, why string) flip.FillFinal {
	shares := o.lastMatched
	if !o.sighted {
		// 从未观测到该单: 成交与否无从确认（重启接管 / 接口一直失败）——保持
		// resting 交人工核对, 不按 0 成交记账
		return t.finalizeSuspect(o, why+"，从未观测到该单", false)
	}
	status := flip.ExecStatusPartial
	switch {
	case shares <= 0:
		status = flip.ExecStatusUnfilled
	case shares >= o.reqShares-0.005:
		status = flip.ExecStatusFilled
	}
	note := fmt.Sprintf("GTC 挂单终态（%s）: 成交 %.2f/%.2f 股, 成本按限价 %.3f 记（cost=%.4f）",
		why, shares, o.reqShares, o.limit, shares*o.limit)
	if o.cancelSent {
		note += fmt.Sprintf("; 未成交余量已撤（rem ≤ %.0fs 主动撤单）", t.cancelLead.Seconds())
	}
	return flip.FillFinal{
		ConditionID: o.conditionID,
		Shares:      shares,
		Cost:        shares * o.limit,
		Status:      status,
		Note:        note,
	}
}

// finalizeSuspect 定稿一笔**无法确认成交量**的挂单: 行保持 resting（不入 pending、
// 不注册结算）, 只把原因写进 ExecNote 并打 ExecNoteUnknown 前缀 → 重启扫描与
// NeedsReconcile 继续算它待人工核对。suspicious=true 时补一句字段可疑说明。
func (t *FillTracker) finalizeSuspect(o *trackedOrder, why string, suspicious bool) flip.FillFinal {
	extra := ""
	if suspicious {
		extra = "（size_matched 字段语义可疑）"
	}
	note := fmt.Sprintf("%s: GTC 挂单终态未确认%s——%s; 请按 order_id 去 data-api / UI 核对该窗实际成交",
		flip.ExecNoteUnknown, extra, why)
	log.Printf("⚠️ [FillTracker] %s window=%s slug=%s order=%s: %s",
		flip.ExecNoteUnknown, o.conditionID, o.slug, o.orderID, why)
	return flip.FillFinal{ConditionID: o.conditionID, Status: flip.ExecStatusResting, Note: note}
}

// normalizeMatched 归一 size_matched 单位。
// 该字段应为股数; 若观测值超过请求股数, 它必是 1e6 基单位（订单成交不可能超过
// 下单量, CLOB 强制）——除法是确定性修正, 并打日志留痕。仍越界的交调用方处理。
func normalizeMatched(matched, reqShares float64) float64 {
	if matched > reqShares+0.005 && matched/1e6 <= reqShares+0.005 {
		log.Printf("[FillTracker] ⚠️ size_matched=%v 超过请求 %.2f 股 → 判为 1e6 基单位, 除回 %.4f 股",
			matched, reqShares, matched/1e6)
		return matched / 1e6
	}
	return matched
}
