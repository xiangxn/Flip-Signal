package flip

import "sync"

// Engine 是「自信崩溃」策略状态机（1s tick 粒度）。
//
// 状态流转（与回测 extract_cross 口径 1:1，见 docs/paper_plan_2026-08-31.md §3）:
//
//	Watching ──首个上升沿(>0.7, 15<rem<260)──▶ Confirming ──+10s──▶ 判定 → Done
//
// 约定:
//   - 只在 15 < rem < 260 的 tick 上做上升沿检测（窗口外 tick 不更新状态）
//   - 事件内时间顺序首个上升沿即观测，判定后本事件不再检测（无 fallback 重试）
//   - 盘口缺失（bid/ask=0）时跳过信号检查（数据质量问题，非规则否决）
//   - 整窗持续跟踪两侧穿越标记（cls 类别诊断用，不影响信号判定）
type Engine struct {
	// mu 保护引擎状态: 主循环写（ProcessTick/Reset/Finalize），Dashboard 读
	// （State/Config 轮询）。1s tick 粒度下锁开销可忽略。
	mu    sync.RWMutex
	cfg   Config
	state engineState

	// Watching 状态: 各侧当前"是否在阈值上方"（最后有效 tick）
	upAbove   bool
	downAbove bool
	// 整窗穿越标记（cls 诊断）: 各侧是否曾在有效 tick 穿越过
	upCrossed   bool
	downCrossed bool

	// Confirming 状态
	pend        *crossAt // 首个穿越上下文
	confirmLeft int      // 剩余确认 tick 数

	// 观测输出（Finalize 时装配）
	cross *Cross
}

// NewEngine 构造引擎。cfg 为空值时用 DefaultConfig。
func NewEngine(cfg Config) *Engine {
	if cfg == (Config{}) {
		cfg = DefaultConfig()
	}
	return &Engine{
		cfg:   cfg,
		state: stateWatching,
	}
}

// Config 返回引擎配置副本。
func (e *Engine) Config() Config {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.cfg
}

// State 返回当前状态机标签（Dashboard 用）。
func (e *Engine) State() string {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.state.String()
}

// Reset 开始新事件（新 5 分钟窗口）。
func (e *Engine) Reset() {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.state = stateWatching
	e.upAbove, e.downAbove = false, false
	e.upCrossed, e.downCrossed = false, false
	e.pend = nil
	e.confirmLeft = 0
	e.cross = nil
}

// ProcessTick 处理一条 1s tick。返回本次 tick 产出的穿越观测（判定完成的成功
// 或失败观测），无产出返回 nil。窗口结束后（Finalize 前）仍可调用补判定。
func (e *Engine) ProcessTick(t Tick) *Cross {
	e.mu.Lock()
	defer e.mu.Unlock()
	// Done 状态: 只更新整窗穿越标记，不再检测（回测每事件仅首个观测）
	if e.state == stateDone {
		e.trackCrossed(t)
		return nil
	}

	// 窗口边界: 只在 15 < rem < 260 的有效 tick 上更新状态（回测口径）。
	// 注意 rem ≤ 15 的 tick 也参与确认判定（回测 j = min(i+10, n-1) 截断在
	// 数组末尾，含窗口尾部 tick）。
	if t.Rem <= e.cfg.MinRemaining || t.Rem >= e.cfg.MaxRemaining {
		if e.state == stateWatching {
			return nil // 窗口外 tick 不更新 any 状态
		}
		// Confirming 中: 继续计数（窗口尾部的确认 tick）
	}

	if e.state == stateWatching {
		e.trackCrossed(t)
		for _, side := range []string{"up", "down"} {
			above := e.sideBid(side, t) > e.cfg.TriggerThreshold
			if above && !e.sideAbove(side) {
				// 首个上升沿 → 进入确认（回测仅记录时间顺序首个）
				if e.pend == nil {
					e.pend = &crossAt{
						side:       side,
						triggerBid: e.sideBid(side, t),
						rem:        t.Rem,
						ts:         t.Ts,
					}
					e.state = stateConfirming
					e.confirmLeft = e.cfg.ConfirmSec
				}
			}
			e.setSideAbove(side, above)
		}
		return nil
	}

	// Confirming: 倒数确认 tick，满 ConfirmSec 后判定
	e.confirmLeft--
	if e.confirmLeft > 0 {
		return nil
	}
	return e.decide(t)
}

// Finalize 结束当前事件，返回类别信息（cls/穿越观测）。
// 若仍处于 Confirming（数据断流导致确认 tick 缺失），用 lastTick 补判定。
func (e *Engine) Finalize(lastTick Tick) eventResult {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.state == stateConfirming {
		// 确认 tick 未到齐即窗口结束: 用最后已知 tick 判定（与回测数组截断等价）
		e.decide(lastTick)
	}
	res := eventResult{
		Cross:   e.cross,
		Cls:     e.cls(),
		Crossed: e.crossedSides(),
	}
	return res
}

// ── 内部 ──

// decide 在确认 tick 到齐时执行 C1/C2 判定，产出穿越观测（成功或失败）。
func (e *Engine) decide(t Tick) *Cross {
	e.state = stateDone
	p := e.pend
	side := p.side
	postEnd := e.sideBid(side, t)           // C2: 穿越侧 bid@+10s
	otherAsk := e.sideAsk(e.other(side), t) // fill: 对侧真实 ask@+10s

	c := &Cross{
		Ts:         p.ts, // 穿越时刻（回测 trades 口径），非确认时刻
		Side:       side,
		Rem:        p.rem,
		TriggerBid: p.triggerBid,
		PostEnd:    postEnd,
		Fill:       otherAsk,
		BookLatMs:  t.BookLatMs,
		TwapAgeMs:  t.TwapAgeMs,
	}
	if postEnd > 0 {
		c.FillComp = 1 - postEnd // 互补价（回测口径）
	}

	// 数据质量: 确认时刻触发侧 bid 缺失 → 跳过信号检查（不视为规则否决）
	if postEnd <= 0 {
		c.RejectReason = "missing_book"
		e.cross = c
		return c
	}
	// C1: 穿越时刻高度自信（trigger_bid > 0.73）
	if p.triggerBid <= e.cfg.TriggerBidMin {
		c.RejectReason = "trigger_bid_too_low"
		e.cross = c
		return c
	}
	// C2: 确认时刻已崩溃（post_end ≤ 0.66）
	if postEnd > e.cfg.PostEndMax {
		c.RejectReason = "post_end_too_high"
		e.cross = c
		return c
	}
	// 成交价可用性: 对侧 ask 缺失无法定价
	if otherAsk <= 0 {
		c.RejectReason = "missing_ask"
		e.cross = c
		return c
	}

	c.OK = true
	c.Shares = e.cfg.Stake / otherAsk // 股数 = stake / fill
	e.cross = c
	return c
}

// trackCrossed 更新整窗穿越标记（cls 诊断）: 各侧曾在有效 tick 穿越过。
func (e *Engine) trackCrossed(t Tick) {
	if t.Rem <= e.cfg.MinRemaining || t.Rem >= e.cfg.MaxRemaining {
		return
	}
	if e.sideBid("up", t) > e.cfg.TriggerThreshold {
		e.upCrossed = true
	}
	if e.sideBid("down", t) > e.cfg.TriggerThreshold {
		e.downCrossed = true
	}
}

// cls 返回事件类别: 双侧都穿越过 → "both"，否则 "only"（整窗信息，诊断用）。
func (e *Engine) cls() string {
	if e.upCrossed && e.downCrossed {
		return "both"
	}
	return "only"
}

func (e *Engine) crossedSides() []string {
	var out []string
	if e.upCrossed {
		out = append(out, "up")
	}
	if e.downCrossed {
		out = append(out, "down")
	}
	return out
}

func (e *Engine) sideBid(side string, t Tick) float64 {
	if side == "up" {
		return t.UpBid
	}
	return t.DownBid
}

func (e *Engine) sideAsk(side string, t Tick) float64 {
	if side == "up" {
		return t.UpAsk
	}
	return t.DownAsk
}

func (e *Engine) other(side string) string {
	if side == "up" {
		return "down"
	}
	return "up"
}

func (e *Engine) sideAbove(side string) bool {
	if side == "up" {
		return e.upAbove
	}
	return e.downAbove
}

func (e *Engine) setSideAbove(side string, above bool) {
	if side == "up" {
		e.upAbove = above
	} else {
		e.downAbove = above
	}
}
