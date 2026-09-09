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
//   - live 的资金安全附加步骤（风控闸 + submitting 先行落盘 + 终态回填）只出现在
//     live 分支——submitting 中间态/熔断/首窗禁单是「真实订单可能已受理」的现实
//     语义（进程崩溃需重启人工核对）; 纸面无资金风险, 走它会自欺地模拟崩溃恢复,
//     并给 OOS 镜像行（Record 不写 exec 字段 = 09-15 回测对照基准）加无谓噪音。
//
// 因此「纸面/实盘唯一逻辑差异 = 是否真实 POST」在执行层成立——live 分支多出的是
// 资金安全编排, 不是另一套判定/记账口径（详见 exec.go Executor 契约）。
type ExecState struct {
	Rec          *Recorder               // 观测/执行记录器（构造后不变）
	Ex           Executor                // 唯一成交入口: PaperExecutor / LiveExecutor（构造后不变）
	Live         bool                    // Ex 为真实下单实现（= 构造时模式 live, 构造后不变）
	Stake        float64                 // 每信号投入（构造后不变）
	MaxDailyLoss float64                 // live 日亏熔断线（构造后不变; paper 无意义）
	FirstWindow  bool                    // 重启后首窗禁单（live）: 主循环首个完整窗口跑完后置 false
	Tokens       func() (string, string) // 窗口 UP/DOWN token（live 下单取狗侧 tokenID; paper 忽略）
}

// HandleObservation 处理引擎产出的触底观测（成功与失败都落盘，频率校准用）。
//
// ok 信号编排（判定后立即落盘，行级 flush，崩溃不丢）——单一路径、Ex 一步分叉:
//   - paper: 无闸（恒放行）→ 统一 Execute（PaperExecutor 模拟全额成交）→
//     RecordObservation 单行落盘——paper 行无 exec 字段，行为与接入 live 前一致;
//   - live: 风控闸（重启首窗禁单 + 日亏熔断现算，拦截记 rejected 行）→
//     SubmitLiveObservation 先写 exec_status=submitting 行（下单前落盘——
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

	// ── live 资金安全前置段（paper 无闸、无两阶段: 见类型 doc）──
	if x.Live {
		gate := func(note string) *Record {
			rec, err := x.Rec.RecordLiveRejected(conditionID, slug, eventStart, o, x.Stake, note)
			if err != nil {
				return done(rec, err)
			}
			log.Printf("[Trading] ⚠️ 信号被风控闸拦截未下单: %s（rejected 行落盘）", note)
			return rec
		}
		if x.FirstWindow {
			return gate("重启后首窗禁单")
		}
		if today := x.todaySettledPnl(); !CanTrade(today, x.MaxDailyLoss) {
			return gate(fmt.Sprintf("日亏熔断: 今日已结算 %.2fU ≤ %.2fU", today, x.MaxDailyLoss))
		}
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

// todaySettledPnl 返回今日（UTC）已结算 P&L（USDC）——日亏熔断的无状态输入:
// 每次从 recorder 磁盘真相现算，重启安全、跨 UTC 日自动归零（今日无结算行 = 0）。
func (x *ExecState) todaySettledPnl() float64 {
	today := time.Now().UTC().Format("2006-01-02")
	for _, d := range x.Rec.DailyPnl() {
		if d.Date == today {
			return d.PnL
		}
	}
	return 0
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
	today := time.Now().UTC().Format("2006-01-02")
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
	ex.BreakerOpen = ex.TodayPnl > ex.MaxDailyLoss
	return ex
}
