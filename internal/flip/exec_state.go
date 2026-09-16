package flip

import (
	"fmt"
	"log"
	"strings"
	"time"
)

// ExecState 是信号成交编排——engine 产出的 ok/否决观测 → 落盘/成交的唯一路径。
// 本包承载编排（判定/记录/执行契约与编排同域, 见 exec.go——接口契约也在此处）;
// 具体成交实现经 Ex 注入: PaperExecutor（本包）/ trading.LiveExecutor（SDK 依赖层,
// 单向依赖本包, 由 cmd/flip main 在 live 分流时构造注入）。
//
// paper 与 live 共享同一形态、同一执行契约（Executor + ExecResult）:
//   - 判定/成交计算/P&L 公式全部同源（engine 状态机 + recorder Resolve）,
//     mode 永不进入判定;
//   - Ex 实现即唯一差异点——paper: PaperExecutor（不真实 POST, 模拟全额 filled）;
//     live: LiveExecutor（真实 FAK 下单 + 响应 sanity 解析）;
//   - 风控闸（gate）两模式共用同一判据（2026-09-16 起）: live 命中即拦下 POST
//     （rejected 行, 无持仓）; paper 命中只写 gate_reason（方案 A, plan §3.5）
//     ——「被闸」同样是真实市场状态, 纸面砍掉会丢失反事实与频率口径;
//   - live 的资金安全附加步骤（submitting 先行落盘 + 终态回填）只出现在 live
//     分支——submitting 中间态是「真实订单可能已受理」的现实语义（进程崩溃需
//     重启人工核对）; 纸面无资金风险, 走它会自欺地模拟崩溃恢复, 并给 OOS 镜像行
//     （Record 不写 exec 字段 = 09-15 回测对照基准）加无谓噪音。
//
// 因此「纸面/实盘唯一逻辑差异 = 是否真实 POST（是否真有持仓）」在执行层成立——
// live 分支多出的是资金安全编排 + 首窗禁单, 不是另一套判定/记账口径
// （详见 exec.go Executor 契约）。
type ExecState struct {
	Rec          *Recorder               // 观测/执行记录器（构造后不变）
	Ex           Executor                // 唯一成交入口: PaperExecutor / LiveExecutor（构造后不变）
	Live         bool                    // Ex 为真实下单实现（= 构造时模式 live, 构造后不变）
	Stake        float64                 // 每信号投入（构造后不变）
	MaxDailyLoss float64                 // 日亏熔断线（构造后不变; 两模式同判据, 见 gate）
	FirstWindow  bool                    // 重启后首窗禁单（live）: 主循环首个完整窗口跑完后置 false
	Tokens       func() (string, string) // 窗口 UP/DOWN token（live 下单取狗侧 tokenID; paper 忽略）
}

// HandleObservation 处理引擎产出的触底观测（成功与失败都落盘，频率校准用）。
//
// ok 信号编排（判定后立即落盘，行级 flush，崩溃不丢）——单一路径、Ex 一步分叉:
//   - 风控闸（两模式同判据, 见 gate）: 命中 → live 记 rejected 行 / paper 记
//     同 schema 观测行 + gate_reason（方案 A）, 均不进入下单段;
//   - paper: 统一 Execute（PaperExecutor 模拟全额成交）→ RecordObservation
//     单行落盘——paper 行无 exec 字段，行为与接入 live 前一致;
//   - live: SubmitLiveObservation 先写 exec_status=submitting 行（下单前落盘——
//     崩溃时真单可能已受理的恢复语义）→ 统一 Execute（= LiveExecutor 真实
//     FAK 下单）→ CompleteExecution 回填终态（filled/partial/unfilled/rejected
//   - 实际 fill/cost/order_id，原子重写当日文件）。
//
// 拦截/未成交/失败均不重试（引擎已 Done，事件内不再检测）。
//
// 返回落盘记录（落盘失败返回 nil）。结算轮询注册由调用方按
// rec.OK && rec.IsFilled() 决定——live 未成交/被拒行不注册（无持仓无结算）。
func (x *ExecState) HandleObservation(o *Observation, conditionID, slug string, eventStart int64) *Record {
	// done 包装落盘: 失败记日志并返回 nil（调用方按 nil 不注册结算）
	done := func(rec *Record, err error) *Record {
		if err != nil {
			log.Printf("[Dog] 观测落盘失败: %v", err)
			return nil
		}
		return rec
	}

	if !o.OK {
		log.Printf("[Event] 触底否决 side=%s rem=%ds fill=%.3f m45=%.2f dist_s=%.2f reason=%s anchor=%.2f σ=%.2f spot=%.2f",
			o.Side, o.Rem, o.Fill, o.M45, o.DistS, o.RejectReason, o.Anchor, o.HistBps, o.Spot)
		return done(x.Rec.RecordObservation(conditionID, slug, eventStart, o, x.Stake))
	}
	log.Printf("[Event] 🎯 触底信号 side=%s rem=%ds fill=%.3f m20=%.2f m30=%.2f m45=%.2f dist_s=%.2f dist_t=%.2f shares=%.1f anchor=%.2f σ=%.2f spot=%.2f",
		o.Side, o.Rem, o.Fill, o.M20, o.M30, o.M45, o.DistS, o.DistT, o.Shares, o.Anchor, o.HistBps, o.Spot)

	// ── 风控闸（逐笔现算: paper/live 同一判据、同一顺序）──
	// 两模式同源（方案 A, docs §3.5）: 唯一差别是 live 的闸拦下真实 POST、
	// paper 的闸只写 gate_reason（行照记照结算, 可回溯反事实）。
	if reason, blocked := x.gate(); blocked {
		return x.gatedRecord(reason, conditionID, slug, eventStart, o)
	}

	// ── live 资金安全前置段（paper 无两阶段: 见类型 doc）──
	if x.Live {
		// 阶段 1: submitting 行先落盘（POST 后崩溃 → 重启扫描告警人工核对, 不自动补单）
		sub, err := x.Rec.SubmitLiveObservation(conditionID, slug, eventStart, o, x.Stake)
		if err != nil {
			return done(sub, err)
		}
	}

	// ── 统一执行入口（唯一分叉点: Ex 实现——是否真实 POST; tokenID 仅 live 需要）──
	tokenID := ""
	if x.Live {
		upTok, downTok := x.Tokens()
		tokenID = downTok
		if o.Side == SideYes {
			tokenID = upTok // 买狗侧 token（UP=yes / DOWN=no; 窗口内必有, 极端缺失由 Execute 拒单兜底）
		}
	}
	start := time.Now()
	res := x.Ex.Execute(o, tokenID, x.Stake)

	// ── live 阶段 2: 回填终态（filled/partial 入 pending → 调用方按 IsFilled 注册结算）──
	if x.Live {
		rec, _, err := x.Rec.CompleteExecution(conditionID, *res)
		if err != nil {
			log.Printf("[Trading] ⚠️ 执行回填失败: %v —— submitting 行残留, 重启扫描会告警人工核对", err)
			return nil
		}
		note := ""
		if res.Note != "" {
			note = " note=" + res.Note
		}
		log.Printf("[Trading] 🎯 live 订单终态: exec=%s order=%s fill=%.3f shares=%.1f cost=%.2f%s（%dms）",
			res.Status, res.OrderID, res.FillPrice, res.Shares, res.Cost, note, time.Since(start).Milliseconds())
		if strings.HasPrefix(res.Note, ExecNoteUnknown) {
			log.Printf("[Trading] ⚠️ 订单结果未知（POST 可能已受理）—— 请按 maker+order=%s 时间窗去 data-api 核对, 勿重复下单", res.OrderID)
		}
		return rec
	}

	// ── paper: 模拟成交后单行落盘（无 exec 字段, schema 与回测镜像 1:1）──
	if res.Status == ExecStatusRejected {
		log.Printf("[Trading] ⚠️ 信号未执行: %s（仍记录观测）", res.Note)
	}
	return done(x.Rec.RecordObservation(conditionID, slug, eventStart, o, x.Stake))
}

// gate 风控闸（模式无关的判据与顺序, docs §3.3）: 返回命中原因与是否拦截。
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

// breakerTripped 日亏熔断是否生效（当日锁存, docs §3.4）: 当日已结算 P&L ≤ 线,
// **或**当日已有被闸行——后者是锁存: paper 方案 A 下被闸行照常结算, 累积 P&L
// 可能回升过线（live 同样有在途单结算回填）, 无锁存则当日自动复牌。
// 两个判据都取磁盘真相（DailyPnl/GatedOn 现算）, 重启自动恢复、UTC 跨日自动归零。
func (x *ExecState) breakerTripped() bool {
	today := utcToday()
	if x.Rec.GatedOn(today, GateDailyLoss) {
		return true
	}
	return !CanTrade(x.todaySettledPnl(), x.MaxDailyLoss)
}

// gatedRecord 落盘一个被闸信号（两模式共用入口, 唯一差别 = 是否真实持仓）:
// live → rejected 行（无持仓不注册结算）; paper → 与正常信号同 schema 的观测行
// （IsFilled 仍 true, 照常注册结算 + 回填, 只多 gate_reason）。见 docs §3.3/§3.5。
func (x *ExecState) gatedRecord(reason, conditionID, slug string, eventStart int64, o *Observation) *Record {
	note := x.gateNote(reason)
	if !x.Live {
		rec, err := x.Rec.RecordGatedObservation(conditionID, slug, eventStart, o, x.Stake, reason)
		if err != nil {
			log.Printf("[Dog] 观测落盘失败: %v", err)
			return nil
		}
		log.Printf("[Trading] ⚠️ 信号被风控闸拦下（纸面: 只标记不拦 POST）: %s", note)
		return rec
	}
	rec, err := x.Rec.RecordLiveRejected(conditionID, slug, eventStart, o, x.Stake, reason, note)
	if err != nil {
		log.Printf("[Dog] 观测落盘失败: %v", err)
		return nil
	}
	log.Printf("[Trading] ⚠️ 信号被风控闸拦截未下单: %s（rejected 行落盘）", note)
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
// 每次从 recorder 磁盘真相现算，重启安全、跨 UTC 日自动归零（今日无结算行 = 0）。
// ⚠️ 含被闸行（方案 A: 被闸行照常结算）——所以锁存判据必须另看 GatedOn, 见 breakerTripped。
func (x *ExecState) todaySettledPnl() float64 {
	today := utcToday()
	for _, d := range x.Rec.DailyPnl() {
		if d.Date == today {
			return d.PnL
		}
	}
	return 0
}

// RiskSummary 构造日亏熔断摘要（Dashboard 风控块, 两模式都填——paper 显示"影子"角标）。
// 判据与 HandleObservation 的闸同源（breakerTripped 同一函数）, 只是多带上展示字段。
func (x *ExecState) RiskSummary() *RiskSummary {
	if x.Rec == nil {
		return nil
	}
	return &RiskSummary{
		TodayPnl:     x.todaySettledPnl(),
		MaxDailyLoss: x.MaxDailyLoss,
		CanTrade:     !x.breakerTripped(),
		GatedToday:   x.Rec.GatedToday(utcToday(), GateDailyLoss),
		Enforced:     x.Live,
	}
}

// LiveSummary 构造 live 执行摘要（Dashboard /api/state 的 live 段; paper 恒 nil）。
// 今日口径 = UTC 日——与 HandleObservation 日亏熔断闸同源（今日已结算 P&L 现算 +
// 同一熔断线）; 待核对行用重启扫描同语义的全量计数（submitting/未知结果, 跨日残留
// 仍计）。由 runtimeState.Snapshot（main 包, Dashboard goroutine）调用, 只读字段 +
// recorder 内部锁, 无竞态。
func (x *ExecState) LiveSummary() *LiveExec {
	if !x.Live {
		return nil
	}
	ex := &LiveExec{MaxDailyLoss: x.MaxDailyLoss, Reconciling: x.Rec.NeedsReconcile()}
	today := utcToday()
	for _, rec := range x.Rec.Observations() {
		switch rec.ExecStatus {
		case ExecStatusFilled, ExecStatusPartial:
			if rec.Date == today {
				ex.TodayFilled++
			}
		}
		if rec.Date == today && rec.OK && rec.Won != nil {
			ex.TodayPnl += rec.PnL
		}
	}
	// BreakerOpen 语义 = 「可开单」（历史字段, 见 LiveExec 注释）: 含当日锁存——
	// 与 HandleObservation 的闸同源, 否则 Dashboard 会显示"可开单"但实际已停单。
	ex.BreakerOpen = !x.breakerTripped()
	return ex
}
