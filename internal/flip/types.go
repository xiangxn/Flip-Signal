// Package flip 实现「自信崩溃」翻转策略引擎。
//
// 检测 Polymarket YES/NO 价格穿越 0.7 后 10 秒内的反转信号（docs/strategy_plan_2026-08-31.md）:
//
//	C1 高度自信: 穿越时刻触发侧 bid > 0.73（trigger_bid）
//	C2 快速崩溃: 确认时刻（+10s）触发侧 bid ≤ 0.66（post_end）
//
// 信号完全来自 PM 订单簿自身动力学（无任何 BTC 价格输入，口径无关）；
// 决策 +10s 买入对侧，fill = 对侧 ask@+10s（纸面/实盘口径），
// 同时记录互补价 1 - 触发侧bid@+10s（回测口径，双记录评估差异）。
// 纯计算层零外部依赖，状态机支持实时运行（1s tick 粒度）。
package flip

// Config 包含策略的全部可调参数。
// 默认值与回测标定一致（python/v3/，docs/strategy_plan_2026-08-31.md §2）。
type Config struct {
	// 穿越阈值：触发侧 bid 超过此值即检测上升沿（0.7）
	TriggerThreshold float64
	// C1 高度自信：穿越时刻触发侧 bid > 此值才通过（0.73）
	TriggerBidMin float64
	// C2 快速崩溃：确认时刻（+10s）触发侧 bid ≤ 此值才通过（0.66）
	PostEndMax float64
	// 确认等待秒数：穿越后等满 N 个 tick 再判定（10）
	ConfirmSec int
	// 窗口剩余秒上限：仅 rem < 此值的穿越有效（260，回测口径）
	MaxRemaining int
	// 窗口剩余秒下限：仅 rem > 此值的穿越有效（15）
	MinRemaining int
	// 每信号投入 USDC（2）
	Stake float64
}

// DefaultConfig 返回回测标定的默认参数（docs/strategy_plan_2026-08-31.md §2.1）:
// n=140, WR 50.7%, EV +0.107/股（2U 口径总 +73.06 / 14 天）。
func DefaultConfig() Config {
	return Config{
		TriggerThreshold: 0.7,   // 穿越阈值
		TriggerBidMin:    0.73,  // C1 高度自信（交互峰值，0.71~0.74 区间皆正）
		PostEndMax:       0.66,  // C2 快速崩溃（推荐档；0.62~0.72 单调平台）
		ConfirmSec:       10,    // +10s 确认窗口（+2s 变体已证伪）
		MaxRemaining:     260,   // 窗口前 40s 不观测（回测口径）
		MinRemaining:     15,    // 窗口后 15s 不观测（回测口径）
		Stake:            2,     // 每笔 2 USDC
	}
}

// ── 状态机 ──

type engineState int

const (
	stateWatching engineState = iota // 检测首个上升沿
	stateConfirming                  // 已穿越，等待 +10s 确认
	stateDone                        // 事件内判定完成，不再检测
)

// StateLabel 返回状态机可读名（Dashboard 用）。
func (s engineState) String() string {
	switch s {
	case stateWatching:
		return "Watching"
	case stateConfirming:
		return "Confirming"
	case stateDone:
		return "Done"
	}
	return "Unknown"
}

// ── 输入 ──

// Tick 是每秒一条的引擎输入（PM 盘口快照，来源 internal/collect.MakePMTick）。
// 盘口缺失（WS 断流）时对应字段为 0。
type Tick struct {
	Ts       int64   // 本地采样时刻（unix 毫秒）
	Rem      int     // 窗口剩余秒数（0 = 窗口结束）
	YesBid   float64 // YES 最优买价
	YesAsk   float64 // YES 最优卖价
	NoBid    float64 // NO 最优买价
	NoAsk    float64 // NO 最优卖价
	BookLatMs int64  // 盘口传输延迟（毫秒，诊断）
}

// ── 输出 ──

// Cross 是一次穿越观测（成功与失败都产出，诊断/校准信号频率用）。
// 与回测 extract_cross 观测 1:1 对齐（docs/paper_plan_2026-08-31.md §3.2）。
type Cross struct {
	Ts        int64   `json:"ts"`          // 穿越时刻（unix 毫秒）
	Side      string  `json:"side"`        // 触发侧: "yes"/"no"
	Rem       int     `json:"rem"`         // 穿越时窗口剩余秒
	TriggerBid float64 `json:"trigger_bid"` // 穿越时刻触发侧 bid（C1）
	PostEnd   float64 `json:"post_end"`    // 确认时刻触发侧 bid（C2）
	Fill      float64 `json:"fill"`        // 对侧真实 ask@+10s（纸面/实盘成交口径）
	FillComp  float64 `json:"fill_comp"`   // 互补价 1 - 触发侧bid@+10s（回测口径）
	Shares    float64 `json:"shares"`      // 目标股数 = stake / fill（仅 ok=true 时有效）
	OK        bool    `json:"ok"`          // 是否通过 C1/C2 成为信号
	RejectReason string `json:"reject_reason,omitempty"` // 未通过原因
	BookLatMs int64   `json:"book_latency_ms,omitempty"` // 确认时刻盘口延迟（诊断）
}

// crossAt 记录首个穿越时刻的上下文（Confirming 阶段使用）。
type crossAt struct {
	side       string  // 触发侧
	triggerBid float64 // C1 输入
	rem        int     // 穿越时剩余秒
	ts         int64   // 穿越时刻（unix 毫秒）
}

// eventResult 是窗口结束时的类别信息（Finalize 返回）。
type eventResult struct {
	ConditionID string // 事件 conditionId
	Slug        string // 事件 slug
	StartTime   int64  // 窗口起点（unix 秒）
	Cross       *Cross // 本事件穿越观测（无穿越为 nil）
	Cls         string // 类别: "both"/"only"（整窗双侧是否都穿越过，机制诊断）
	Crossed     []string // 本窗穿越过的侧别
}
