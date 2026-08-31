package flip

import (
	"testing"
	"time"
)

// mkTick 构造一条 tick（缺省 ask 为 1-bid 互补，缺省 bid 为 0）。
func mkTick(rem int, upBid, downBid float64) Tick {
	t := Tick{
		Ts:  time.Now().UnixMilli(),
		Rem: rem,
	}
	if upBid > 0 {
		t.UpBid = upBid
		t.UpAsk = 1 - upBid
	}
	if downBid > 0 {
		t.DownBid = downBid
		t.DownAsk = 1 - downBid
	}
	return t
}

// run 逐 tick 驱动引擎，返回所有观测。
func run(e *Engine, ticks []Tick) []*Cross {
	var out []*Cross
	for _, t := range ticks {
		if c := e.ProcessTick(t); c != nil {
			out = append(out, c)
		}
	}
	return out
}

// crossingSeq 生成"穿越 + N 个确认 tick"的序列: 第 0 tick 触发侧 bid 0.75，
// 确认 tick 触发侧 bid 为 postEnd。
func crossingSeq(postEnd float64, n int) []Tick {
	seq := []Tick{mkTick(200, 0.75, 0.10)}
	for i := 1; i <= n; i++ {
		seq = append(seq, mkTick(200-i, postEnd, 0.10))
	}
	return seq
}

func TestEngine_CrossingTrigger(t *testing.T) {
	e := NewEngine(DefaultConfig())
	c := run(e, []Tick{
		mkTick(200, 0.69, 0.10), // 阈值下方
		mkTick(199, 0.71, 0.10), // 首个上升沿
	})
	if len(c) != 0 {
		t.Fatalf("穿越应进入确认而非立即判定, got %+v", c)
	}
	if e.pend == nil || e.pend.side != "up" || e.pend.triggerBid != 0.71 {
		t.Fatalf("首个穿越记录错误: %+v", e.pend)
	}
	if e.State() != "Confirming" {
		t.Fatalf("应处于 Confirming, got %s", e.State())
	}
}

func TestEngine_NoTriggerBelowThreshold(t *testing.T) {
	e := NewEngine(DefaultConfig())
	c := run(e, []Tick{
		mkTick(200, 0.69, 0.69), // 两侧都低于阈值
		mkTick(199, 0.70, 0.10), // 恰等于阈值（回测 >0.7 严格大于）
	})
	if len(c) != 0 || e.pend != nil {
		t.Fatalf("不应触发穿越: %+v", c)
	}
}

func TestEngine_RemWindowExcluded(t *testing.T) {
	e := NewEngine(DefaultConfig())
	// rem ≥ 260 与 rem ≤ 15 的 tick 不参与检测
	c := run(e, []Tick{
		mkTick(261, 0.80, 0.10), // rem 超出上限
		mkTick(10, 0.80, 0.10),  // rem 低于下限
	})
	if len(c) != 0 || e.pend != nil {
		t.Fatalf("窗口外 tick 不应触发穿越: %+v", c)
	}
	// 窗口内正常触发
	c = run(e, []Tick{mkTick(200, 0.75, 0.10)})
	if e.pend == nil {
		t.Fatalf("窗口内应触发穿越")
	}
}

func TestEngine_FirstCrossingWins(t *testing.T) {
	e := NewEngine(DefaultConfig())
	// UP 先穿，DOWN 后穿 → 观测侧为 UP
	c := run(e, []Tick{
		mkTick(200, 0.75, 0.10),
		mkTick(199, 0.72, 0.74),
	})
	if len(c) != 0 || e.pend == nil || e.pend.side != "up" {
		t.Fatalf("首个穿越应为 UP: %+v", e.pend)
	}
}

func TestEngine_ConfirmAndSignal(t *testing.T) {
	e := NewEngine(DefaultConfig())
	cfg := e.Config()
	// 穿越 0.75 > 0.73 (C1✓)，确认 tick post_end 0.62 ≤ 0.66 (C2✓)
	seq := crossingSeq(0.62, cfg.ConfirmSec)
	seq[0].Ts = 1788183600000 // 固定穿越时刻（其余 tick Ts 由 mkTick 实时生成）
	outs := run(e, seq)
	if len(outs) != 1 {
		t.Fatalf("应产出 1 个观测, got %d", len(outs))
	}
	c := outs[0]
	if !c.OK {
		t.Fatalf("应通过判定, reject=%s", c.RejectReason)
	}
	if c.Side != "up" || c.TriggerBid != 0.75 || c.PostEnd != 0.62 {
		t.Fatalf("字段错误: %+v", c)
	}
	// Ts 必须是穿越时刻（非确认 tick 时刻），与回测 trades 口径一致
	if c.Ts != seq[0].Ts {
		t.Fatalf("Ts 应为穿越时刻 %d, got %d", seq[0].Ts, c.Ts)
	}
	// fill = 对侧 ask@+10s = 1 - 0.10 = 0.90; fill_comp = 1 - 0.62 = 0.38
	if c.Fill != 0.90 || c.FillComp != 0.38 {
		t.Fatalf("fill 口径错误: fill=%v fill_comp=%v", c.Fill, c.FillComp)
	}
	if c.Shares != cfg.Stake/0.90 {
		t.Fatalf("股数错误: %v", c.Shares)
	}
	// 判定后 Done: 后续 tick 不再检测
	more := run(e, []Tick{mkTick(150, 0.95, 0.95)})
	if len(more) != 0 {
		t.Fatalf("Done 后不应再检测: %+v", more)
	}
}

func TestEngine_RejectTriggerBidTooLow(t *testing.T) {
	e := NewEngine(DefaultConfig())
	cfg := e.Config()
	// 穿越 0.72 ≤ 0.73 (C1✗)
	seq := []Tick{mkTick(200, 0.72, 0.10)}
	for i := 1; i <= cfg.ConfirmSec; i++ {
		seq = append(seq, mkTick(200-i, 0.60, 0.10))
	}
	outs := run(e, seq)
	if len(outs) != 1 || outs[0].OK || outs[0].RejectReason != "trigger_bid_too_low" {
		t.Fatalf("C1 应拒绝: %+v", outs)
	}
}

func TestEngine_RejectPostEndTooHigh(t *testing.T) {
	e := NewEngine(DefaultConfig())
	cfg := e.Config()
	// 穿越 0.75 (C1✓)，确认 tick 0.68 > 0.66 (C2✗)
	seq := crossingSeq(0.68, cfg.ConfirmSec)
	outs := run(e, seq)
	if len(outs) != 1 || outs[0].OK || outs[0].RejectReason != "post_end_too_high" {
		t.Fatalf("C2 应拒绝: %+v", outs)
	}
}

func TestEngine_RejectAskOutOfBand(t *testing.T) {
	e := NewEngine(DefaultConfig())
	cfg := e.Config()
	// C1✓ C2✓，但对侧 ask 越界（ask < 0.05）→ 不成交（与回测 fill∈[0.05,0.95] 过滤一致）
	seq := []Tick{mkTick(200, 0.75, 0.98)} // downBid 0.98 → DownAsk = 0.02 < 0.05
	for i := 1; i <= cfg.ConfirmSec; i++ {
		seq = append(seq, mkTick(200-i, 0.60, 0.98))
	}
	outs := run(e, seq)
	if len(outs) != 1 || outs[0].OK || outs[0].RejectReason != "ask_out_of_band" {
		t.Fatalf("低价越界应拒绝: %+v", outs)
	}

	// 上界: ask > 0.95 → downBid 0.02 → DownAsk = 0.98
	e = NewEngine(DefaultConfig())
	seq = []Tick{mkTick(200, 0.75, 0.02)}
	for i := 1; i <= cfg.ConfirmSec; i++ {
		seq = append(seq, mkTick(200-i, 0.60, 0.02))
	}
	outs = run(e, seq)
	if len(outs) != 1 || outs[0].OK || outs[0].RejectReason != "ask_out_of_band" {
		t.Fatalf("高价越界应拒绝: %+v", outs)
	}
}

func TestEngine_ConfirmingTracksCls(t *testing.T) {
	e := NewEngine(DefaultConfig())
	// UP 穿越进入 Confirming；确认期间 DOWN 也穿越 → cls = both
	seq := []Tick{mkTick(200, 0.75, 0.10)}
	for i := 1; i <= e.Config().ConfirmSec; i++ {
		seq = append(seq, mkTick(200-i, 0.62, 0.80)) // DOWN 在确认期间穿越（0.80 > 0.7）
	}
	run(e, seq)
	if res := e.Finalize(mkTick(0, 0.62, 0.10)); res.Cls != "both" {
		t.Fatalf("确认期对侧穿越应记 both, got %s", res.Cls)
	}
}

func TestEngine_ConfirmAtWindowTail(t *testing.T) {
	e := NewEngine(DefaultConfig())
	cfg := e.Config()
	// 穿越在 rem=20，确认 tick 落入 rem≤15（回测 j 截断等价，确认仍完成）
	seq := []Tick{mkTick(20, 0.75, 0.10)}
	for i := 1; i <= cfg.ConfirmSec; i++ {
		seq = append(seq, mkTick(20-i, 0.60, 0.10))
	}
	outs := run(e, seq)
	if len(outs) != 1 || !outs[0].OK {
		t.Fatalf("窗口尾部确认应完成: %+v", outs)
	}
}

func TestEngine_MissingBookSkipsSignal(t *testing.T) {
	e := NewEngine(DefaultConfig())
	cfg := e.Config()
	// 确认时刻触发侧盘口缺失（bid=0）→ 跳过信号检查（非规则否决）
	seq := []Tick{mkTick(200, 0.75, 0.10)}
	for i := 1; i <= cfg.ConfirmSec; i++ {
		seq = append(seq, mkTick(200-i, 0, 0.10)) // 触发侧 bid 缺失
	}
	outs := run(e, seq)
	if len(outs) != 1 || outs[0].OK || outs[0].RejectReason != "missing_book" {
		t.Fatalf("缺数据应跳过: %+v", outs)
	}
}

func TestEngine_FinalizeCompletesConfirm(t *testing.T) {
	e := NewEngine(DefaultConfig())
	// 穿越后只走 3 个确认 tick 即窗口结束 → Finalize 用末 tick 补判定
	run(e, []Tick{mkTick(200, 0.75, 0.10), mkTick(199, 0.74, 0.10), mkTick(198, 0.63, 0.10)})
	res := e.Finalize(mkTick(197, 0.60, 0.10))
	if res.Cross == nil || !res.Cross.OK {
		t.Fatalf("Finalize 应补判定: %+v", res.Cross)
	}
}

func TestEngine_ClsBothAndOnly(t *testing.T) {
	// only: 单侧穿越
	e := NewEngine(DefaultConfig())
	run(e, []Tick{mkTick(200, 0.75, 0.10), mkTick(199, 0.80, 0.10)})
	if res := e.Finalize(mkTick(0, 0.62, 0.10)); res.Cls != "only" {
		t.Fatalf("单侧穿越应为 only, got %s", res.Cls)
	}

	// both: 两侧都穿越（判定后仍跟踪，Finalize 前补 DOWN）
	e = NewEngine(DefaultConfig())
	seq := crossingSeq(0.62, e.Config().ConfirmSec) // UP 穿越
	run(e, seq)
	run(e, []Tick{mkTick(150, 0.90, 0.75)}) // DOWN 穿越（Done 后仍标记）
	if res := e.Finalize(mkTick(0, 0.62, 0.10)); res.Cls != "both" {
		t.Fatalf("双侧穿越应为 both, got %s", res.Cls)
	}
	if got := e.crossedSides(); len(got) != 2 {
		t.Fatalf("穿越侧应含两侧: %v", got)
	}
}

func TestEngine_NoCrossEvent(t *testing.T) {
	e := NewEngine(DefaultConfig())
	run(e, []Tick{mkTick(200, 0.30, 0.30), mkTick(100, 0.40, 0.40)})
	res := e.Finalize(mkTick(0, 0.30, 0.30))
	if res.Cross != nil {
		t.Fatalf("无穿越事件应无观测: %+v", res.Cross)
	}
}
