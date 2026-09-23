package tail

import (
	"fmt"
	"log"
	"strings"
	"time"

	"github.com/necklace/flip-signal/internal/flip"
)

// ExecState 是快照的落盘/成交编排——engine 产出的行 → 落盘/下单的唯一路径。
//
// 镜像 flip.ExecState 的形态（engine 判定 / recorder 记账 / executor 成交三段分离,
// 编排只有一处）, 但只保留本族需要的那部分:
//   - **帧行**（KindFrame）走 HandleFrame: 只落盘, 永不下单;
//   - **决策快照**（KindSnap）走 HandleObservation: 落盘 → 风控闸 → 执行 → 回填;
//   - **监听对账行**（KindScan）走 HandleScan: 只落盘（不过闸、不碰 Ex）, 见其注释。
//
// 纸面/实盘同源:
//   - 判定/股数/P&L 公式全部同源（engine + recorder.Resolve）, mode 永不进入判定;
//   - Ex 是唯一差异点——paper: flip.PaperExecutor（模拟全额成交）;
//     live: trading.LiveExecutor（真实 GTC 限价挂单）;
//   - 风控闸两模式共用同一判据（live 命中拦下真实 POST 记 rejected; paper 命中只写
//     gate_reason 且行照记照结算, 方案 A）;
//   - live 的资金安全附加步骤（submitting 先行落盘 + 终态回填）只在 live 分支。
//
// ⚠️ 与 flip 的一处形制差异: `flip.Executor` 的入参是 `*flip.Observation`, 而本包
// 有自己的 Observation（字段完全不同）。复用的依据是**接口契约本身很窄**——它只读
// `Fill`（限价）与 `Shares`（目标股数）:
//   - PaperExecutor 读 Fill/Shares; trading.OrderSpecForObs 只读 Fill。
//
// 故 execObs 造一个只填这两个字段的 flip.Observation 交给同一批执行器实现——
// **不新增执行路径**, live 的 GTC 语义/撤单/FillTracker 全部沿用 flip 的已验证代码。
type ExecState struct {
	Rec          *Recorder               // 观测/执行记录器（构造后不变）
	Ex           flip.Executor           // 唯一成交入口: PaperExecutor / LiveExecutor（构造后不变）
	Live         bool                    // Ex 为真实下单实现（= 构造时模式 live）
	Stake        float64                 // 每信号投入（构造后不变）
	MaxDailyLoss float64                 // 日亏熔断线（构造后不变; 两模式同判据, 见 gate）
	FirstWindow  bool                    // 重启后首窗禁单（live）: 主循环首个完整窗口跑完后置 false
	Tokens       func() (string, string) // 窗口 UP/DOWN token（live 下单取热门侧 tokenID; paper 忽略）
}

// execObs 把本包的快照观测折叠成 flip.Executor 契约所需的最小观测
// （Fill = 热门侧 ask 即下单限价; Shares = 目标股数）——见类型注释。
func execObs(o *Observation) *flip.Observation {
	return &flip.Observation{Fill: o.HotAsk, Shares: o.Shares}
}

// HandleFrame 落盘一条原始快照帧（rem ≤ FrameRem 的首个有效 tick）。
//
// **绝不下单**: 帧只是记录（策略本体是尾盘的决策快照）, 且帧行不带判定。
// 重入去重: 同窗已有帧行时跳过——崩溃重启会让引擎在新进程里重新闩锁取帧,
// 不查磁盘就会在同一窗写下第二条 frame（同一个窗口三条行, 分析时要额外去重）。
//
// 返回落盘记录（跳过/落盘失败返回 nil）。
func (x *ExecState) HandleFrame(o *Observation, conditionID, slug string, eventStart int64) *Record {
	if o.Kind != KindFrame {
		log.Printf("⚠️ [Tail] HandleFrame 收到非帧行（kind=%q）——按帧落盘, 请检查调用点", o.Kind)
	}
	if x.Rec.HasKind(conditionID, KindFrame) {
		log.Printf("[Tail] 帧行已存在, 跳过重写: condition=%s slug=%s rem=%d", conditionID, slug, o.Rem)
		return nil
	}
	// stake 传 0: 帧行没有仓位语义（omitempty → 行里不出现该键）。
	rec, err := x.Rec.RecordObservation(conditionID, slug, eventStart, o, 0)
	if err != nil {
		log.Printf("[Tail] 帧行落盘失败: %v", err)
		return nil
	}
	log.Printf("[Tail] 📸 帧 rem=%ds hot=%s ask=%.3f dev=%.1f sd=%.1f spot=%.2f anchor=%.2f σ=%.2fbps",
		o.Rem, o.Side, o.HotAsk, o.Dev, o.Sd, o.Spot, o.Anchor, o.HistBps)
	return rec
}

// HandleScan 落盘一条监听口径对账行（决策快照之后第一个 ⑤ 达标的 tick）。
//
// **绝不下单、也不过风控闸**: 本行是一条反事实样本（「若把一次快照改成持续监听,
// 会在哪里成交」）, 不是仓位——碰 Ex 就等于开了一笔策略里不存在的单。不过闸是为了
// 与对照物可比: 回测的监听口径 B 是在**没有熔断器**的 14 天回放里算的, 施加熔断会
// 让纸面样本与它不同分布。
//
// 仍然要**结算**（落盘即进待结算队列, 由 settle.Resolver 三层回退取官方 outcome,
// 决策 #19）: 只有官方 outcome 能把这条反事实算成 P&L, 而 snap 被拒的窗没有任何别的
// 可结算行（见 recorder.isSettlable）。
//
// 重入去重同 HandleFrame: 同窗已有 scan 行时跳过（崩溃重启会让引擎在新进程里重新
// 闩锁, 不查磁盘就会写重）。stake 记账 = x.Stake（P&L 公式需要它, 与回测 STAKE 同源;
// 行里 Shares 是假想股数）——它**不是**真实投入, 见 Record.Stake 的类型语义。
//
// 返回落盘记录（跳过/落盘失败返回 nil）。
func (x *ExecState) HandleScan(o *Observation, conditionID, slug string, eventStart int64) *Record {
	if o.Kind != KindScan {
		log.Printf("⚠️ [Tail] HandleScan 收到非监听行（kind=%q）——拒绝落盘, 请检查调用点", o.Kind)
		return nil
	}
	if x.Rec.HasKind(conditionID, KindScan) {
		log.Printf("[Tail] 监听行已存在, 跳过重写: condition=%s slug=%s rem=%d", conditionID, slug, o.Rem)
		return nil
	}
	rec, err := x.Rec.RecordObservation(conditionID, slug, eventStart, o, x.Stake)
	if err != nil {
		log.Printf("[Tail] 监听行落盘失败: %v", err)
		return nil
	}
	log.Printf("[Tail] 👁️ 监听口径达标（只记录, 不下单） side=%s rem=%ds hot_ask=%.3f dev=%.1f sd=%.1f shares=%.2f anchor=%.2f σ=%.2fbps spot=%.2f",
		o.Side, o.Rem, o.HotAsk, o.Dev, o.Sd, o.Shares, o.Anchor, o.HistBps, o.Spot)
	return rec
}

// HandleObservation 处理引擎产出的决策快照（成功与失败都落盘）。
//
// ok 信号编排（判定后立即落盘, 行级 flush, 崩溃不丢）——单一路径、Ex 一步分叉:
//   - 风控闸（两模式同判据）: 命中 → live 记 rejected 行 / paper 记同 schema 观测行
//   - gate_reason（方案 A）, 均不进入下单段;
//   - paper: 统一 Execute（模拟全额成交）→ RecordObservation 单行落盘;
//   - live: SubmitLiveObservation 先写 submitting 行 → Execute（真实 GTC 挂单）→
//     CompleteExecution 回填即时结果。⚠️ GTC 可能只到 resting（挂单在簿、成交量
//     未定）——定稿在 trading.FillTracker（撤单点 = **闭市**, 见 trading.CancelAtClose）,
//     经 ApplyFillFinal 回填终态。
//
// 拦截/未成交/失败均不重试（引擎已 Done, 本窗不再检测）。
// 返回落盘记录（落盘失败返回 nil）。结算注册由调用方按 rec.OK && rec.IsFilled() 决定;
// resting 行不满足 IsFilled, 由调用方改交 FillTracker 定稿后再注册。
func (x *ExecState) HandleObservation(o *Observation, conditionID, slug string, eventStart int64) *Record {
	done := func(rec *Record, err error) *Record {
		if err != nil {
			log.Printf("[Tail] 观测落盘失败: %v", err)
			return nil
		}
		return rec
	}

	// 防线: 帧行不得进执行路径（唯一可能把无仓位的东西下成真单的地方）。
	if o.Kind != KindSnap {
		log.Printf("⚠️ [Tail] HandleObservation 收到非快照行（kind=%q）——拒绝执行, 请检查调用点", o.Kind)
		return nil
	}

	if !o.OK {
		log.Printf("[Tail] 快照否决 side=%s rem=%ds hot_ask=%.3f dev=%.1f sd=%.1f reason=%s anchor=%.2f σ=%.2fbps spot=%.2f",
			o.Side, o.Rem, o.HotAsk, o.Dev, o.Sd, o.RejectReason, o.Anchor, o.HistBps, o.Spot)
		return done(x.Rec.RecordObservation(conditionID, slug, eventStart, o, x.Stake))
	}
	log.Printf("[Tail] 🎯 尾盘信号 side=%s rem=%ds hot_ask=%.3f dev=%.1f sd=%.1f shares=%.2f anchor=%.2f σ=%.2fbps spot=%.2f rules=%+v",
		o.Side, o.Rem, o.HotAsk, o.Dev, o.Sd, o.Shares, o.Anchor, o.HistBps, o.Spot, o.Rules)

	// ── 风控闸（逐笔现算: paper/live 同一判据、同一顺序）──
	if reason, blocked := x.gate(); blocked {
		return x.gatedRecord(reason, conditionID, slug, eventStart, o)
	}

	// ── live 资金安全前置段（paper 无两阶段）──
	if x.Live {
		if _, err := x.Rec.SubmitLiveObservation(conditionID, slug, eventStart, o, x.Stake); err != nil {
			return done(nil, err)
		}
	}

	// ── 统一执行入口（唯一分叉点: Ex 实现——是否真实 POST; tokenID 仅 live 需要）──
	tokenID := ""
	if x.Live {
		upTok, downTok := x.Tokens()
		tokenID = downTok
		if o.Side == flip.SideYes {
			tokenID = upTok // 买热门侧 token（yes → UP / no → DOWN）
		}
	}
	start := time.Now()
	res := x.Ex.Execute(execObs(o), tokenID, x.Stake)

	// ── live 阶段 2: 回填即时终态 ──
	if x.Live {
		rec, _, err := x.Rec.CompleteExecution(conditionID, *res)
		if err != nil {
			log.Printf("[Tail] ⚠️ 执行回填失败: %v —— submitting 行残留, 重启扫描会告警人工核对", err)
			return nil
		}
		note := ""
		if res.Note != "" {
			note = " note=" + res.Note
		}
		log.Printf("[Tail] 🎯 live 订单终态: exec=%s order=%s fill=%.3f shares=%.2f cost=%.2f%s（%dms）",
			res.Status, res.OrderID, res.FillPrice, res.Shares, res.Cost, note, time.Since(start).Milliseconds())
		if res.Status == flip.ExecStatusResting {
			log.Printf("[Tail] 📋 挂单在簿（成交量待定稿, 闭市撤单时定稿）: order=%s", res.OrderID)
		}
		if strings.HasPrefix(res.Note, flip.ExecNoteUnknown) {
			log.Printf("[Tail] ⚠️ 订单结果未知（POST 可能已受理）—— 请按 order=%s 去 data-api 核对, 勿重复下单", res.OrderID)
		}
		return rec
	}

	// ── paper: 模拟成交后单行落盘（无 exec 字段, schema 与回测镜像 1:1）──
	if res.Status == flip.ExecStatusRejected {
		log.Printf("[Tail] ⚠️ 信号未执行: %s（仍记录观测）", res.Note)
	}
	return done(x.Rec.RecordObservation(conditionID, slug, eventStart, o, x.Stake))
}

// ApplyFillFinal 回填一笔 GTC 挂单的终态成交（trading.FillTracker 撤单/闭市查询
// size_matched 后经回调送到这里, 见其类型 doc）。返回值同 HandleObservation。
// 调用方据此让该行进待结算队列——入队时点从「POST 返回」推到「挂单定稿」，
// 而定稿在闭市后十几秒内（撤单点 = 闭市, 见决策 #17）, 早于结算编排 ① 层的
// +25s 闸, 口径不受影响（三层判据只看时钟, 不看入队时刻）。
//
// Status=resting 是合法的「仍未确认」终态（查询失败/重启遗留从未观测到该单）:
// 行保持 resting + note 说明原因, 不入 pending、不结算、NeedsReconcile 继续计它。
func (x *ExecState) ApplyFillFinal(f flip.FillFinal) *Record {
	rec, err := x.Rec.CompleteRestingFill(f)
	if err != nil {
		log.Printf("[Tail] ⚠️ 挂单终态回填失败: %v", err)
		return nil
	}
	log.Printf("[Tail] 🎯 GTC 挂单终态: exec=%s shares=%.2f cost=%.2f fill=%.4f note=%s",
		rec.ExecStatus, rec.Shares, rec.Cost, rec.FillPrice, rec.ExecNote)
	if strings.HasPrefix(f.Note, flip.ExecNoteUnknown) {
		log.Printf("[Tail] ⚠️ 挂单成交未确认（order=%s）—— 请去 data-api 按 order_id 核对该窗实际成交, 勿重复下单", rec.OrderID)
	}
	return rec
}

// gate 风控闸（模式无关的判据与顺序）: 返回命中原因与是否拦截。
//   - first_window: 仅 live（重启防双单缝隙; paper 无真实仓位可撞, 不设）;
//   - daily_loss:   两模式同源（paper 只写 gate_reason, live 拦下 POST）。
func (x *ExecState) gate() (reason string, blocked bool) {
	if x.Live && x.FirstWindow {
		return GateFirstWindow, true
	}
	if x.breakerTripped() {
		return GateDailyLoss, true
	}
	return "", false
}

// breakerTripped 日亏熔断是否生效（当日锁存）: 当日已结算 P&L ≤ 线, **或**当日已有
// 被闸行——后者是锁存: paper 方案 A 下被闸行照常结算, 累积 P&L 可能回升过线
// （live 同样有在途单结算回填）, 无锁存则当日自动复牌。
// 两个判据都取磁盘真相（DailyPnl/GatedOn 现算）, 重启自动恢复、UTC 跨日自动归零。
func (x *ExecState) breakerTripped() bool {
	today := utcToday()
	if x.Rec.GatedOn(today, GateDailyLoss) {
		return true
	}
	return !flip.CanTrade(x.todaySettledPnl(), x.MaxDailyLoss)
}

// gatedRecord 落盘一个被闸信号（两模式共用入口, 唯一差别 = 是否真实持仓）:
// live → rejected 行（无持仓不注册结算）; paper → 与正常信号同 schema 的观测行
// （IsFilled 仍 true, 照常注册结算 + 回填, 只多 gate_reason）。
func (x *ExecState) gatedRecord(reason, conditionID, slug string, eventStart int64, o *Observation) *Record {
	note := x.gateNote(reason)
	if !x.Live {
		rec, err := x.Rec.RecordGatedObservation(conditionID, slug, eventStart, o, x.Stake, reason)
		if err != nil {
			log.Printf("[Tail] 观测落盘失败: %v", err)
			return nil
		}
		log.Printf("[Tail] ⚠️ 信号被风控闸拦下（纸面: 只标记不拦 POST）: %s", note)
		return rec
	}
	rec, err := x.Rec.RecordLiveRejected(conditionID, slug, eventStart, o, x.Stake, reason, note)
	if err != nil {
		log.Printf("[Tail] 观测落盘失败: %v", err)
		return nil
	}
	log.Printf("[Tail] ⚠️ 信号被风控闸拦截未下单: %s（rejected 行落盘）", note)
	return rec
}

// gateNote 闸命中原因的可读说明（进 ExecNote; 载今日已结算与线值便于事后核对）。
func (x *ExecState) gateNote(reason string) string {
	switch reason {
	case GateFirstWindow:
		return "重启后首窗禁单"
	case GateDailyLoss:
		return fmt.Sprintf("日亏熔断(锁存): 今日已结算 %.2fU, 线 %.2fU", x.todaySettledPnl(), x.MaxDailyLoss)
	}
	return reason
}

// todaySettledPnl 返回今日（UTC）已结算 P&L（USDC）——日亏熔断的无状态输入:
// 每次从 recorder 磁盘真相现算，重启安全、跨 UTC 日自动归零。
// ⚠️ 含被闸行（方案 A: 被闸行照常结算）——所以锁存判据必须另看 GatedOn。
func (x *ExecState) todaySettledPnl() float64 {
	today := utcToday()
	for _, d := range x.Rec.DailyPnl() {
		if d.Date == today {
			return d.PnL
		}
	}
	return 0
}

// RiskSummary 构造日亏熔断摘要（Dashboard 风控块，两模式都填——paper 显示"影子"角标）。
// 判据与 HandleObservation 的闸同源（breakerTripped 同一函数）, 只是多带上展示字段。
// 与 flip.ExecState.RiskSummary 逐字段同形（复用 flip.RiskSummary 类型, 前端共用渲染）。
func (x *ExecState) RiskSummary() *flip.RiskSummary {
	if x.Rec == nil {
		return nil
	}
	return &flip.RiskSummary{
		TodayPnl:     x.todaySettledPnl(),
		MaxDailyLoss: x.MaxDailyLoss,
		CanTrade:     !x.breakerTripped(),
		GatedToday:   x.Rec.GatedToday(utcToday(), GateDailyLoss),
		Enforced:     x.Live,
	}
}

// LiveSummary 构造 live 执行摘要（Dashboard /api/state 的 live 段; paper 恒 nil）。
// 今日口径 = UTC 日——与日亏熔断闸同源（今日已结算 P&L 现算 + 同一熔断线）; 待核对行
// 用重启扫描同语义的全量计数（submitting/未知结果, 跨日残留仍计）。
// 与 flip.ExecState.LiveSummary 同口径, 只把「已结算行」的筛选换成 snap 行
// （本族的帧行 OK 恒 false, 天然不入）。
func (x *ExecState) LiveSummary() *flip.LiveExec {
	if !x.Live {
		return nil
	}
	ex := &flip.LiveExec{MaxDailyLoss: x.MaxDailyLoss, Reconciling: x.Rec.NeedsReconcile()}
	today := utcToday()
	for _, rec := range x.Rec.Observations() {
		if rec.Date != today {
			continue
		}
		switch rec.ExecStatus {
		case flip.ExecStatusFilled, flip.ExecStatusPartial:
			ex.TodayFilled++
		}
		if rec.Kind == KindSnap && rec.OK && rec.Won != nil {
			ex.TodayPnl += rec.PnL
		}
	}
	// BreakerOpen 语义 = 「可开单」（历史字段, 见 flip.LiveExec 注释）: 含当日锁存——
	// 与 HandleObservation 的闸同源, 否则 Dashboard 会显示"可开单"但实际已停单。
	ex.BreakerOpen = !x.breakerTripped()
	return ex
}
