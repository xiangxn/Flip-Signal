package flip

import (
	"log"
	"time"

	"github.com/necklace/flip-signal/internal/lab"
)

// pendingCrossing 记录盲窗期间（确认等待中）检测到的穿越，
// 供当前穿越失败后逐个尝试，消除流式引擎与 Python 批量回测之间的差异。
type pendingCrossing struct {
	crossIdx int
	side     string
}

// T0Features 保存穿越时刻（T=0）计算的特征值。
// 导出供 Dashboard 展示穿越点详情。
type T0Features struct {
	Side           string  `json:"side"`
	RangeExpansion float64 `json:"range_expansion"`
	BTCPosition    float64 `json:"btc_position"`
	BtcDivergence  float64 `json:"btc_divergence"` // 背离度：正=BTC 与 PM 反向

	// 评分阶段特征（仅在 onConfirmed 中填充，失败穿越诊断用）
	OtherDelta       float64 `json:"other_delta,omitempty"`
	EntryPrice       float64 `json:"entry_price,omitempty"`
	Score            int     `json:"score,omitempty"`
	OrderBookLatency int64   `json:"order_book_latency,omitempty"` // 穿越时刻订单簿延迟（毫秒）
}

// Engine 从 ResearchSnapshot 流中检测翻转信号。
//
// 多穿越重试：每个向上穿越 0.7 的上升沿都触发观测；首个通过评分的穿越获胜。
// 每事件最多一注，YES 侧优先。
//
// 每轮市场周期的使用方式：
//
//	engine.Reset(gen)
//	for each snapshot:
//	    sig := engine.ProcessSnapshot(snap, gen)
//	    if sig != nil { ... }
type Engine struct {
	cfg       FlipConfig
	histRange *HistRangeTracker
	state     flipState

	snapBuffer []*lab.ResearchSnapshot // 当前周期全部 snapshot（持续追加）
	crossSnap  *lab.ResearchSnapshot   // 当前穿越时刻的 snapshot
	crossSide  string                  // "yes" 或 "no"
	crossIdx   int                     // 当前穿越点在 snapBuffer 中的下标

	confirmCount int  // 已等待的 Confirming tick 数
	generation   int64
	doneThisGen  bool

	// 上升沿检测（≤阈值 → >阈值）
	yesWasAbove bool // 上一 snapshot 中 YES 是否 > 阈值
	noWasAbove  bool // 上一 snapshot 中 NO 是否 > 阈值

	// 多穿越重试追踪
	retryCount   int        // 本周期内进入 Confirming 的次数
	lastFailedT0 *T0Features // 最近一次失败穿越的 T=0 特征（Dashboard 展示用）

	// 盲窗期间记录的穿越（确认等待期间上升沿，当前穿越失败后逐个尝试）
	pendingCrossings []pendingCrossing

	// 最近一次失败穿越的评分数据（onConfirmed 中保存，诊断用）
	lastFailedOtherDelta float64
	lastFailedEntryPrice float64
	lastFailedScore      int

	// T=0 特征值（enterConfirming 中计算，onConfirmed 中读取）
	rangeExpansion   float64
	btcPosition      float64
	btcDivergence    float64
	orderBookLatency int64 // 穿越时刻订单簿延迟（毫秒），用于风控
}

// NewEngine 创建一个新的翻转检测引擎。
func NewEngine(cfg FlipConfig, histRange *HistRangeTracker) *Engine {
	return &Engine{
		cfg:       cfg,
		histRange: histRange,
		state:     stateIdle,
	}
}

// Reset 为新一轮市场周期重置引擎状态。
func (e *Engine) Reset(generation int64) {
	e.state = stateWatching
	e.snapBuffer = e.snapBuffer[:0]
	e.crossSnap = nil
	e.crossSide = ""
	e.crossIdx = 0
	e.confirmCount = 0
	e.generation = generation
	e.doneThisGen = false

	e.yesWasAbove = false
	e.noWasAbove = false

	e.retryCount = 0
	e.lastFailedT0 = nil
	e.pendingCrossings = e.pendingCrossings[:0]
	e.lastFailedOtherDelta = 0
	e.lastFailedEntryPrice = 0
	e.lastFailedScore = 0

	e.rangeExpansion = 0
	e.btcPosition = 0
	e.btcDivergence = 0
	e.orderBookLatency = 0
}

// ProcessSnapshot 处理一个 ResearchSnapshot。满足全部条件时返回 FlipSignal，否则返回 nil。
//
//   - 对 YES 和 NO 两侧做上升沿检测（≤阈值 → >阈值）。
//   - 每个上升沿触发观测；首个通过全部前置检查且评分达标的穿越获胜。
//     一旦下注，本周期终止。
//   - 确认失败时，检查盲窗期间记录的另一侧穿越并尝试之；
//     若无则回到 Watching 状态。
func (e *Engine) ProcessSnapshot(snap *lab.ResearchSnapshot, gen int64) *FlipSignal {
	if gen != e.generation || e.doneThisGen {
		return nil
	}

	// 始终追加到 buffer，保留完整周期历史。
	bufIdx := len(e.snapBuffer)
	e.snapBuffer = append(e.snapBuffer, snap)

	// 两侧上升沿检测（MinRemainingSec < remaining_sec < MaxRemainingSec）
	yesIsAbove := snap.YesPrice > e.cfg.TriggerThreshold &&
		snap.RemainingSec < e.cfg.MaxRemainingSec &&
		snap.RemainingSec > e.cfg.MinRemainingSec
	noIsAbove := snap.NoPrice > e.cfg.TriggerThreshold &&
		snap.RemainingSec < e.cfg.MaxRemainingSec &&
		snap.RemainingSec > e.cfg.MinRemainingSec

	yesRisingEdge := yesIsAbove && !e.yesWasAbove
	noRisingEdge := noIsAbove && !e.noWasAbove

	e.yesWasAbove = yesIsAbove
	e.noWasAbove = noIsAbove

	switch e.state {
	case stateIdle:
		return nil

	case stateWatching:
		// 检查上一轮 Confirming 失败后留下的 pending crossings。
		// 其确认数据可能在本 tick 刚刚到齐，优先评估（先到先服务）。
		if len(e.pendingCrossings) > 0 {
			var stillPending []pendingCrossing
			for _, pc := range e.pendingCrossings {
				if pc.crossIdx+e.cfg.ConfirmDelayTicks < len(e.snapBuffer) {
					// 确认数据已到齐
					e.retryCount++
					if sig := e.evaluateCrossingAt(pc.crossIdx, pc.side); sig != nil {
						e.state = stateDone
						e.doneThisGen = true
						e.pendingCrossings = e.pendingCrossings[:0]
						return sig
					}
					// 评估完成 → 不保留
				} else {
					// 确认数据仍未到齐 → 继续等待
					stillPending = append(stillPending, pc)
				}
			}
			e.pendingCrossings = stillPending
		}

		// 每个上升沿都尝试，同 snapshot 内 YES 优先
		if yesRisingEdge && bufIdx >= e.cfg.MinPreSnaps {
			return e.enterConfirming(bufIdx, "yes")
		}
		if noRisingEdge && bufIdx >= e.cfg.MinPreSnaps {
			return e.enterConfirming(bufIdx, "no")
		}
		return nil

	case stateConfirming:
		// 盲窗期间记录新穿越，供当前穿越失败后逐个尝试
		// （对应 Python run_backtest 批量扫描的行为）
		if yesRisingEdge && bufIdx >= e.cfg.MinPreSnaps {
			e.pendingCrossings = append(e.pendingCrossings, pendingCrossing{bufIdx, "yes"})
		}
		if noRisingEdge && bufIdx >= e.cfg.MinPreSnaps {
			e.pendingCrossings = append(e.pendingCrossings, pendingCrossing{bufIdx, "no"})
		}
		e.confirmCount++
		if e.confirmCount >= e.cfg.ConfirmDelayTicks {
			sig := e.onConfirmed(snap)
			if sig != nil {
				e.state = stateDone
				e.doneThisGen = true
				return sig
			}
			// 确认失败 → 尝试另一侧穿越，或回到 Watching
			return e.afterFailedConfirm(snap)
		}
		return nil

	case stateDone:
		return nil
	}
	return nil
}

// enterConfirming 使用指定 buffer 下标的穿越点，切换到 Confirming 状态。
// 计算 T=0 特征并应用 Formula B 硬过滤（B1 背离 + F0 真突破）。
// 本次穿越被否决不会耗尽该侧 —— 后续上升沿仍会重试。
func (e *Engine) enterConfirming(crossIdx int, side string) *FlipSignal {
	e.retryCount++ // 每次进入 Confirming 计数（多穿越模式下可能 > 1）

	crossSnap := e.snapBuffer[crossIdx]

	// 检查穿越前 snapshot 数量（含穿越点自身）。
	if crossIdx+1 < e.cfg.MinPreSnaps {
		return e.returnToWatching()
	}

	openPrice := crossSnap.OpenPrice

	// 振幅扩张（仅在历史数据就绪后计算）
	if e.histRange.IsReady() {
		e.rangeExpansion = RangeExpansion(crossSnap.CurrentPrice, openPrice, e.histRange.AvgRange())
	}

	// F0: 振幅扩张过大 → 真突破，PM 判断正确，一票否决
	if e.histRange.IsReady() && e.rangeExpansion >= e.cfg.RangeExpMax {
		return e.returnToWatching()
	}

	// B1: 背离硬要求 —— BTC 不得与 PM 同向（DivergenceFloor），
	// 可选再要求背离强度（MinDivergence > 0）。
	// 历史振幅未就绪时无法计算背离度 → 否决（与 Python 回测一致：
	// 无 hist_avg_range 的事件整体跳过，避免预热期放行无 B1 的信号）
	if e.cfg.MinDivergence > 0 || e.cfg.DivergenceFloor > divergenceFloorDisabled {
		if !e.histRange.IsReady() {
			return e.returnToWatching()
		}
		e.btcPosition = BTCPosition(crossSnap.CurrentPrice, openPrice, e.histRange.AvgRange())
		e.btcDivergence = Divergence(side, e.btcPosition)
		if e.btcDivergence < e.cfg.DivergenceFloor {
			return e.returnToWatching()
		}
		if e.cfg.MinDivergence > 0 && e.btcDivergence < e.cfg.MinDivergence {
			return e.returnToWatching()
		}
	}

	// 全部前置检查通过 → 保存穿越状态
	e.crossSnap = crossSnap
	e.crossSide = side
	e.crossIdx = crossIdx
	e.confirmCount = 0
	e.orderBookLatency = crossSnap.OrderBookLatency // 穿越时刻订单簿延迟，用于风控

	// 若确认 tick 已在 buffer 中（回退到另一侧更早穿越时会出现），
	// 则同步评估 —— 对应 Python check_signal 一次性拥有全部数据。
	if confIdx := crossIdx + e.cfg.ConfirmDelayTicks; confIdx < len(e.snapBuffer) {
		sig := e.onConfirmed(e.snapBuffer[confIdx])
		if sig != nil {
			e.state = stateDone
			e.doneThisGen = true
			return sig
		}
		// 该侧确认失败 → 尝试另一侧穿越，或回到 Watching
		return e.afterFailedConfirm(e.snapBuffer[confIdx])
	}

	// 常规路径：等待确认 tick 到达
	e.state = stateConfirming
	return nil
}

// afterFailedConfirm 处理确认失败后无法产生信号的情况。
// 优先尝试盲窗期间记录的穿越（按 snapshot 时间顺序，先到先评），
// 再检查另一侧当前是否在阈值之上，否则回到 Watching。
func (e *Engine) afterFailedConfirm(snap *lab.ResearchSnapshot) *FlipSignal {
	// ── 优先尝试盲窗期间记录的穿越（按 snapshot 时间顺序）──
	// 对应 Python run_backtest 纯时间顺序扫描：先到先评
	if len(e.pendingCrossings) > 0 {
		// 重要：数据已到齐的穿越才评估，数据不够的保留到后续 tick 重试。
		// （否则 pending crossing 的确认数据尚未到达就被清空，导致信号永久丢失）
		var stillPending []pendingCrossing
		for _, pc := range e.pendingCrossings {
			if pc.crossIdx+e.cfg.ConfirmDelayTicks < len(e.snapBuffer) {
				// 确认数据已到齐，尝试评估
				e.retryCount++
				if sig := e.evaluateCrossingAt(pc.crossIdx, pc.side); sig != nil {
					e.state = stateDone
					e.doneThisGen = true
					e.pendingCrossings = e.pendingCrossings[:0]
					return sig
				}
				// 评估完成 → 不保留（已确定失败）
			} else {
				// 确认数据尚未到达 → 保留，等后续 tick
				stillPending = append(stillPending, pc)
			}
		}
		e.pendingCrossings = stillPending
	}

	// ── fallback：检查另一侧当前是否在阈值之上 ──
	otherSide := "no"
	if e.crossSide == "no" {
		otherSide = "yes"
	}

	otherIsAbove := false
	if otherSide == "yes" {
		otherIsAbove = snap.YesPrice > e.cfg.TriggerThreshold &&
			snap.RemainingSec < e.cfg.MaxRemainingSec &&
			snap.RemainingSec > e.cfg.MinRemainingSec
	} else {
		otherIsAbove = snap.NoPrice > e.cfg.TriggerThreshold &&
			snap.RemainingSec < e.cfg.MaxRemainingSec &&
			snap.RemainingSec > e.cfg.MinRemainingSec
	}

	if otherIsAbove {
		// 反向扫描 buffer，找另一侧最近一次上升沿
		otherCrossIdx := e.findRecentCrossing(otherSide)
		if otherCrossIdx >= 0 && otherCrossIdx >= e.cfg.MinPreSnaps {
			return e.enterConfirming(otherCrossIdx, otherSide)
		}
	}

	// 无待处理穿越 → 回到 Watching
	e.returnToWatching()
	return nil
}

// findRecentCrossing 反向扫描 snapBuffer，寻找指定 side 最近一次
// 上升沿（≤阈值 → >阈值）。未找到返回 -1。
func (e *Engine) findRecentCrossing(side string) int {
	wasAbove := false
	for i := len(e.snapBuffer) - 1; i >= 0; i-- {
		s := e.snapBuffer[i]
		var price float64
		if side == "yes" {
			price = s.YesPrice
		} else {
			price = s.NoPrice
		}
		isAbove := price > e.cfg.TriggerThreshold &&
			s.RemainingSec < e.cfg.MaxRemainingSec &&
			s.RemainingSec > e.cfg.MinRemainingSec
		if wasAbove && !isAbove {
			return i + 1 // rising edge at next snapshot
		}
		wasAbove = isAbove
	}
	if wasAbove {
		return 0 // very first snapshot was already above threshold
	}
	return -1
}

// returnToWatching 重置穿越状态并切换回 Watching。
func (e *Engine) returnToWatching() *FlipSignal {
	// 保存本次失败穿越的 T=0 特征供 Dashboard 诊断用
	if e.crossSnap != nil {
		e.lastFailedT0 = &T0Features{
			Side:             e.crossSide,
			RangeExpansion:   e.rangeExpansion,
			BTCPosition:      e.btcPosition,
			BtcDivergence:    e.btcDivergence,
			OtherDelta:       e.lastFailedOtherDelta,
			EntryPrice:       e.lastFailedEntryPrice,
			Score:            e.lastFailedScore,
			OrderBookLatency: e.orderBookLatency,
		}
	}
	e.state = stateWatching
	e.crossSnap = nil
	e.crossSide = ""
	e.crossIdx = 0
	e.confirmCount = 0
	// 注意：不清空 pendingCrossings — 其中可能还有确认数据未到齐的穿越，
	// 需在后续 Watching 状态的 tick 中重试评估（见 ProcessSnapshot case stateWatching）
	return nil
}

// evaluateCrossingAt 对指定穿越点计算完整评分（T=0 特征 + T+N 确认 + Formula B 评分），
// 返回信号或 nil。不修改引擎状态，仅更新 lastFailedT0 诊断字段。
//
// 与 enterConfirming + onConfirmed 计算逻辑完全一致，但作为纯函数运行 ——
// 专为盲窗期间记录的穿越（pendingCrossings）设计：确认数据已在 buffer 中，
// 无需等待，直接同步评估。
func (e *Engine) evaluateCrossingAt(crossIdx int, side string) *FlipSignal {
	crossSnap := e.snapBuffer[crossIdx]

	// ── 前置条件 ──
	if crossIdx+1 < e.cfg.MinPreSnaps {
		return nil
	}

	openPrice := crossSnap.OpenPrice

	// 振幅扩张 + F0 真突破否决
	var rangeExpansion float64
	if e.histRange.IsReady() {
		rangeExpansion = RangeExpansion(crossSnap.CurrentPrice, openPrice, e.histRange.AvgRange())
		if rangeExpansion >= e.cfg.RangeExpMax {
			return nil
		}
	}

	// B1 背离硬要求：BTC 不得与 PM 同向（DivergenceFloor），
	// 可选再要求背离强度（MinDivergence > 0）
	// 历史振幅未就绪时无法计算背离度 → 否决（与 Python 回测一致）
	var btcPosition, btcDivergence float64
	if e.cfg.MinDivergence > 0 || e.cfg.DivergenceFloor > divergenceFloorDisabled {
		if !e.histRange.IsReady() {
			return nil
		}
		btcPosition = BTCPosition(crossSnap.CurrentPrice, openPrice, e.histRange.AvgRange())
		btcDivergence = Divergence(side, btcPosition)
		if btcDivergence < e.cfg.DivergenceFloor {
			return nil
		}
		if e.cfg.MinDivergence > 0 && btcDivergence < e.cfg.MinDivergence {
			return nil
		}
	}

	// ── 确认数据（T+N）──
	confIdx := crossIdx + e.cfg.ConfirmDelayTicks
	if confIdx >= len(e.snapBuffer) {
		return nil // 确认数据尚未到达（不应出现，防御性检查）
	}
	confSnap := e.snapBuffer[confIdx]

	// other_delta
	var otherDelta float64
	if side == "yes" {
		otherDelta = confSnap.NoPrice - crossSnap.NoPrice
	} else {
		otherDelta = confSnap.YesPrice - crossSnap.YesPrice
	}
	if otherDelta < e.cfg.ODHardFilter {
		return nil
	}

	// entry price
	var entryPrice float64
	if side == "yes" {
		entryPrice = crossSnap.NoPrice
	} else {
		entryPrice = crossSnap.YesPrice
	}

	// ── 入场价上限 gate（与 onConfirmed 一致，保持多穿越重试路径同步）──
	// ASK 口径：yes_price/no_price 存的是各订单簿 best bid，实盘 FAK 买对侧
	// 成交在对侧 ask = 1 - 触发侧 bid（双 token 互补），与 Trader 的最优卖价
	// 校验（trader.go）口径一致。
	var confirmPrice float64
	if side == "yes" {
		confirmPrice = 1.0 - confSnap.YesPrice // 买 NO: NO ask = 1 - YES bid
	} else {
		confirmPrice = 1.0 - confSnap.NoPrice // 买 YES: YES ask = 1 - NO bid
	}
	if e.cfg.MaxEntryPrice > 0 && confirmPrice > e.cfg.MaxEntryPrice {
		return nil
	}

	// 穿越点与确认点订单簿延迟取最大值
	latency := crossSnap.OrderBookLatency
	if confSnap.OrderBookLatency > latency {
		latency = confSnap.OrderBookLatency
	}

	// ── Formula B 评分 ──
	params := ScoreParams{
		OtherDelta:       otherDelta,
		RangeExpansion:   rangeExpansion,
		HistReady:        e.histRange.IsReady(),
		OrderBookLatency: latency,
		Cfg:              e.cfg,
	}

	score, vetoed := ComputeFlipScore(params)
	if vetoed || score < e.cfg.ScoreEntry {
		// 更新诊断字段供 Dashboard 展示
		e.lastFailedT0 = &T0Features{
			Side:             side,
			RangeExpansion:   rangeExpansion,
			BTCPosition:      btcPosition,
			BtcDivergence:    btcDivergence,
			OtherDelta:       otherDelta,
			EntryPrice:       entryPrice,
			Score:            score,
			OrderBookLatency: latency,
		}
		e.lastFailedOtherDelta = otherDelta
		e.lastFailedEntryPrice = entryPrice
		e.lastFailedScore = score
		return nil
	}

	// 评估通过 → 更新引擎状态以反映本次穿越
	e.crossSnap = crossSnap
	e.crossSide = side
	e.crossIdx = crossIdx
	e.rangeExpansion = rangeExpansion
	e.btcPosition = btcPosition
	e.btcDivergence = btcDivergence
	e.orderBookLatency = crossSnap.OrderBookLatency

	return &FlipSignal{
		Time:             time.Now().UTC(),
		Side:             side,
		Score:            score,
		EntryPrice:       entryPrice,
		FillPrice:        confirmPrice,
		Shares:           0,
		ExecStatus:       "pending",
		RemainingSec:     crossSnap.RemainingSec,
		RangeExpansion:   rangeExpansion,
		BTCPosition:      btcPosition,
		BtcDivergence:    btcDivergence,
		OtherDelta:       otherDelta,
		OrderBookLatency: latency,
	}
}

// onConfirmed 在确认延迟（ConfirmDelayTicks）到达后被调用，
// 计算 T+N 确认期特征及最终 Formula B 评分。
func (e *Engine) onConfirmed(snap *lab.ResearchSnapshot) *FlipSignal {
	// 计算 other_delta —— 对面价格在确认期内的变化
	var otherDelta float64
	if e.crossSide == "yes" {
		// YES>0.7，对面是 NO
		otherDelta = snap.NoPrice - e.crossSnap.NoPrice
	} else {
		// NO>0.7，对面是 YES
		otherDelta = snap.YesPrice - e.crossSnap.YesPrice
	}

	// other_delta 硬过滤（默认 -999 = 禁用，由评分权重处理）
	if otherDelta < e.cfg.ODHardFilter {
		e.lastFailedOtherDelta = otherDelta
		e.lastFailedScore = 0
		return nil
	}

	// 入场价 = 穿越时刻对面 bid（评分特征/纸面结算用）
	var entryPrice float64
	if e.crossSide == "yes" {
		entryPrice = e.crossSnap.NoPrice // buy NO, bet DOWN
	} else {
		entryPrice = e.crossSnap.YesPrice // buy YES, bet UP
	}

	// ── 入场价上限 gate（与回测 max_entry_price 一致）──
	// ASK 口径：确认时刻对侧 ask = 1 - 触发侧 bid（双 token 互补）。
	// yes_price/no_price 存的是 best bid，实盘 FAK 买对侧必须跨 spread 成交
	// 在 ask，与 Trader 的最优卖价校验（trader.go）口径一致。
	// 强 other_delta 的确认意味着廉价入场已消失（穿越价是确认期前的旧价），
	// 对侧 ask 已超上限 → 盈亏比恶化，实盘 FAK 必被拒 → 信号无效。
	var confirmPrice float64
	if e.crossSide == "yes" {
		confirmPrice = 1.0 - snap.YesPrice // 买 NO: NO ask = 1 - YES bid
	} else {
		confirmPrice = 1.0 - snap.NoPrice // 买 YES: YES ask = 1 - NO bid
	}
	if e.cfg.MaxEntryPrice > 0 && confirmPrice > e.cfg.MaxEntryPrice {
		log.Printf("[Flip] 💸 价格失效: side=%s 对侧ask %.3f > 上限 %.3f（穿越时对侧bid %.3f 已过期）",
			e.crossSide, confirmPrice, e.cfg.MaxEntryPrice, entryPrice)
		e.lastFailedOtherDelta = otherDelta
		e.lastFailedEntryPrice = entryPrice
		e.lastFailedScore = 0
		return nil
	}

	// 取穿越时刻与确认时刻延迟的最大值，防止确认 tick 延迟飙升漏检
	latency := max(e.orderBookLatency, snap.OrderBookLatency)

	// 计算 Formula B 评分
	params := ScoreParams{
		OtherDelta:       otherDelta,
		RangeExpansion:   e.rangeExpansion,
		HistReady:        e.histRange.IsReady(),
		OrderBookLatency: latency,
		Cfg:              e.cfg,
	}

	score, vetoed := ComputeFlipScore(params)
	if vetoed {
		// 延迟否决时输出日志
		if e.cfg.MaxLatencyMs > 0 && latency > e.cfg.MaxLatencyMs {
			log.Printf("[Flip] 🐢 延迟否决: latency=%dms > max=%dms, side=%s entry=%.3f",
				latency, e.cfg.MaxLatencyMs, e.crossSide, entryPrice)
		}
		e.lastFailedOtherDelta = otherDelta
		e.lastFailedEntryPrice = entryPrice
		e.lastFailedScore = score
		return nil
	}
	if score < e.cfg.ScoreEntry {
		e.lastFailedOtherDelta = otherDelta
		e.lastFailedEntryPrice = entryPrice
		e.lastFailedScore = score
		return nil
	}

	// 仓位大小由调用方根据 stake_per_signal 计算，Engine 仅负责信号检测。
	// ScoreAdd 阈值不再控制仓位翻倍（由调用方自行决定是否使用）。

	return &FlipSignal{
		Time:             time.Now().UTC(),
		Side:             e.crossSide,
		Score:            score,
		EntryPrice:       entryPrice,
		FillPrice:        confirmPrice,
		Shares:           0, // 由调用方根据 stake_per_signal 计算后填充
		ExecStatus:       "pending",
		RemainingSec:     e.crossSnap.RemainingSec,
		RangeExpansion:   e.rangeExpansion,
		BTCPosition:      e.btcPosition,
		BtcDivergence:    e.btcDivergence,
		OtherDelta:       otherDelta,
		OrderBookLatency: latency,
	}
}

// ── Dashboard 访问器 ──
// Engine 字段仅在主市场循环 goroutine 中写入。
// Dashboard HTTP handler 从另一 goroutine 读取；Go 内存模型对简单类型
// 保证最终可见性，对展示用途已足够。

// State 返回当前引擎状态。
func (e *Engine) State() flipState { return e.state }

// Generation 返回当前周期的代数。
func (e *Engine) Generation() int64 { return e.generation }

// SnapCount 返回当前周期已采集的 snapshot 数量。
func (e *Engine) SnapCount() int { return len(e.snapBuffer) }

// CurrentSide 返回当前正在评估的 side（"yes"、"no" 或 ""）。
func (e *Engine) CurrentSide() string { return e.crossSide }

// ConfirmTicksWaited 返回进入 Confirming 状态后已等待的 tick 数。
func (e *Engine) ConfirmTicksWaited() int { return e.confirmCount }

// T0Features 返回穿越时刻（T=0）计算的特征快照。
// 无活跃穿越时返回 nil。
func (e *Engine) T0Features() *T0Features {
	if e.crossSnap == nil {
		return nil
	}
	return &T0Features{
		Side:             e.crossSide,
		RangeExpansion:   e.rangeExpansion,
		BTCPosition:      e.btcPosition,
		BtcDivergence:    e.btcDivergence,
		OrderBookLatency: e.orderBookLatency,
	}
}

// IsDone 在当前周期已产生信号时返回 true。
func (e *Engine) IsDone() bool { return e.doneThisGen }

// StateLabel 返回当前引擎状态的可读标签。
func (e *Engine) StateLabel() string {
	switch e.state {
	case stateIdle:
		return "Idle"
	case stateWatching:
		return "Watching"
	case stateConfirming:
		return "Confirming"
	case stateDone:
		return "Done"
	default:
		return "Unknown"
	}
}

// Config 返回引擎运行配置的副本。
func (e *Engine) Config() FlipConfig { return e.cfg }

// RetryCount 返回本周期内已进入 Confirming 的次数（多穿越模式下可能 > 1）。
func (e *Engine) RetryCount() int { return e.retryCount }

// LastFailedT0 返回最近一次失败穿越的 T=0 特征，无失败穿越时返回 nil。
func (e *Engine) LastFailedT0() *T0Features { return e.lastFailedT0 }
