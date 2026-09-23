package tail

import (
	"testing"

	"github.com/necklace/flip-signal/internal/flip"
)

const testAnchor = 100000.0

// tailTick 构造一个默认**有效**的 tick: 双档报价齐备、延迟 50ms、现货充裕
// （默认热门侧 = yes, dev = +100 美元 ⇒ ⑤ 成立）。
func tailTick(rem int, mut ...func(*flip.Tick)) flip.Tick {
	t := flip.Tick{
		Ts:        int64(1780000000+(300-rem)) * 1000,
		Rem:       rem,
		UpBid:     0.90,
		UpAsk:     0.92,
		DownBid:   0.07,
		DownAsk:   0.09,
		BookLatMs: 50,
		BinPrice:  testAnchor + 100, // dev = +100 美元
		SpotAgeMs: 100,
		TwapPrice: testAnchor + 50,
		TwapAgeMs: 500,
	}
	for _, f := range mut {
		f(&t)
	}
	return t
}

func newTestEngine(t *testing.T, histBps float64) *Engine {
	t.Helper()
	e := NewEngine(DefaultConfig())
	e.BeginWindow(0, 0) // 与 cmd/tail 一致: 开局不设过渡锚
	if !e.UpgradeAnchor(testAnchor, histBps) {
		t.Fatalf("窗口开局注入锚被拒（state=%v）", e.State())
	}
	return e
}

// flushFrame 先用一个 rem=149 的有效 tick 把帧闩锁走完（帧行不带判定, 与后续用例无关）,
// 让紧接着的尾盘 tick 只可能产出 snap 一行——否则 rem≤60 的 tick 会同时命中两个闩锁。
func flushFrame(t *testing.T, e *Engine) {
	t.Helper()
	out := e.ProcessTick(tailTick(149))
	if len(out) != 1 || out[0].Kind != KindFrame {
		t.Fatalf("预置帧行失败: %+v", out)
	}
}

// snapRow 走完帧闩锁后取决策快照, 断言恰好一行。
func snapRow(t *testing.T, e *Engine, rem int, mut ...func(*flip.Tick)) Observation {
	t.Helper()
	flushFrame(t, e)
	out := e.ProcessTick(tailTick(rem, mut...))
	if len(out) != 1 || out[0].Kind != KindSnap {
		t.Fatalf("应恰好产出 1 行 snap, 得到 %+v", out)
	}
	return out[0]
}

// TestEngineTwoFrames 两帧是 tick 流上两个独立闩锁: 先 frame（rem≤150）后 snap（rem≤60）。
func TestEngineTwoFrames(t *testing.T) {
	e := newTestEngine(t, 10) // sd = 10*100000/1e4 = 100 美元

	// rem=200 > FrameRem: 两个闩锁都还没到。
	if out := e.ProcessTick(tailTick(200)); len(out) != 0 {
		t.Fatalf("rem=200 不该产出任何行, 得到 %d 行", len(out))
	}

	// 首个 rem≤150 的有效 tick → frame 行。
	out := e.ProcessTick(tailTick(149))
	if len(out) != 1 || out[0].Kind != KindFrame {
		t.Fatalf("rem=149 应产出 1 行 frame, 得到 %+v", out)
	}
	if out[0].FrameT != 150 || out[0].Rem != 149 {
		t.Fatalf("frame 行 FrameT/Rem 应为 150/149, 得到 %d/%d", out[0].FrameT, out[0].Rem)
	}
	if out[0].Rules != (Rules{}) || out[0].OK || out[0].RejectReason != "" {
		t.Fatalf("frame 行不该带判定: %+v", out[0])
	}
	if out[0].Anchor != testAnchor || out[0].HistBps != 10 {
		t.Fatalf("frame 行应带本窗锚与 σ: anchor=%.2f hist=%.2f", out[0].Anchor, out[0].HistBps)
	}

	// 同一个闩锁只触发一次。
	if out := e.ProcessTick(tailTick(120)); len(out) != 0 {
		t.Fatalf("frame 已产出后不该再产 frame, 得到 %+v", out)
	}

	// rem≤60 → snap 行（带判定）, 随后转 Done。
	out = e.ProcessTick(tailTick(59))
	if len(out) != 1 || out[0].Kind != KindSnap {
		t.Fatalf("rem=59 应产出 1 行 snap, 得到 %+v", out)
	}
	s := out[0]
	if s.FrameT != 60 || s.Rem != 59 {
		t.Fatalf("snap 行 FrameT/Rem 应为 60/59, 得到 %d/%d", s.FrameT, s.Rem)
	}
	if !s.OK || !s.Rules.Rule5() {
		t.Fatalf("dev=+100 应构成 ⑤ 信号, 得到 ok=%v rules=%+v reason=%s", s.OK, s.Rules, s.RejectReason)
	}
	if want := 2 / 0.92; s.Shares != want {
		t.Fatalf("股数应为 stake/hotAsk = %.6f, 得到 %.6f", want, s.Shares)
	}
	if e.State() != stateDone {
		t.Fatalf("产出 snap 后应转 Done, 实为 %v", e.State())
	}
	if out := e.ProcessTick(tailTick(30)); len(out) != 0 {
		t.Fatalf("Done 之后不该再产出, 得到 %+v", out)
	}
}

// TestEngineBothLatchesOnSameTick 首个有效 tick 已在尾盘（如进程尾盘才接上）:
// 两个闩锁同 tick 命中 → 一次调用两行。python 对每个 T 各取一次首帧, 此时本就是同一条 tick。
func TestEngineBothLatchesOnSameTick(t *testing.T) {
	e := newTestEngine(t, 10)
	out := e.ProcessTick(tailTick(50))
	if len(out) != 2 {
		t.Fatalf("首个有效 tick 已 rem≤60 时应同发两行, 得到 %d 行", len(out))
	}
	if out[0].Kind != KindFrame || out[1].Kind != KindSnap {
		t.Fatalf("行序应为 frame→snap, 得到 %s→%s", out[0].Kind, out[1].Kind)
	}
	if out[0].Rem != 50 || out[1].Rem != 50 {
		t.Fatalf("两行同源同 tick, Rem 都应为 50: %d/%d", out[0].Rem, out[1].Rem)
	}
	if out[0].FrameT != 150 || out[1].FrameT != 60 {
		t.Fatalf("FrameT 应分别为 150/60, 得到 %d/%d", out[0].FrameT, out[1].FrameT)
	}
	if out[0].OK || !out[1].OK {
		t.Fatalf("只有 snap 行做判定: frame.ok=%v snap.ok=%v", out[0].OK, out[1].OK)
	}
	if ws := e.WindowStats(); ws.Frames != 2 {
		t.Fatalf("Frames 应计 2, 得到 %d", ws.Frames)
	}
}

// TestEngineInvalidTickDoesNotLatch 无效 tick 不推进任何闩锁——「首帧」指的是首个**有效**
// tick（对应 python 在 `rem ≤ T` 判断之前的 continue）。
func TestEngineInvalidTickDoesNotLatch(t *testing.T) {
	e := newTestEngine(t, 10)

	// 延迟超阈: 不占 valid、不产行。
	if out := e.ProcessTick(tailTick(140, func(x *flip.Tick) { x.BookLatMs = 301 })); len(out) != 0 {
		t.Fatalf("延迟超阈的 tick 不该产出行, 得到 %+v", out)
	}
	// 四档缺一: 同上。
	for _, name := range []string{"up_bid", "up_ask", "down_bid", "down_ask"} {
		zero := tailTick(139)
		switch name {
		case "up_bid":
			zero.UpBid = 0
		case "up_ask":
			zero.UpAsk = 0
		case "down_bid":
			zero.DownBid = 0
		case "down_ask":
			zero.DownAsk = 0
		}
		if out := e.ProcessTick(zero); len(out) != 0 {
			t.Fatalf("四档缺 %s 的 tick 不该产出行, 得到 %+v", name, out)
		}
	}

	// 首个有效 tick（rem=138, 在闸内）才取帧, 且取的是**当前** tick 的快照。
	out := e.ProcessTick(tailTick(138))
	if len(out) != 1 || out[0].Kind != KindFrame || out[0].Rem != 138 {
		t.Fatalf("首个有效 tick 应取帧且 Rem=138, 得到 %+v", out)
	}

	ws := e.WindowStats()
	if ws.Ticks != 6 || ws.TicksValid != 1 || ws.BookStale != 1 || ws.BookMissing != 4 {
		t.Fatalf("健康度计数不符: %+v", ws)
	}
	if ws.Ticks != ws.TicksValid+ws.BookStale+ws.BookMissing {
		t.Fatalf("恒等式 Ticks == Valid + Stale + Missing 被破坏: %+v", ws)
	}
}

// TestEngineRemZeroEndsWindow rem==0 是终 tick: 不产出、转 Done; 之后的 tick 一律忽略。
func TestEngineRemZeroEndsWindow(t *testing.T) {
	e := newTestEngine(t, 10)
	if out := e.ProcessTick(tailTick(0)); len(out) != 0 {
		t.Fatalf("rem==0 不该产出, 得到 %+v", out)
	}
	if e.State() != stateDone {
		t.Fatalf("rem==0 应转 Done, 实为 %v", e.State())
	}
	if ws := e.WindowStats(); ws.Ticks != 0 {
		t.Fatalf("终 tick 不计入 Ticks, 得到 %+v", ws)
	}
	if out := e.ProcessTick(tailTick(120)); len(out) != 0 {
		t.Fatalf("窗口结束后不该再产出, 得到 %+v", out)
	}
}

// TestEngineNoAnchorNoRows 锚始终未取到（取锚通道 20s 预算耗尽）⇒ 本窗一行不产出;
// 但 tick 计数照常（健康度仍要能落盘）。
func TestEngineNoAnchorNoRows(t *testing.T) {
	e := NewEngine(DefaultConfig())
	e.BeginWindow(0, 0)
	for _, rem := range []int{200, 149, 120, 59, 30} {
		if out := e.ProcessTick(tailTick(rem)); len(out) != 0 {
			t.Fatalf("无锚时 rem=%d 不该产出行, 得到 %+v", rem, out)
		}
	}
	ws := e.WindowStats()
	if ws.TicksValid != 5 || ws.Frames != 0 {
		t.Fatalf("无锚: 有效 tick 照数、零产出, 得到 %+v", ws)
	}
	if a, h := e.WindowAnchor(); a != 0 || h != 0 {
		t.Fatalf("未命中锚应为 (0,0), 得到 (%.2f,%.2f)", a, h)
	}
}

// TestEngineAnchorFrozenAfterEmit 首行产出即冻结锚: 两行必须共用同一 dev 基准。
func TestEngineAnchorFrozenAfterEmit(t *testing.T) {
	e := newTestEngine(t, 10)
	if out := e.ProcessTick(tailTick(149)); len(out) != 1 {
		t.Fatalf("应产出 frame 行, 得到 %+v", out)
	}
	if e.UpgradeAnchor(testAnchor+500, 12) {
		t.Fatal("首行已产出, 改锚必须被拒（否则帧行与快照行基准不同源）")
	}
	if a, h := e.WindowAnchor(); a != testAnchor || h != 10 {
		t.Fatalf("锚应保持原值 (%.2f,%.2f), 得到 (%.2f,%.2f)", testAnchor, 10.0, a, h)
	}
	// 冻结的是「产出之后」; 产出前的多次注入按 last-wins 生效（取锚通道重试语义）。
	e2 := NewEngine(DefaultConfig())
	e2.BeginWindow(0, 0)
	if !e2.UpgradeAnchor(testAnchor-100, 9) || !e2.UpgradeAnchor(testAnchor, 10) {
		t.Fatal("产出前的注入应被采纳")
	}
	if a, h := e2.WindowAnchor(); a != testAnchor || h != 10 {
		t.Fatalf("产出前应 last-wins, 得到 (%.2f,%.2f)", a, h)
	}
	// anchor ≤ 0 一律拒收（不等值匹配失败时推送值为 0 的防线）。
	if e2.UpgradeAnchor(0, 10) {
		t.Fatal("anchor=0 必须拒收")
	}
	// 窗口结束后拒收。
	e3 := newTestEngine(t, 10)
	e3.ProcessTick(tailTick(0))
	if e3.UpgradeAnchor(testAnchor+1, 10) {
		t.Fatal("窗口结束后改锚必须被拒")
	}
}

// TestEngineRejectReasons 覆盖四类拒绝 + 前两条的「整窗丢弃」语义
// （python 13_tail_sweep.py:91-93 的 continue 落在外层事件循环——**不**顺延到下一个有 spot 的 tick）。
func TestEngineRejectReasons(t *testing.T) {
	cases := []struct {
		name string
		mut  func(*flip.Tick)
		want string
		ok   bool
	}{
		{"ok", func(*flip.Tick) {}, "", true},
		{"missing_spot", func(x *flip.Tick) { x.BinPrice = 0; x.SpotAgeMs = 3000 }, RejectMissingSpot, false},
		{"missing_twap", func(x *flip.Tick) { x.TwapPrice = 0 }, RejectMissingTwap, false},
		{"price_low", func(x *flip.Tick) { x.UpAsk = 0.79 }, RejectPriceLow, false},
		// 价格腿过、两腿都不过: dev=10 < 63, sd=100 时 dev < sd。
		{"leg_out", func(x *flip.Tick) { x.BinPrice = testAnchor + 10 }, RejectLegOut, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			e := newTestEngine(t, 10) // sd = 100 美元
			o := snapRow(t, e, 59, c.mut)
			if o.OK != c.ok || o.RejectReason != c.want {
				t.Fatalf("ok=%v reason=%q, 期望 ok=%v reason=%q", o.OK, o.RejectReason, c.ok, c.want)
			}
			if o.OK && o.Shares <= 0 {
				t.Fatalf("ok 行必须有正股数: %+v", o)
			}
			if !o.OK && o.Shares != 0 {
				t.Fatalf("未成交行不该有股数: %+v", o)
			}
			if e.State() != stateDone {
				t.Fatalf("判定后应转 Done（首触不重试）, 实为 %v", e.State())
			}
		})
	}
}

// TestEngineMissingSpotOrTwapAbandonsWindow 快照 tick 缺 spot/twap ⇒ 整窗丢弃:
// 后续有齐全数据的 tick 也不再判定（与回测的整窗 continue 一致, **不是**往后找）。
func TestEngineMissingSpotOrTwapAbandonsWindow(t *testing.T) {
	for _, c := range []struct {
		name string
		mut  func(*flip.Tick)
	}{
		{"missing_spot", func(x *flip.Tick) { x.BinPrice = 0 }},
		{"missing_twap", func(x *flip.Tick) { x.TwapPrice = 0 }},
	} {
		t.Run(c.name, func(t *testing.T) {
			e := newTestEngine(t, 10)
			o := snapRow(t, e, 59, c.mut)
			if o.OK {
				t.Fatalf("应产出被拒行, 得到 %+v", o)
			}
			// 紧随其后一个数据齐全的 tick（dev=+100 本会构成 ⑤）**不得**产出。
			if out := e.ProcessTick(tailTick(58)); len(out) != 0 {
				t.Fatalf("整窗已在首个快照 tick 上判定完毕, 不该顺延重试, 得到 %+v", out)
			}
		})
	}
}

// TestEngineSideAndDevSign 热门侧 = ask 高的一侧（平局取 yes）; dev 的符号随侧别翻转。
func TestEngineSideAndDevSign(t *testing.T) {
	// no 侧为热门（0.92 > 0.10）, 且现货在锚**下方** 100 美元 ⇒ 押 no 的 dev = +100。
	e := newTestEngine(t, 10)
	o := snapRow(t, e, 59, func(x *flip.Tick) {
		x.UpAsk, x.UpBid = 0.10, 0.09
		x.DownAsk, x.DownBid = 0.92, 0.90
		x.BinPrice = testAnchor - 100
	})
	if o.Side != flip.SideNo || o.HotAsk != 0.92 {
		t.Fatalf("热门侧应为 no/0.92, 得到 %s/%.2f", o.Side, o.HotAsk)
	}
	if o.Dev != 100 {
		t.Fatalf("押 no 时现货低于锚 ⇒ dev 应为 +100, 得到 %.4f", o.Dev)
	}
	if !o.OK || !o.Rules.Rule5() {
		t.Fatalf("dev=+100 应构成 ⑤, 得到 ok=%v rules=%+v", o.OK, o.Rules)
	}

	// 平局取 yes（python: `"yes" if ya >= na else "no"`）。
	e2 := newTestEngine(t, 10)
	o2 := snapRow(t, e2, 59, func(x *flip.Tick) {
		x.UpAsk, x.DownAsk = 0.18, 0.18
	})
	if o2.Side != flip.SideYes {
		t.Fatalf("ask 平局应取 yes, 得到 %s", o2.Side)
	}
}

// TestEngineSdScalesWithAnchor 钉住 sd = hist_bps·anchor/1e4（美元）这一口径
// ——把 bps 当美元用是这条策略最容易出现的量级错误（文档 §4 的 n=17 事故）。
func TestEngineSdScalesWithAnchor(t *testing.T) {
	e := newTestEngine(t, 9.26)
	o := snapRow(t, e, 59, func(x *flip.Tick) { x.BinPrice = testAnchor + 10 })
	if want := 9.26 * testAnchor / 1e4; o.Sd != want { // = 92.6 美元
		t.Fatalf("sd 应为 hist_bps·anchor/1e4 = %.4f 美元, 得到 %.4f", want, o.Sd)
	}
}

// TestEngineNoSigmaRejectsWithNoHist 纯函数防线: σ 不可用（hist_bps=0）时 snap 行
// 记 no_hist。现网由 cmd/tail 的前置闸整窗跳过, 到不了这里。
func TestEngineNoSigmaRejectsWithNoHist(t *testing.T) {
	e := NewEngine(DefaultConfig())
	e.BeginWindow(0, 0)
	if !e.UpgradeAnchor(testAnchor, 0) {
		t.Fatal("锚注入应被采纳（σ=0 只表示不可用）")
	}
	o := snapRow(t, e, 59)
	if o.RejectReason != RejectNoHist {
		t.Fatalf("σ 不可用应记 no_hist, 得到 %+v", o)
	}
	if o.Rules.Sigma || o.Rules.SigmaUSD40 {
		t.Fatalf("σ 不可用时 σ 腿必须为 false: %+v", o.Rules)
	}
}

// TestEngineBeginWindowResets 换窗必须彻底重置（闩锁/统计/锚）。
func TestEngineBeginWindowResets(t *testing.T) {
	e := newTestEngine(t, 10)
	e.ProcessTick(tailTick(149))
	e.BeginWindow(0, 0)
	if e.State() != stateWatching {
		t.Fatalf("换窗后应回 Watching, 实为 %v", e.State())
	}
	// 锚清空是**有意**的: 新窗的锚要走一次新的精确取锚（cmd/tail 每窗都起取锚通道）,
	// 沿用上一窗的锚判定会让 dev 基准错一整窗。
	if a, h := e.WindowAnchor(); a != 0 || h != 0 {
		t.Fatalf("换窗后锚应清空, 得到 (%.2f,%.2f)", a, h)
	}
	if ws := e.WindowStats(); ws != (WindowStats{}) {
		t.Fatalf("换窗后统计应归零, 得到 %+v", ws)
	}
	// 锚清空期间不产出行。
	if out := e.ProcessTick(tailTick(149)); len(out) != 0 {
		t.Fatalf("新窗锚未就绪时不该产出行, 得到 %+v", out)
	}
	if !e.UpgradeAnchor(testAnchor, 10) {
		t.Fatal("新窗注入锚应被采纳")
	}
	if out := e.ProcessTick(tailTick(149)); len(out) != 1 || out[0].Kind != KindFrame {
		t.Fatalf("换窗后闩锁应复位（可再取帧）, 得到 %+v", out)
	}
}
