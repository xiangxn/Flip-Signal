package flip

import (
	"log"
	"math"
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
	PathEff        float64 `json:"path_eff"`
	NoiseRatio     float64 `json:"noise_ratio"`
	Flips          int     `json:"flips"`
	Oscillating    bool    `json:"is_oscillating"`
	RangeExpansion float64 `json:"range_expansion"`
	BTCPosition    float64 `json:"btc_position"`
	BTCExtreme     bool    `json:"btc_extreme"`

	// 评分阶段特征（仅在 onConfirmed 中填充，失败穿越诊断用）
	OtherDelta       float64 `json:"other_delta,omitempty"`
	EntryPrice       float64 `json:"entry_price,omitempty"`
	Score            int     `json:"score,omitempty"`
	OrderBookLatency int64   `json:"order_book_latency,omitempty"` // 穿越时刻订单簿延迟（毫秒）
}

// Engine 从 ResearchSnapshot 流中检测翻转信号。
//
// 多穿越模式（AllowRetryCrossings=true，默认）：
// 每个向上穿越 0.7 的上升沿都触发观测；首个通过评分的穿越获胜。
// 每事件最多一注，YES 侧优先。
//
// 兼容模式（AllowRetryCrossings=false）：每侧仅检测首次穿越。
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

	// 多穿越模式：上升沿检测（≤阈值 → >阈值）
	// 替代旧的 first_crossing_only 字段（yesFirstCrossIdx, noFirstCrossIdx, yesTried, noTried）。
	yesWasAbove bool // 上一 snapshot 中 YES 是否 > 阈值
	noWasAbove  bool // 上一 snapshot 中 NO 是否 > 阈值

	// 兼容模式下的首次穿越追踪（仅 AllowRetryCrossings=false 时使用）
	yesFirstCrossIdx int  // YES 首次 >0.7 的 buffer 下标，-1 表示无
	noFirstCrossIdx  int  // NO 首次 >0.7 的 buffer 下标，-1 表示无
	yesTried         bool // YES 侧已尝试且失败（本周期内）
	noTried          bool // NO 侧已尝试且失败（本周期内）

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
	pathEff          float64
	noiseRatio       float64
	flips            int
	oscillating      bool
	rangeExpansion   float64
	btcPosition      float64
	btcExtreme       bool
	orderBookLatency int64 // 穿越时刻订单簿延迟（毫秒），用于风控
}

// NewEngine 创建一个新的翻转检测引擎。
func NewEngine(cfg FlipConfig, histRange *HistRangeTracker) *Engine {
	return &Engine{
		cfg:               cfg,
		histRange:         histRange,
		state:             stateIdle,
		yesFirstCrossIdx:  -1,
		noFirstCrossIdx:   -1,
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

	e.yesFirstCrossIdx = -1
	e.noFirstCrossIdx = -1
	e.yesTried = false
	e.noTried = false

	e.retryCount = 0
	e.lastFailedT0 = nil
	e.pendingCrossings = e.pendingCrossings[:0]
	e.lastFailedOtherDelta = 0
	e.lastFailedEntryPrice = 0
	e.lastFailedScore = 0

	e.pathEff = 0
	e.noiseRatio = 0
	e.flips = 0
	e.oscillating = false
	e.rangeExpansion = 0
	e.btcPosition = 0
	e.btcExtreme = false
	e.orderBookLatency = 0
}

// ProcessSnapshot 处理一个 ResearchSnapshot。满足全部条件时返回 FlipSignal，否则返回 nil。
//
// 多穿越模式（AllowRetryCrossings=true，默认）：
//   - 对 YES 和 NO 两侧做上升沿检测（≤阈值 → >阈值）。
//   - 每个上升沿触发观测；首个通过全部前置检查且评分达标的穿越获胜。
//     一旦下注，本周期终止。
//   - 确认失败时，检查另一侧是否在等待期间发生了穿越并尝试之；
//     若无则回到 Watching 状态。
//
// 兼容模式（AllowRetryCrossings=false）：每侧仅检测首次穿越。
func (e *Engine) ProcessSnapshot(snap *lab.ResearchSnapshot, gen int64) *FlipSignal {
	if gen != e.generation || e.doneThisGen {
		return nil
	}

	// 始终追加到 buffer，保留完整周期历史。
	bufIdx := len(e.snapBuffer)
	e.snapBuffer = append(e.snapBuffer, snap)

	// 两侧上升沿检测（§2.1: MinRemainingSec < remaining_sec < MaxRemainingSec）
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
		if e.cfg.AllowRetryCrossings && len(e.pendingCrossings) > 0 {
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

		if e.cfg.AllowRetryCrossings {
			// 多穿越模式：每个上升沿都尝试，YES 优先
			if yesRisingEdge && bufIdx >= e.cfg.MinPreSnaps {
				return e.enterConfirming(bufIdx, "yes")
			}
			if noRisingEdge && bufIdx >= e.cfg.MinPreSnaps {
				return e.enterConfirming(bufIdx, "no")
			}
		} else {
			// 兼容模式：记录每侧首次穿越，YES/NO 各尝试一次
			if e.yesFirstCrossIdx < 0 && yesIsAbove {
				e.yesFirstCrossIdx = bufIdx
			}
			if e.noFirstCrossIdx < 0 && noIsAbove {
				e.noFirstCrossIdx = bufIdx
			}
			if !e.yesTried && e.yesFirstCrossIdx >= 0 {
				return e.enterConfirming(e.yesFirstCrossIdx, "yes")
			}
			if !e.noTried && e.noFirstCrossIdx >= 0 {
				return e.enterConfirming(e.noFirstCrossIdx, "no")
			}
		}
		return nil

	case stateConfirming:
		// 盲窗期间记录新穿越，供当前穿越失败后逐个尝试
		// （对应 Python check_signal 批量扫描的行为）
		if e.cfg.AllowRetryCrossings {
			if yesRisingEdge && bufIdx >= e.cfg.MinPreSnaps {
				e.pendingCrossings = append(e.pendingCrossings, pendingCrossing{bufIdx, "yes"})
			}
			if noRisingEdge && bufIdx >= e.cfg.MinPreSnaps {
				e.pendingCrossings = append(e.pendingCrossings, pendingCrossing{bufIdx, "no"})
			}
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
// 从 snapBuffer[0:crossIdx+1] 计算全部 T=0 特征。
//
// 多穿越模式（AllowRetryCrossings=true）：本次穿越被否决不会耗尽该侧 ——
// 后续上升沿仍会重试。仅成功评分（score ≥ ScoreEntry）产生的信号才能终止本周期。
func (e *Engine) enterConfirming(crossIdx int, side string) *FlipSignal {
	e.retryCount++ // 每次进入 Confirming 计数（多穿越模式下可能 > 1）

	crossSnap := e.snapBuffer[crossIdx]

	// 检查穿越前 snapshot 数量（含穿越点自身）。
	nPre := crossIdx + 1 // 穿越点及其之前的所有 snapshot
	if nPre < e.cfg.MinPreSnaps {
		return e.handleEnterFail(side)
	}

	// 从 buffer 中提取穿越时刻之前的价格序列
	prePrices := make([]float64, nPre)
	for i := 0; i < nPre; i++ {
		prePrices[i] = e.snapBuffer[i].CurrentPrice
	}

	openPrice := crossSnap.OpenPrice

	// 计算 T=0 实时特征
	netMove := math.Abs(prePrices[len(prePrices)-1] - openPrice)
	preHigh, preLow := prePrices[0], prePrices[0]
	for _, p := range prePrices {
		if p > preHigh {
			preHigh = p
		}
		if p < preLow {
			preLow = p
		}
	}
	preRange := preHigh - preLow
	if preRange == 0 {
		return e.handleEnterFail(side)
	}

	e.pathEff = netMove / preRange

	// path_eff 过低 → 趋势不明朗，否决
	if e.pathEff < e.cfg.PathEffVetoMin {
		return e.handleEnterFail(side)
	}

	totalPathVal := TotalPath(prePrices)
	if netMove > 0 {
		e.noiseRatio = totalPathVal / netMove
	} else {
		e.noiseRatio = totalPathVal // pure oscillation
	}

	// noise_ratio 过高 → PM 价格不稳定，否决
	if e.noiseRatio > e.cfg.NoiseRatioVetoMax {
		return e.handleEnterFail(side)
	}

	e.flips = CountFlips(prePrices)
	e.oscillating = IsOscillating(e.pathEff, e.noiseRatio, e.flips, e.cfg)

	// 振幅扩张（仅在历史数据就绪后计算）
	if e.histRange.IsReady() {
		e.rangeExpansion = RangeExpansion(crossSnap.CurrentPrice, openPrice, e.histRange.AvgRange())
	}

	// F0: 振幅扩张过大 → 真突破，PM 判断正确，一票否决
	if e.histRange.IsReady() && e.rangeExpansion >= e.cfg.RangeExpMax {
		return e.handleEnterFail(side)
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

// handleEnterFail 处理 enterConfirming 期间的否决。
// 多穿越模式下：放弃本次穿越但不耗尽该侧；兼容模式下：标记该侧已尝试。
func (e *Engine) handleEnterFail(side string) *FlipSignal {
	if !e.cfg.AllowRetryCrossings {
		e.markSideTried(side)
		return e.fallbackAfterFailedConfirm()
	}
	// 多穿越模式：本次穿越失败，回到 Watching 等待下一个上升沿
	e.returnToWatching()
	return nil
}

// afterFailedConfirm 处理确认失败后无法产生信号的情况。
// 多穿越模式下：检查另一侧是否在确认等待期间发生了穿越并尝试之，
// 否则回到 Watching。兼容模式下：委托给 fallbackAfterFailedConfirm。
func (e *Engine) afterFailedConfirm(snap *lab.ResearchSnapshot) *FlipSignal {
	if !e.cfg.AllowRetryCrossings {
		return e.fallbackAfterFailedConfirm()
	}

	// ── 优先尝试盲窗期间记录的穿越（按 snapshot 时间顺序）──
	// 对应 Python run_backtest 纯时间顺序扫描：先到先评
	if len(e.pendingCrossings) > 0 {
		// 按 snapshot 时间顺序尝试。
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

	// ── 现有 fallback：检查另一侧当前是否在阈值之上 ──
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
func (e *Engine) returnToWatching() {
	// 保存本次失败穿越的 T=0 特征供 Dashboard 诊断用
	if e.crossSnap != nil {
		e.lastFailedT0 = &T0Features{
			Side:             e.crossSide,
			PathEff:          e.pathEff,
			NoiseRatio:       e.noiseRatio,
			Flips:            e.flips,
			Oscillating:      e.oscillating,
			RangeExpansion:   e.rangeExpansion,
			BTCPosition:      e.btcPosition,
			BTCExtreme:       e.btcExtreme,
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
}

// fallbackAfterFailedConfirm 实现兼容模式的 fallback：一侧确认失败后，
// 尝试 buffer 中另一侧的首次穿越。仅 AllowRetryCrossings=false 时使用。
func (e *Engine) fallbackAfterFailedConfirm() *FlipSignal {
	// 检查另一侧是否也发生了穿越（更早或当前 tick）
	if !e.yesTried && e.yesFirstCrossIdx >= 0 {
		return e.enterConfirming(e.yesFirstCrossIdx, "yes")
	}
	if !e.noTried && e.noFirstCrossIdx >= 0 {
		return e.enterConfirming(e.noFirstCrossIdx, "no")
	}

	// 两侧均已尝试或均未穿越 → 回到 Watching 等待后续穿越
	e.state = stateWatching
	e.crossSnap = nil
	e.crossSide = ""
	e.crossIdx = 0
	e.confirmCount = 0
	return nil
}

// evaluateCrossingAt 对指定穿越点计算完整评分（T=0 特征 + T+5s 确认 + 7 特征评分），
// 返回信号或 nil。不修改引擎状态，仅更新 lastFailedT0 诊断字段。
//
// 与 enterConfirming + onConfirmed 计算逻辑完全一致，但作为纯函数运行 ——
// 专为盲窗期间记录的穿越（pendingCrossings）设计：确认数据已在 buffer 中，
// 无需等待，直接同步评估。
func (e *Engine) evaluateCrossingAt(crossIdx int, side string) *FlipSignal {
	crossSnap := e.snapBuffer[crossIdx]

	// ── 前置条件 ──
	nPre := crossIdx + 1
	if nPre < e.cfg.MinPreSnaps {
		return nil
	}

	// ── T=0 特征 ──
	prePrices := make([]float64, nPre)
	for i := 0; i < nPre; i++ {
		prePrices[i] = e.snapBuffer[i].CurrentPrice
	}
	openPrice := crossSnap.OpenPrice

	netMove := math.Abs(prePrices[len(prePrices)-1] - openPrice)
	preHigh, preLow := prePrices[0], prePrices[0]
	for _, p := range prePrices {
		if p > preHigh {
			preHigh = p
		}
		if p < preLow {
			preLow = p
		}
	}
	preRange := preHigh - preLow
	if preRange == 0 {
		return nil
	}

	pathEff := netMove / preRange
	if pathEff < e.cfg.PathEffVetoMin {
		return nil
	}

	totalPathVal := TotalPath(prePrices)
	noiseRatio := totalPathVal / netMove
	if netMove == 0 {
		noiseRatio = totalPathVal
	}
	if noiseRatio > e.cfg.NoiseRatioVetoMax {
		return nil
	}

	flips := CountFlips(prePrices)
	oscillating := IsOscillating(pathEff, noiseRatio, flips, e.cfg)

	// Range expansion + F0 veto
	var rangeExpansion float64
	if e.histRange.IsReady() {
		rangeExpansion = RangeExpansion(crossSnap.CurrentPrice, openPrice, e.histRange.AvgRange())
		if rangeExpansion >= e.cfg.RangeExpMax {
			return nil
		}
	}

	// ── 确认数据（T+5s）──
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
	var confirmPrice float64
	if side == "yes" {
		confirmPrice = confSnap.NoPrice
	} else {
		confirmPrice = confSnap.YesPrice
	}
	if e.cfg.MaxEntryPrice > 0 && confirmPrice > e.cfg.MaxEntryPrice {
		return nil
	}

	// BTC position + extreme
	var btcPosition float64
	var btcExtreme bool
	if e.histRange.IsReady() {
		btcPosition = BTCPosition(crossSnap.CurrentPrice, openPrice, e.histRange.AvgRange())
		if side == "yes" {
			btcExtreme = btcPosition < e.cfg.BTCPosMin
		} else {
			btcExtreme = btcPosition > e.cfg.BTCPosMax
		}
	}

	// 穿越点自身的订单簿延迟
	latency := crossSnap.OrderBookLatency
	if confSnap.OrderBookLatency > latency {
		latency = confSnap.OrderBookLatency
	}

	// ── 复合评分 ──
	params := ScoreParams{
		Side:             side,
		OtherDelta:       otherDelta,
		IsOscillating:    oscillating,
		EntryPrice:       entryPrice,
		RangeExpansion:   rangeExpansion,
		BTCPosition:      btcPosition,
		BTCExtreme:       btcExtreme,
		HistReady:        e.histRange.IsReady(),
		OrderBookLatency: latency,
		Cfg:              e.cfg,
	}

	score, vetoed := ComputeFlipScore(params)
	if vetoed || score < e.cfg.ScoreEntry {
		// 更新诊断字段供 Dashboard 展示
		e.lastFailedT0 = &T0Features{
			Side:             side,
			PathEff:          pathEff,
			NoiseRatio:       noiseRatio,
			Flips:            flips,
			Oscillating:      oscillating,
			RangeExpansion:   rangeExpansion,
			BTCPosition:      btcPosition,
			BTCExtreme:       btcExtreme,
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
	e.pathEff = pathEff
	e.noiseRatio = noiseRatio
	e.flips = flips
	e.oscillating = oscillating
	e.rangeExpansion = rangeExpansion
	e.btcPosition = btcPosition
	e.btcExtreme = btcExtreme
	e.orderBookLatency = crossSnap.OrderBookLatency

	return &FlipSignal{
		Time:           time.Now().UTC(),
		Side:           side,
		Score:          score,
		EntryPrice:     entryPrice,
		Shares:         0,
		ExecStatus:     "pending",
		RemainingSec:   crossSnap.RemainingSec,
		PathEff:        pathEff,
		NoiseRatio:     noiseRatio,
		Flips:          flips,
		IsOscillating:  oscillating,
		RangeExpansion: rangeExpansion,
		BTCPosition:    btcPosition,
		BTCExtreme:     btcExtreme,
		OtherDelta:     otherDelta,
		OrderBookLatency: latency,
	}
}

// onConfirmed 在确认延迟（ConfirmDelayTicks）到达后被调用，
// 计算 T+5s 确认期特征及最终复合评分。
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

	// Formula A: other_delta 硬过滤（默认 -999 = 禁用，由评分体系处理）
	if otherDelta < e.cfg.ODHardFilter {
		e.lastFailedOtherDelta = otherDelta
		e.lastFailedScore = 0
		return nil
	}

	// 入场价 = 穿越时刻对面价格
	var entryPrice float64
	if e.crossSide == "yes" {
		entryPrice = e.crossSnap.NoPrice // buy NO, bet DOWN
	} else {
		entryPrice = e.crossSnap.YesPrice // buy YES, bet UP
	}

	// ── 入场价上限 gate（与回测 max_entry_price 一致）──
	// 确认时刻对侧价 = 此刻真正可成交的 5s 采样代理。
	// 强 other_delta 的确认意味着廉价入场已消失（穿越价是确认期前的旧价），
	// 对侧价已超上限 → 盈亏比恶化，实盘 FAK 必被拒 → 信号无效。
	var confirmPrice float64
	if e.crossSide == "yes" {
		confirmPrice = snap.NoPrice
	} else {
		confirmPrice = snap.YesPrice
	}
	if e.cfg.MaxEntryPrice > 0 && confirmPrice > e.cfg.MaxEntryPrice {
		log.Printf("[Flip] 💸 价格失效: side=%s 确认价 %.3f > 上限 %.3f（穿越价 %.3f 已过期）",
			e.crossSide, confirmPrice, e.cfg.MaxEntryPrice, entryPrice)
		e.lastFailedOtherDelta = otherDelta
		e.lastFailedEntryPrice = entryPrice
		e.lastFailedScore = 0
		return nil
	}

	// BTC 位置（仅历史数据就绪时）
	e.btcPosition = 0.0
	e.btcExtreme = false
	if e.histRange.IsReady() {
		e.btcPosition = BTCPosition(e.crossSnap.CurrentPrice, e.crossSnap.OpenPrice, e.histRange.AvgRange())
		// Formula A: BTC 与 PM 背离 = 翻转机会；PM 与 BTC 同向 = 真趋势。
		if e.crossSide == "yes" {
			// YES>0.7（PM 看涨），BTC 下跌 → PM 过度反应
			e.btcExtreme = e.btcPosition < e.cfg.BTCPosMin // btc_pos < -0.1
		} else {
			// NO>0.7（PM 看跌），BTC 上涨 → PM 过度反应
			e.btcExtreme = e.btcPosition > e.cfg.BTCPosMax // btc_pos > 0.1
		}
	}

	// 取穿越时刻与确认时刻延迟的最大值，防止确认 tick 延迟飙升漏检
	latency := max(e.orderBookLatency, snap.OrderBookLatency)

	// 计算复合评分
	params := ScoreParams{
		Side:             e.crossSide,
		OtherDelta:       otherDelta,
		IsOscillating:    e.oscillating,
		EntryPrice:       entryPrice,
		RangeExpansion:   e.rangeExpansion,
		BTCPosition:      e.btcPosition,
		BTCExtreme:       e.btcExtreme,
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
		Time:           time.Now().UTC(),
		Side:           e.crossSide,
		Score:          score,
		EntryPrice:     entryPrice,
		Shares:         0, // 由调用方根据 stake_per_signal 计算后填充
		ExecStatus:     "pending",
		RemainingSec:   e.crossSnap.RemainingSec,
		PathEff:        e.pathEff,
		NoiseRatio:     e.noiseRatio,
		Flips:          e.flips,
		IsOscillating:  e.oscillating,
		RangeExpansion: e.rangeExpansion,
		BTCPosition:    e.btcPosition,
		BTCExtreme:     e.btcExtreme,
		OtherDelta:     otherDelta,
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
		PathEff:          e.pathEff,
		NoiseRatio:       e.noiseRatio,
		Flips:            e.flips,
		Oscillating:      e.oscillating,
		RangeExpansion:   e.rangeExpansion,
		BTCPosition:      e.btcPosition,
		BTCExtreme:       e.btcExtreme,
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

// markSideTried 标记指定 side 在本周期内已尝试。
func (e *Engine) markSideTried(side string) {
	if side == "yes" {
		e.yesTried = true
	} else {
		e.noTried = true
	}
}
