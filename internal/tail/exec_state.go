package tail

import (
	"fmt"
	"log"
	"strings"
	"time"

	"github.com/necklace/flip-signal/internal/flip"
)

// ExecState 是行的落盘/成交编排——engine 产出的行 → 落盘/下单的唯一路径。
//
// 镜像 flip.ExecState 的形态（engine 判定 / recorder 记账 / executor 成交三段分离,
// 编排只有一处）, 但只保留本族需要的那部分: 三段链产出的行**形态完全一致**
// （Kind=KindSnap, 由 Stage 区分）, 故只有**一个**入口 HandleDecision——
// 判定行（ok=false）只落盘, 信号行（ok=true）才进风控闸与执行。
//
// 纸面/实盘同源:
//   - 判定/股数/P&L 公式全部同源（engine + recorder.recomputePnL）, mode 永不进入判定;
//   - Ex 是唯一差异点——paper: flip.PaperExecutor（模拟全额成交）;
//     live: trading.LiveExecutor（真实 GTC 限价挂单）;
//   - 风控闸两模式共用同一判据, 且 2026-09-24 起**后果也统一**: 被闸 = rejected
//     （无仓位 → 未成交, 不进胜率与 P&L）。flip 侧仍是方案 A, 本族已改（a.md 第 3/4 条）;
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
	Sell         flip.SellExecutor       // **止损卖出**入口: PaperSellExecutor / trading.LiveSellExecutor（决策 #34; 构造后不变, live 时不得为空）
	Live         bool                    // Ex 为真实下单实现（= 构造时模式 live）
	Stake        float64                 // 每信号投入（构造后不变）
	MaxDailyLoss float64                 // 日亏熔断线（构造后不变; 两模式同判据, 见 gate）
	Tokens       func() (string, string) // 窗口 UP/DOWN token（live 下单/卖出取对应侧 tokenID; paper 忽略）
}

// execObs 把本包的快照观测折叠成 flip.Executor 契约所需的最小观测
// （Fill = 热门侧有效价即下单限价; Shares = 目标股数）——见类型注释。
func execObs(o *Observation) *flip.Observation {
	return &flip.Observation{Fill: o.HotAsk, Shares: o.Shares}
}

// HandleDecision 处理引擎产出的一行（判定行与信号行共用本入口）。
//
// **绝不下单的两条防线**（都写在最前面）:
//   - 非 snap 行（legacy 帧/对账行）拒绝执行——唯一可能把无仓位的东西下成真单的地方;
//   - 本窗已有 OK 行（Recorder.HasSignal）时拒绝再下单——三段链本身保证「出信号即
//     Done」, 这里是落盘层的冗余守卫（重启重入/未来改动引入第二路径时兜住双单）。
//
// 落盘路径（判定行成功与否都落盘, 行级 flush, 崩溃不丢）——单一路径、Ex 一步分叉:
//   - ok=false: 只落盘（RejectReason 分解, 供频率校准）;
//   - 风控闸（两模式同判据）: 命中 → rejected 行（无仓位, 照常注册结算以便页面显示
//     结果, 但 P&L 恒 0）——paper 与 live 同一条路径, 见 RecordRejected;
//   - paper: 统一 Execute（模拟全额成交）→ RecordObservation 单行落盘;
//   - live: SubmitLiveObservation 先写 submitting 行 → Execute（真实 GTC 挂单）→
//     CompleteExecution 回填即时结果。⚠️ GTC 可能只到 resting（挂单在簿、成交量
//     未定）——定稿在 trading.FillTracker（撤单点 = **闭市**, 见 trading.CancelAtClose）,
//     经 ApplyFillFinal 回填终态。
//
// 拦截/未成交/失败均不重试（引擎已 Done, 本窗不再检测）。
// 返回落盘记录（落盘失败返回 nil）。结算注册由调用方按 rec.OK 决定;
// resting 行不满足 isSettlable, 由调用方改交 FillTracker 定稿后再注册。
func (x *ExecState) HandleDecision(o *Observation, conditionID, slug string, eventStart int64) *Record {
	done := func(rec *Record, err error) *Record {
		if err != nil {
			log.Printf("[Tail] 观测落盘失败: %v", err)
			return nil
		}
		return rec
	}

	if o.Kind != KindSnap {
		log.Printf("⚠️ [Tail] HandleDecision 收到非快照行（kind=%q）——拒绝执行, 请检查调用点", o.Kind)
		return nil
	}
	if o.OK && x.Rec.HasSignal(conditionID) {
		log.Printf("⚠️ [Tail] %s 本窗已有 OK 行, 拒绝重复下单（stage=%s rem=%d）——请检查是否有进程重入", conditionID, o.Stage, o.Rem)
		return nil
	}

	if !o.OK {
		log.Printf("[Tail] 判定否决 stage=%s side=%s rem=%ds hot_ask=%.3f dev=%.1f sd=%.1f reason=%s anchor=%.2f σ=%.2fbps spot=%.2f",
			o.Stage, o.Side, o.Rem, o.HotAsk, o.Dev, o.Sd, o.RejectReason, o.Anchor, o.HistBps, o.Spot)
		return done(x.Rec.RecordObservation(conditionID, slug, eventStart, o, x.Stake))
	}
	log.Printf("[Tail] 🎯 尾盘信号 stage=%s side=%s rem=%ds hot_ask=%.3f(%s) dev=%.1f sd=%.1f shares=%.2f anchor=%.2f σ=%.2fbps spot=%.2f rules=%+v",
		o.Stage, o.Side, o.Rem, o.HotAsk, o.HotSrc, o.Dev, o.Sd, o.Shares, o.Anchor, o.HistBps, o.Spot, o.Rules)
	if o.HotSrc == BookSrcBid {
		log.Printf("[Tail] ⚠️ 该侧 ask 为空, 按 bid %.3f 挂单（live 成交概率低, 见 docs/tail_integrated_2026-09-24.md §2）", o.HotAsk)
	}

	// ── 风控闸（逐笔现算: paper/live 同一判据、同一后果）──
	if reason, blocked := x.gate(); blocked {
		return x.rejectedRecord(reason, conditionID, slug, eventStart, o)
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
// size_matched 后经回调送到这里, 见其类型 doc）。返回值同 HandleDecision。
// 调用方据此让该行进待结算队列——入队时点从「POST 返回」推到「挂单定稿」,
// 而定稿在闭市后十几秒内（撤单点 = 闭市, 见决策 #17）, 早于结算编排 ① 层的 +10s 闸,
// 口径不受影响（三层判据只看时钟, 不看入队时刻）。
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

// ── 止损卖出编排（2026-10-03 决策 #34）──

// StopOutcome 一次止损尝试的结果形态（调用方据此决定日志节流与后续动作）。
type StopOutcome int

const (
	// StopNone 未发生任何动作（未建仓/已冻结/已卖完——调用方已做过触发判定,
	// 这里是防线）。
	StopNone StopOutcome = iota
	// StopSold 本次卖出委托成交（可能只是部分）——已落盘。剩余仓位若还低于阈值,
	// 下一 tick 会自然再卖（调用方无需额外动作）。
	StopSold
	// StopRetry 本次 FAK 委托未成交（吃不到对手方）——**不落盘不冻结**, 下一 tick
	// 重试（触发条件仍满足时）。
	StopRetry
	// StopFrozen 已冻结点位（下单被拒/结果未知/剩余不足最小单量/落盘失败）——
	// 不再重试, ExitNote 已写（以 flip.ExecNoteUnknown 开头者需人工核对）。
	StopFrozen
)

// StopSell 执行一次止损卖出尝试（**开仓之外唯一的真钱出口**, 决策 #34）。
//
// 调用契约（cmd/tail 主循环）: 只在「本窗有已确认仓位 ∧ 本 tick 过 StopArmed 门控 ∧
// StopTrigger(cfg, 持仓侧 bid) 为真」时调用——本函数不重复做触发判定（唯一口径在
// stop.go 的纯函数里）, 只负责编排「下单 → 落盘 → 冻结」。
//
// 各分支:
//   - 已冻结（ExitNote 非空）/ 已卖完 / 无仓位 → StopNone（静默防线）;
//   - live 且剩余 < MinSellShares（CLOB 最小单量）→ **冻结**（卖不掉的残仓只能等
//     到期兑付, 反复尝试只会被交易所拒——写一次 note 交人工; paper 不套这条:
//     纸面本就不模拟交易所微观约束, 且默认 stake=2 时整仓都不足 5 股,
//     套用它会让纸面止损永远不成交、丧失观测价值）;
//   - filled/partial → Recorder.RecordExit 立即落盘（加权平均累计）→ StopSold;
//   - unfilled → StopRetry（FAK 吃不到就下一 tick 再来, 不写任何盘）;
//   - rejected（含 ExecNoteUnknown 前缀的**结果未知**）→ 冻结 + note → StopFrozen。
//     未知结果绝不重试（可能已成交, 重试 = 卖两次）; 已知拒单（4xx/规格错）重试
//     也不会成功, 同样是冻结。
//
// ⚠️ 卖出落盘失败（磁盘错）时**冻结而不重试**: 交易所那笔可能已成交, 再次下单
// 就是双卖——宁可留一条 note 交人工。内存行的 ExitShares 通常已更新（RecordExit
// 先改内存后重写磁盘）, 且结算时的 Resolve 重写会把它补落盘。
func (x *ExecState) StopSell(rec *Record, bid float64, rem int, ts int64) StopOutcome {
	if rec == nil || !rec.HasPosition() || rec.ExitNote != "" {
		return StopNone
	}
	remaining := rec.RemainingShares()
	if remaining <= 0 {
		return StopNone
	}
	if x.Sell == nil {
		// 构造点漏注入的 wiring bug: 大声报但不臆造成交（每 tick 一条, 一眼可见）
		log.Printf("⚠️ [Tail] ExecState.Sell 未注入——止损无法执行（%s 剩余 %.2f 股, bid=%.3f）",
			rec.ConditionID, remaining, bid)
		return StopNone
	}
	if x.Live && remaining < MinSellShares {
		note := fmt.Sprintf("止损剩余 %.2f 股 < CLOB 最小单量 %.0f 股, 无法卖出（买回/对冲请人工处理）",
			remaining, MinSellShares)
		x.freezeExit(rec, note)
		log.Printf("[Tail] ⚠️ 止损冻结: %s %s", rec.ConditionID, note)
		return StopFrozen
	}

	tokenID := ""
	if x.Live {
		upTok, downTok := x.Tokens()
		tokenID = downTok
		if rec.Side == flip.SideYes {
			tokenID = upTok // 卖出**所押侧** token（与买入同一条腿）
		}
	}
	start := time.Now()
	res := x.Sell.Sell(tokenID, remaining, bid)
	if res == nil {
		// 执行器契约是永不返回 nil——真返回了按结果未知处理（绝不重试）
		note := flip.ExecNoteUnknown + ": SellExecutor 返回 nil（无任何结果描述）"
		x.freezeExit(rec, note)
		log.Printf("⚠️ [Tail] 止损卖出结果未知: %s %s", rec.ConditionID, note)
		return StopFrozen
	}

	switch res.Status {
	case flip.ExecStatusFilled, flip.ExecStatusPartial:
		rec2, err := x.Rec.RecordExit(rec.ConditionID, res.Shares, res.Price, rem, ts)
		if err != nil {
			note := fmt.Sprintf("%s: 止损成交 %.2f 股 @%.4f 落盘失败（%v）——该笔已在交易所成交, 请人工核对",
				flip.ExecNoteUnknown, res.Shares, res.Price, err)
			log.Printf("⚠️ [Tail] 止损卖出落盘失败: %v（已成交 %.2f 股 @%.4f——请人工核对）", err, res.Shares, res.Price)
			x.freezeExit(rec, note)
			return StopFrozen
		}
		log.Printf("[Tail] 🛑 止损卖出 %s: %.2f 股 @%.4f（剩余 %.2f 股）rem=%ds side=%s bid=%.3f（%dms）",
			rec.ConditionID, res.Shares, res.Price, rec2.RemainingShares(), rem, rec.Side, bid, time.Since(start).Milliseconds())
		return StopSold

	case flip.ExecStatusUnfilled:
		return StopRetry

	default: // rejected（含未知结果）——一切非成交终态都冻结, 细节在 note 里
		note := res.Note
		if note == "" {
			note = fmt.Sprintf("止损卖出未成交终态 %q（无 note）", res.Status)
		}
		x.freezeExit(rec, note)
		log.Printf("[Tail] ⚠️ 止损冻结: %s %s", rec.ConditionID, note)
		return StopFrozen
	}
}

// freezeExit 写一次止损冻结说明并落盘（best effort——落盘失败只告警, 内存中的
// ExitNote 已置位 ⇒ 本进程内不会重试; 磁盘行由结算时的 Resolve 重写补上）。
func (x *ExecState) freezeExit(rec *Record, note string) {
	if _, err := x.Rec.RecordExitNote(rec.ConditionID, note); err != nil {
		log.Printf("⚠️ [Tail] 止损冻结落盘失败: %v（内存已冻结, 结算重写时会补）", err)
	}
}

// gate 风控闸: 只剩日亏熔断一条（两模式同源同后果——都落 rejected 行, 都不下单）。
//
// ⚠️ 2026-09-29 起**不再有 live 首窗禁单**（决策 #28）。理由: 「旧进程已死、新进程才起」
// 的重启路径撞不上同一市场——重启落在边界后 >lateLimit(15s) 时整窗跳过（旧进程交易过的
// 那一窗根本不重入）, ≤15s 时 join 的那一窗旧进程也来不及下单（本族最早决策点在 +150s）,
// 万一真重入还有 HasSignal/dup_record + Engine.Resume 兜住。该闸唯一还挡着的只剩
// 「两实例同时在跑」（滚动重启先起新再杀旧）——那是运维纪律问题, 不该每启一次白丢一窗信号
// （实盘 09-24~27 已挡 5 笔, 全部 ≥0.94 且全赢）。
func (x *ExecState) gate() (reason string, blocked bool) {
	if x.breakerTripped() {
		return GateDailyLoss, true
	}
	return "", false
}

// breakerTripped 日亏熔断是否生效（当日锁存）: 当日已结算 P&L ≤ 线, **或**当日已有
// 被闸行——后者是锁存: 在途单的结算回填会让累积 P&L 回升过线, 无锁存则当日自动复牌。
// 两个判据都取磁盘真相（DailyPnl/GatedOn 现算）, 重启自动恢复、UTC 跨日自动归零。
//
// ⚠️ DailyPnl 只累加**有仓位**的行（HasPosition）——未成交/被闸行 P&L 恒 0, 天然不影响。
func (x *ExecState) breakerTripped() bool {
	today := utcToday()
	if x.Rec.GatedOn(today, GateDailyLoss) {
		return true
	}
	return !flip.CanTrade(x.todaySettledPnl(), x.MaxDailyLoss)
}

// rejectedRecord 落盘一个被风控闸拦下的信号（**两模式共用一条路径**）:
// exec_status = rejected + gate_reason + note ⇒ IsFilled 为假 ⇒ 无仓位
// ⇒ 不计 P&L、不入胜率（a.md 第 4 条把「被风控拦」归入未成交）。
//
// 仍然**注册结算**: 页面要显示这一笔的官方结果（a.md 第 3 条「所有信号都要注册结算」）,
// 只是 P&L 恒 0（recorder.recomputePnL 按 HasPosition 归零）。
//
// ⚠️ 与 flip 的分歧（有意）: flip 走方案 A（paper 被闸行照常成交照常结算,
// 是「当日不熔断会怎样」的反事实样本）; tail 自 2026-09-24 起不再保留该反事实——
// 用户决定见 docs/tail_integrated_2026-09-24.md §3.3。熔断锁存判据不受影响
// （GatedOn 扫磁盘真相, 与行的成交口径无关）。
func (x *ExecState) rejectedRecord(reason, conditionID, slug string, eventStart int64, o *Observation) *Record {
	note := x.gateNote(reason)
	rec, err := x.Rec.RecordRejected(conditionID, slug, eventStart, o, x.Stake, reason, note)
	if err != nil {
		log.Printf("[Tail] 观测落盘失败: %v", err)
		return nil
	}
	mode := "纸面"
	if x.Live {
		mode = "实盘"
	}
	log.Printf("[Tail] ⚠️ 信号被风控闸拦下（%s, 记未成交）: %s", mode, note)
	return rec
}

// gateNote 闸命中原因的可读说明（进 ExecNote; 载今日已结算与线值便于事后核对）。
func (x *ExecState) gateNote(reason string) string {
	if reason == GateDailyLoss {
		return fmt.Sprintf("日亏熔断(锁存): 今日已结算 %.2fU, 线 %.2fU", x.todaySettledPnl(), x.MaxDailyLoss)
	}
	return reason
}

// todaySettledPnl 返回今日（UTC）已结算 P&L（USDC）——日亏熔断的无状态输入:
// 每次从 recorder 磁盘真相现算，重启安全、跨 UTC 日自动归零。
// ⚠️ 未成交行 P&L 恒 0（HasPosition 过滤在 DailyPnl 里）, 故熔断只看真实仓位。
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
// 判据与 HandleDecision 的闸同源（breakerTripped 同一函数）, 只是多带上展示字段。
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
//
// ⚠️ 今日 P&L 只吃**有仓位**的行（HasPosition）: 未成交/被闸行不进来, 否则 LiveSummary
// 的今日盈亏会和熔断输入（DailyPnl）对不上。
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
		if rec.Kind == KindSnap && rec.OK && rec.Won != nil && rec.HasPosition() {
			ex.TodayPnl += rec.PnL
		}
	}
	// BreakerOpen 语义 = 「可开单」（历史字段, 见 flip.LiveExec 注释）: 含当日锁存——
	// 与 HandleDecision 的闸同源, 否则 Dashboard 会显示"可开单"但实际已停单。
	ex.BreakerOpen = !x.breakerTripped()
	return ex
}
