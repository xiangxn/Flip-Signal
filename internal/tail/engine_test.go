package tail

import (
	"testing"

	"github.com/necklace/flip-signal/internal/flip"
)

const testAnchor = 100000.0

// tailTick 构造一个默认**可判定**的 tick: 四档齐备、延迟 50ms、现货充裕
// （默认热门侧 = yes/0.92, dev = +100 美元 ⇒ ⑤ 与 ② 都成立）。
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

// row 处理一个 tick 并断言恰好产出一行（nil = 断言失败）。
func row(t *testing.T, e *Engine, rem int, mut ...func(*flip.Tick)) Observation {
	t.Helper()
	out := e.ProcessTick(tailTick(rem, mut...))
	if out == nil {
		t.Fatalf("rem=%d 应产出一行, 实为零产出（state=%v）", rem, e.State())
	}
	return *out
}

// noRow 处理一个 tick 并断言零产出。
func noRow(t *testing.T, e *Engine, rem int, mut ...func(*flip.Tick)) {
	t.Helper()
	if out := e.ProcessTick(tailTick(rem, mut...)); out != nil {
		t.Fatalf("rem=%d 不该产出行, 得到 %+v", rem, *out)
	}
}

// quietRow 构造一条只过价格腿、两条腿都不过的 tick 变异（dev=+10 < 63, sd=100）。
func quietRow(x *flip.Tick) { x.BinPrice = testAnchor + 10 }

// ── 三段递进链 ──

// TestEngineThreeStages 三段链主路径: T=150 出信号即止（此后整窗不再判定、不再下单）。
func TestEngineThreeStages(t *testing.T) {
	e := newTestEngine(t, 10) // sd = 10·100000/1e4 = 100 美元

	// rem=200 > T150Rem: 还没到第一段判定点, 一行不落（旧口径会在这里就取帧）。
	noRow(t, e, 200)
	if l := e.Latches(); l.T150 || l.T60 || l.Listening {
		t.Fatalf("早于 T=150 的 tick 不该推进任何段, 得到 %+v", l)
	}

	// 首个 rem ≤ 150 的可判定 tick → 第一段判定行。
	o := row(t, e, 149)
	if o.Stage != StageT150 || o.FrameT != 150 || o.Rem != 149 {
		t.Fatalf("应为 t150 判定行（FrameT=150）, 得到 stage=%s frameT=%d rem=%d", o.Stage, o.FrameT, o.Rem)
	}
	if o.Kind != KindSnap {
		t.Fatalf("行类型应为 snap, 得到 %s", o.Kind)
	}
	if !o.OK || !o.Rules.Rule5() {
		t.Fatalf("dev=+100 应构成 ⑤ 信号, 得到 ok=%v rules=%+v reason=%s", o.OK, o.Rules, o.RejectReason)
	}
	if want := 2 / 0.92; o.Shares != want {
		t.Fatalf("股数应为 stake/有效价 = %.6f, 得到 %.6f", want, o.Shares)
	}
	if o.HotSrc != BookSrcAsk {
		t.Fatalf("四档齐备时有效价应来自 ask, 得到 %s", o.HotSrc)
	}
	if l := e.Latches(); !l.T150 || !l.Signal || !l.Frozen {
		t.Fatalf("出信号后 T150/Signal/Frozen 应全真, 得到 %+v", l)
	}
	// 出信号即 Done: 整窗只下一单, 后续 tick（含监听段）一行都不产。
	if e.State() != stateDone {
		t.Fatalf("出信号后应转 Done, 实为 %v", e.State())
	}
	noRow(t, e, 148)
	noRow(t, e, 59)
}

// TestEngineT150RejectThenT60 T=150 不达标 → T=60 再判一次（同规则、同一套输入）。
func TestEngineT150RejectThenT60(t *testing.T) {
	e := newTestEngine(t, 10)

	o := row(t, e, 149, quietRow) // dev=+10: 价格腿过、位移腿与 σ 腿都不过
	if o.Stage != StageT150 || o.OK || o.RejectReason != RejectLegOut {
		t.Fatalf("第一段应被位移腿拒, 得到 stage=%s ok=%v reason=%s", o.Stage, o.OK, o.RejectReason)
	}
	if l := e.Latches(); !l.T150 || l.T60 || l.Listening || l.Signal {
		t.Fatalf("被拒后应只推进 T150, 得到 %+v", l)
	}
	// 150~60 之间不判定（第二段的判定点在 rem ≤ 60）。
	noRow(t, e, 149, quietRow)
	noRow(t, e, 100)
	noRow(t, e, 61)

	// 第二段判定行: 同一个 ⑤, 时点更晚（价格更高）。
	o = row(t, e, 59)
	if o.Stage != StageT60 || o.FrameT != 60 || o.Rem != 59 {
		t.Fatalf("应为 t60 判定行（FrameT=60）, 得到 stage=%s frameT=%d rem=%d", o.Stage, o.FrameT, o.Rem)
	}
	if !o.OK || o.RejectReason != "" {
		t.Fatalf("dev=+100 应构成 ⑤ 信号, 得到 ok=%v reason=%s", o.OK, o.RejectReason)
	}
	if l := e.Latches(); !l.T150 || !l.T60 || !l.Listening || !l.Signal {
		t.Fatalf("第二段出信号后四个闩锁应全真, 得到 %+v", l)
	}
	noRow(t, e, 30)
}

// TestEngineBothStagesRejectThenListening 前两段都不达标 → 监听段: 每秒判 ②, **只在达标时**
// 落行（被拒的 tick 不落盘）。
func TestEngineBothStagesRejectThenListening(t *testing.T) {
	e := newTestEngine(t, 10) // sd = 100 美元
	if o := row(t, e, 149, quietRow); o.OK {
		t.Fatalf("第一段应被拒, 得到 %+v", o)
	}
	if o := row(t, e, 59, quietRow); o.OK {
		t.Fatalf("第二段应被拒, 得到 %+v", o)
	}
	if l := e.Latches(); !l.Listening || l.Signal {
		t.Fatalf("两段都被拒后应进监听段, 得到 %+v", l)
	}
	// 监听段: 不达标的 tick 一行不落（否则每窗白写 ~60 行）。
	noRow(t, e, 58, quietRow)
	noRow(t, e, 57, func(x *flip.Tick) { x.UpAsk, x.UpBid = 0.70, 0.69 }) // 价格腿不过

	// 达标（② = 价格腿 ∧ dev ≥ 63）→ 信号行。
	o := row(t, e, 56)
	if o.Stage != StageListen || o.FrameT != 60 {
		t.Fatalf("监听段信号行 stage 应为 listen / FrameT=60, 得到 %s/%d", o.Stage, o.FrameT)
	}
	if !o.OK || o.RejectReason != "" || !o.Rules.Rule2() {
		t.Fatalf("监听行恒 ok 且满足 ②, 得到 ok=%v reason=%q rules=%+v", o.OK, o.RejectReason, o.Rules)
	}
	if want := 2 / 0.92; o.Shares != want {
		t.Fatalf("股数应为 stake/有效价 = %.6f, 得到 %.6f", want, o.Shares)
	}
	// 监听段出信号后**仍记着进过监听段**（否则页面会显示「监听 · 无（本窗未达标）」,
	// 而信号恰恰是监听段出的）。
	if l := e.Latches(); !l.Listening || !l.Signal {
		t.Fatalf("监听段的信号: Listening 与 Signal 应同时为真, 得到 %+v", l)
	}
	noRow(t, e, 55)
}

// TestEngineListeningUsesRule2NotRule5 监听段的口径是 ②（无 σ 腿）——一条「⑤ 成立但 ② 不成立」
// 的 tick（dev 在 [sd, 63) 之间）在监听段**不该**下单: 那正是 ⑤ 与 ② 的差别所在。
func TestEngineListeningUsesRule2NotRule5(t *testing.T) {
	e := newTestEngine(t, 4) // sd = 40 美元
	if o := row(t, e, 149, quietRow); o.OK {
		t.Fatalf("第一段应被拒, 得到 %+v", o)
	}
	// dev = +45: ⑤ 成立（sd=40 ≥ 40 且 dev ≥ sd）但 ② 不成立（dev < 63）。
	mid := func(x *flip.Tick) { x.BinPrice = testAnchor + 45 }
	if o := row(t, e, 59, mid); !o.OK || !o.Rules.Rule5() {
		t.Fatalf("第二段该 tick 应构成 ⑤, 得到 ok=%v rules=%+v", o.OK, o.Rules)
	}
	// 换一个窗: 同样 dev=+45 落在**监听段** ⇒ 一行不落。
	e2 := newTestEngine(t, 4)
	row(t, e2, 149, quietRow)
	row(t, e2, 59, quietRow)
	noRow(t, e2, 58, mid)
}

// TestEngineLateJoinSkipsT150 迟到接入（首个可判定 tick 已在 rem ≤ 60）: 跳过第一段,
// **不伪造 t150 行**（那一 tick 本来就不存在）。
func TestEngineLateJoinSkipsT150(t *testing.T) {
	e := newTestEngine(t, 10)
	o := row(t, e, 50)
	if o.Stage != StageT60 || o.FrameT != 60 || o.Rem != 50 {
		t.Fatalf("迟到接入应直接产 t60 行, 得到 stage=%s frameT=%d rem=%d", o.Stage, o.FrameT, o.Rem)
	}
	// 本窗确实没有 T=150 判定行 → 页面上如实显示「T150 · 未判」。
	if l := e.Latches(); l.T150 || !l.T60 {
		t.Fatalf("迟到接入不应标 T150 已判, 得到 %+v", l)
	}
	if ws := e.WindowStats(); ws.Rows != 1 {
		t.Fatalf("一次调用应只产 1 行, 得到 %+v", ws)
	}
}

// ── tick 有效性 ──

// TestEngineInvalidTickDoesNotAdvance 无效 tick 不推进任何段——「首个 tick」指的是首个
// **可判定** tick（对应 python 在 `rem ≤ T` 判断之前的 continue）。
func TestEngineInvalidTickDoesNotAdvance(t *testing.T) {
	e := newTestEngine(t, 10)

	// 延迟超阈: 不占 valid、不产行。
	noRow(t, e, 140, func(x *flip.Tick) { x.BookLatMs = 301 })
	// 四档全空: 无有效价（**缺一侧不算**——见 TestEngineEmptySideFallsBackToBid）。
	noRow(t, e, 139, func(x *flip.Tick) { x.UpBid, x.UpAsk, x.DownBid, x.DownAsk = 0, 0, 0, 0 })

	// 首个可判定 tick（rem=138, 在闸内）才判定, 且判定的是**当前** tick 的快照。
	o := row(t, e, 138)
	if o.Rem != 138 || o.Stage != StageT150 {
		t.Fatalf("首个可判定 tick 应做第一段判定且 Rem=138, 得到 %+v", o)
	}

	ws := e.WindowStats()
	if ws.Ticks != 3 || ws.TicksValid != 1 || ws.BookStale != 1 || ws.BookMissing != 1 {
		t.Fatalf("健康度计数不符: %+v", ws)
	}
	if ws.Ticks != ws.TicksValid+ws.BookStale+ws.BookMissing {
		t.Fatalf("恒等式 Ticks == Valid + Stale + Missing 被破坏: %+v", ws)
	}
}

// TestEngineEmptySideFallsBackToBid 缺一侧仍可判定（a.md 第 2 条）: 每侧有效价 = ask 优先、
// bid 兜底; 本行记 hot_src 供审计（bid = 那一侧卖单被整侧撤空）。
func TestEngineEmptySideFallsBackToBid(t *testing.T) {
	// 热门侧（yes）ask 空 → 用 bid 0.90。
	e := newTestEngine(t, 10)
	o := row(t, e, 149, func(x *flip.Tick) { x.UpAsk = 0 })
	if o.Side != flip.SideYes || o.HotAsk != 0.90 || o.HotSrc != BookSrcBid {
		t.Fatalf("yes 侧 ask 空应兜底 bid 0.90, 得到 side=%s px=%.3f src=%s", o.Side, o.HotAsk, o.HotSrc)
	}
	if !o.OK {
		t.Fatalf("价格腿与位移腿都过, 应为信号: %+v", o)
	}
	if want := 2 / 0.90; o.Shares != want {
		t.Fatalf("股数应按兜底价算 = %.6f, 得到 %.6f", want, o.Shares)
	}

	// 两侧 ask 都空: 各自用 bid 比大小（yes 0.90 > no 0.07）。
	e2 := newTestEngine(t, 10)
	o2 := row(t, e2, 149, func(x *flip.Tick) { x.UpAsk, x.DownAsk = 0, 0 })
	if o2.Side != flip.SideYes || o2.HotAsk != 0.90 || o2.HotSrc != BookSrcBid {
		t.Fatalf("两侧 ask 都空应各用 bid 比较, 得到 %+v", o2)
	}

	// 只有 bid 的一侧 + 另一侧只有 ask: 仍是有效 tick（缺一侧不等于整簿无效）。
	e3 := newTestEngine(t, 10)
	o3 := row(t, e3, 149, func(x *flip.Tick) { x.UpBid, x.UpAsk = 0, 0 }) // yes 整侧空
	if o3.Side != flip.SideNo || o3.HotAsk != 0.09 || o3.HotSrc != BookSrcAsk {
		t.Fatalf("yes 整侧空时应选 no, 得到 %+v", o3)
	}
	if o3.RejectReason != RejectPriceLow {
		t.Fatalf("0.09 < 0.80 应记 price_low, 得到 %s", o3.RejectReason)
	}
}

// TestEngineNoSpotSkipsTick 缺 spot 的 tick 不落行、不推进段, **但也不整窗丢弃**:
// 下一个 spot 齐备的可判定 tick 照常判定（历史 14 天里这类 tick 一次也没有, 取更稳的口径）。
func TestEngineNoSpotSkipsTick(t *testing.T) {
	e := newTestEngine(t, 10)
	noRow(t, e, 149, func(x *flip.Tick) { x.BinPrice, x.SpotAgeMs = 0, 3000 })
	if l := e.Latches(); l.T150 {
		t.Fatalf("缺 spot 的 tick 不该推进段闩锁, 得到 %+v", l)
	}
	o := row(t, e, 148)
	if o.Stage != StageT150 || o.Rem != 148 || !o.OK {
		t.Fatalf("下一个可判定 tick 应照常判定, 得到 %+v", o)
	}
	if ws := e.WindowStats(); ws.SpotMissing != 1 || ws.TicksValid != 2 {
		t.Fatalf("缺 spot 应计入 SpotMissing（有效 tick 的子集）: %+v", ws)
	}
}

// TestEngineTwapAbsentDoesNotBlock ⑤ 与 ② 都不用 twap: 流值缺失不该影响判定
// （旧口径把「spot+twap 不同时在场」当无效, 现口径只看 spot）。
func TestEngineTwapAbsentDoesNotBlock(t *testing.T) {
	e := newTestEngine(t, 10)
	o := row(t, e, 149, func(x *flip.Tick) { x.TwapPrice, x.TwapAgeMs = 0, -1 })
	if !o.OK || o.Twap != 0 {
		t.Fatalf("twap 缺失不影响判定（仅诊断字段）: %+v", o)
	}
}

// TestEngineNoAnchorNoRows 锚始终未取到（取锚通道 20s 预算耗尽）⇒ 本窗一行不产出;
// 但 tick 计数照常（健康度仍要能落盘）。
func TestEngineNoAnchorNoRows(t *testing.T) {
	e := NewEngine(DefaultConfig())
	e.BeginWindow(0, 0)
	for _, rem := range []int{200, 149, 120, 59, 30} {
		if out := e.ProcessTick(tailTick(rem)); out != nil {
			t.Fatalf("无锚时 rem=%d 不该产出行, 得到 %+v", rem, *out)
		}
	}
	ws := e.WindowStats()
	if ws.TicksValid != 5 || ws.Rows != 0 {
		t.Fatalf("无锚: 有效 tick 照数、零产出, 得到 %+v", ws)
	}
	if a, h := e.WindowAnchor(); a != 0 || h != 0 {
		t.Fatalf("未命中锚应为 (0,0), 得到 (%.2f,%.2f)", a, h)
	}
}

// TestEngineAnchorFrozenAfterEmit 首行产出即冻结锚: 本窗各行必须共用同一 dev 基准。
func TestEngineAnchorFrozenAfterEmit(t *testing.T) {
	e := newTestEngine(t, 10)
	row(t, e, 149)
	if e.UpgradeAnchor(testAnchor+500, 12) {
		t.Fatal("首行已产出, 改锚必须被拒（否则前后两行基准不同源）")
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
	// anchor ≤ 0 一律拒收（精确匹配失败时推送值为 0 的防线）。
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

// TestEngineRemZeroEndsWindow rem==0 是终 tick: 不产出、转 Done; 之后的 tick 一律忽略。
func TestEngineRemZeroEndsWindow(t *testing.T) {
	e := newTestEngine(t, 10)
	if out := e.ProcessTick(tailTick(0)); out != nil {
		t.Fatalf("rem==0 不该产出, 得到 %+v", *out)
	}
	if e.State() != stateDone {
		t.Fatalf("rem==0 应转 Done, 实为 %v", e.State())
	}
	if ws := e.WindowStats(); ws.Ticks != 0 {
		t.Fatalf("终 tick 不计入 Ticks, 得到 %+v", ws)
	}
	noRow(t, e, 120)
}

// ── 判定与派生量 ──

// TestEngineRejectReasons 覆盖三类拒绝（missing_spot 已不落行, 见 TestEngineNoSpotSkipsTick）。
func TestEngineRejectReasons(t *testing.T) {
	cases := []struct {
		name string
		mut  func(*flip.Tick)
		want string
		ok   bool
	}{
		{"ok", func(*flip.Tick) {}, "", true},
		{"price_low", func(x *flip.Tick) { x.UpAsk = 0.79 }, RejectPriceLow, false},
		// 价格腿过、两条腿都不过: dev=10 < 63, 且 dev < sd(=100)。
		{"leg_out", quietRow, RejectLegOut, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			e := newTestEngine(t, 10)
			o := row(t, e, 149, c.mut)
			if o.OK != c.ok || o.RejectReason != c.want {
				t.Fatalf("ok=%v reason=%q, 期望 ok=%v reason=%q", o.OK, o.RejectReason, c.ok, c.want)
			}
			if o.OK && o.Shares <= 0 {
				t.Fatalf("ok 行必须有正股数: %+v", o)
			}
			if !o.OK && o.Shares != 0 {
				t.Fatalf("未成信号的行不该有股数: %+v", o)
			}
		})
	}
}

// TestEngineNoSigmaRejectsWithNoHist 纯函数防线: σ 不可用（hist_bps=0）时判定行记 no_hist。
// 现网由 cmd/tail 的前置闸整窗跳过（skip=no_sigma）, 到不了这里。
func TestEngineNoSigmaRejectsWithNoHist(t *testing.T) {
	e := NewEngine(DefaultConfig())
	e.BeginWindow(0, 0)
	if !e.UpgradeAnchor(testAnchor, 0) {
		t.Fatal("锚注入应被采纳（σ=0 只表示不可用）")
	}
	o := row(t, e, 149)
	if o.RejectReason != RejectNoHist {
		t.Fatalf("σ 不可用应记 no_hist, 得到 %+v", o)
	}
	if o.Rules.Sigma || o.Rules.SigmaUSD40 {
		t.Fatalf("σ 不可用时 σ 腿必须为 false: %+v", o.Rules)
	}
}

// TestEngineSideAndDevSign 热门侧 = 有效价高的一侧（平局取 yes）; dev 的符号随侧别翻转。
func TestEngineSideAndDevSign(t *testing.T) {
	// no 侧为热门（0.92 > 0.10）, 且现货在锚**下方** 100 美元 ⇒ 押 no 的 dev = +100。
	// twap 也在锚下方 60 ⇒ walk = +60 过 T=150 的入场闸（否则这一窗会被 walk_low 拦下,
	// 见 TestEngineWalkGateBlocksGapOnlyT150 —— 本用例只关心侧别与符号）。
	e := newTestEngine(t, 10)
	o := row(t, e, 149, func(x *flip.Tick) {
		x.UpAsk, x.UpBid = 0.10, 0.09
		x.DownAsk, x.DownBid = 0.92, 0.90
		x.BinPrice = testAnchor - 100
		x.TwapPrice = testAnchor - 60
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
	// 分解恒等式: dev = basis(sgn·(spot−twap)) + walk(sgn·(twap−anchor))。
	if w, b := WalkUSD(o.Side, o.Twap, o.Anchor), SgnFor(o.Side)*(o.Spot-o.Twap); w+b != o.Dev {
		t.Fatalf("dev 分解破了: walk %+.4f + basis %+.4f ≠ dev %+.4f", w, b, o.Dev)
	}

	// 平局取 yes（python: `"yes" if ya >= na else "no"`）。
	e2 := newTestEngine(t, 10)
	o2 := row(t, e2, 149, func(x *flip.Tick) { x.UpAsk, x.DownAsk = 0.18, 0.18 })
	if o2.Side != flip.SideYes {
		t.Fatalf("有效价平局应取 yes, 得到 %s", o2.Side)
	}
}

// TestEngineWalkGateBlocksGapOnlyT150 钉住入场闸在**引擎链上**的真实形态（决策 #29）:
//
//	dev = +100（⑤ 成立）但 walk = −50（结算线还在锚的另一侧）⇒ 段 1 落一行 walk_low,
//	**链继续**——紧接着的 T=60 段照常判（同一批 tick 上 walk 已长好 ⇒ 该段出信号）。
//
// 这一形态正是闸要拦的那批: `dev` 全由**缺口**（basis = +150）撑起来, 而缺口是领先量、
// 还没写进结算线（"便宜价编码的是还没定局"）。若哪天有人把闸挪到 leg_out 之前、
// 或让它对整窗生效, 本用例会红。
func TestEngineWalkGateBlocksGapOnlyT150(t *testing.T) {
	e := newTestEngine(t, 10) // sd = 100 美元
	// 押 no（热门侧）: 现货在锚**下方** 100 ⇒ dev = +100; 而 twap 还在锚**上方** 50
	// ⇒ walk = −50（结算线在锚的反侧, 一点没往押注方向走）, 缺口 basis = +150 撑起全部 dev。
	o := row(t, e, 149, func(x *flip.Tick) {
		x.UpAsk, x.UpBid = 0.10, 0.09
		x.DownAsk, x.DownBid = 0.92, 0.90
		x.BinPrice = testAnchor - 100 // 押 no: dev = +100
		x.TwapPrice = testAnchor + 50 // walk = −50 < 43 ⇒ 闸拦下
	})
	if o.Stage != StageT150 || o.OK {
		t.Fatalf("段 1 应被入场闸拦下, 得到 stage=%s ok=%v", o.Stage, o.OK)
	}
	if o.RejectReason != RejectWalkLow {
		t.Fatalf("拒绝原因应为 %s, 得到 %s", RejectWalkLow, o.RejectReason)
	}
	// 闸的**判据语义**: 它只在 ⑤ 已达标时才会出现（否则读 leg_out）。
	if !o.Rules.Rule5() {
		t.Fatalf("walk_low 行必须 ⑤ 达标（否则该读 leg_out）: %+v", o.Rules)
	}
	if lim, got := DefaultConfig().WalkMinUSD, WalkUSD(o.Side, o.Twap, o.Anchor); got >= lim {
		t.Fatalf("本用例的前提是 walk < %.0f（默认阈值）, 实为 %+.4f", lim, got)
	}

	// 链继续: T=60 段照常判, 且此时 walk 已长好（twap 也翻到锚下方）⇒ 该段出信号。
	o2 := row(t, e, 59, func(x *flip.Tick) {
		x.UpAsk, x.UpBid = 0.10, 0.09
		x.DownAsk, x.DownBid = 0.95, 0.94
		x.BinPrice = testAnchor - 100
		x.TwapPrice = testAnchor - 70 // walk = +70
	})
	if o2.Stage != StageT60 || !o2.OK {
		t.Fatalf("段 2 应出信号（闸只闸段 1）, 得到 stage=%s ok=%v reject=%s",
			o2.Stage, o2.OK, o2.RejectReason)
	}
}

// TestEngineWalkGateThresholdFromConfig 钉住「闸的阈值取自**引擎持有的 cfg**」这条接线
// （决策 #29 的阈值在同日稍后由常量改成配置键 `tail.walk_min_usd`, 为的是 ETH 等标的各带
// 一份自己的标定值）——同一批 tick 换个 cfg 就该换结果, 阈值不能是摆设、更不能被
// decision() 里某个写死的默认值盖掉。
func TestEngineWalkGateThresholdFromConfig(t *testing.T) {
	// 押 no（热门侧）: spot 在锚下方 100 ⇒ dev = +100; twap 在锚下方 60 ⇒ walk = +60。
	// ⇒ 默认阈值 43 下是信号, 阈值抬到 200 后同一 tick 落 walk_low。
	tick := func(x *flip.Tick) {
		x.UpAsk, x.UpBid = 0.10, 0.09
		x.DownAsk, x.DownBid = 0.92, 0.90
		x.BinPrice = testAnchor - 100
		x.TwapPrice = testAnchor - 60
	}
	loose := DefaultConfig() // 43
	e1 := NewEngine(loose)
	e1.BeginWindow(0, 0)
	if !e1.UpgradeAnchor(testAnchor, 10) {
		t.Fatal("窗口开局注入锚被拒")
	}
	if o := row(t, e1, 149, tick); !o.OK || o.Stage != StageT150 {
		t.Fatalf("默认阈值 43 下 walk=+60 该出信号, 得到 ok=%v stage=%s reject=%s",
			o.OK, o.Stage, o.RejectReason)
	}

	strict := DefaultConfig()
	strict.WalkMinUSD = 200
	e2 := NewEngine(strict)
	e2.BeginWindow(0, 0)
	if !e2.UpgradeAnchor(testAnchor, 10) {
		t.Fatal("窗口开局注入锚被拒")
	}
	o := row(t, e2, 149, tick)
	if o.OK || o.RejectReason != RejectWalkLow {
		t.Fatalf("阈值抬到 200 后同一 tick 该落 %s, 得到 ok=%v reject=%s",
			RejectWalkLow, o.OK, o.RejectReason)
	}
}

// TestEngineListenFloorShadowAndContinue 监听段**价格地板**（决策 #32）:
// ② 达标但有效价 ≤ listen_min_price（默认 0.83）⇒ 落一行**影子行**
//（ok=false, reject_reason=floor_low）且**链继续**（同段等更贵的 tick）;
// 影子行**每窗至多一条**（无地板世界里只有第一个 ② 达标的 tick 会成交）。
func TestEngineListenFloorShadowAndContinue(t *testing.T) {
	e := newTestEngine(t, 10) // sd = 100 美元
	// 前两段各落一条 leg_out（quietRow: dev=+10 两条腿都不过）⇒ 进监听段。
	if o := row(t, e, 149, quietRow); o.OK {
		t.Fatalf("第一段应被拒, 得到 %+v", o)
	}
	if o := row(t, e, 59, quietRow); o.OK {
		t.Fatalf("第二段应被拒, 得到 %+v", o)
	}

	// rem=41, 有效价 0.81（② 达标: 价格腿 ≥0.80 ∧ dev=+100 ≥ 63）但过不了地板
	// ⇒ 影子行: ok=false、reason=floor_low、stage=listen、本 tick 本就是一笔信号。
	o := row(t, e, 41, func(x *flip.Tick) { x.UpAsk, x.UpBid = 0.81, 0.80 })
	if o.OK {
		t.Fatalf("被地板拦下的 tick 不该是信号行: %+v", o)
	}
	if o.Stage != StageListen || o.RejectReason != RejectFloorLow {
		t.Fatalf("影子行应为 listen/%s, 得到 %s/%q", RejectFloorLow, o.Stage, o.RejectReason)
	}
	if !o.Rules.Rule2() {
		t.Fatalf("floor_low 行必须 ② 达标（否则该零产出）: %+v", o.Rules)
	}
	// 影子行**不是信号**: 闩锁的 Signal 为假（防双单/结算都看这个）。
	if l := e.Latches(); l.Signal || !l.Listening {
		t.Fatalf("影子行不该点亮 Signal 闩锁, 得到 %+v", l)
	}

	// rem=40 有效价 0.82: 同样过不了地板, 但影子行只落一条 ⇒ 零产出（否则每窗会
	// 被廉价 tick 刷屏）。链没有停在影子行上——它还在监听。
	noRow(t, e, 40, func(x *flip.Tick) { x.UpAsk, x.UpBid = 0.82, 0.81 })

	// rem=39 有效价 0.90 过了地板 ⇒ 本窗唯一的信号行, 股数按**有效价**算。
	sig := row(t, e, 39, func(x *flip.Tick) { x.UpAsk, x.UpBid = 0.90, 0.89 })
	if !sig.OK || sig.Stage != StageListen || sig.RejectReason != "" {
		t.Fatalf("过地板的 tick 应出信号行, 得到 ok=%v stage=%s reason=%q",
			sig.OK, sig.Stage, sig.RejectReason)
	}
	if want := 2 / 0.90; sig.Shares != want {
		t.Fatalf("股数 = stake/有效价 = %.6f, 得到 %.6f", want, sig.Shares)
	}
	noRow(t, e, 38) // 出信号即 Done
}

// TestEngineListenFloorOnlyListenAndFromConfig 两件事: ① 地板**只管监听段**
//（T=60 段的 0.82 照常成信号, 与决策 #26「段相关腿只此一条」的纪律同型）;
// ② 阈值取自**引擎持有的 cfg**（配置键 `tail.listen_min_price` 是给其他标的重标定
// 用的, 不能是摆设）。
func TestEngineListenFloorOnlyListenAndFromConfig(t *testing.T) {
	// ① 迟到接入 ⇒ 首个可判定 tick 直接进 T=60 段: 0.82 在**段 2**该出信号。
	e := newTestEngine(t, 10)
	if o := row(t, e, 59, func(x *flip.Tick) { x.UpAsk, x.UpBid = 0.82, 0.81 }); !o.OK || o.Stage != StageT60 {
		t.Fatalf("段 2 不受监听段地板约束（0.82 应出信号）, 得到 ok=%v stage=%s reason=%q",
			o.OK, o.Stage, o.RejectReason)
	}

	// ② 同一批 tick、阈值抬到 0.95: 0.90 被拦成影子行、0.96 才过。
	cfg := DefaultConfig()
	cfg.ListenMinPrice = 0.95
	e2 := NewEngine(cfg)
	e2.BeginWindow(0, 0)
	if !e2.UpgradeAnchor(testAnchor, 10) {
		t.Fatal("窗口开局注入锚被拒")
	}
	row(t, e2, 149, quietRow)
	row(t, e2, 59, quietRow)
	o := row(t, e2, 41, func(x *flip.Tick) { x.UpAsk, x.UpBid = 0.90, 0.89 })
	if o.OK || o.RejectReason != RejectFloorLow {
		t.Fatalf("阈值 0.95 下 0.90 该落 %s, 得到 ok=%v reason=%q", RejectFloorLow, o.OK, o.RejectReason)
	}
	if sig := row(t, e2, 40, func(x *flip.Tick) { x.UpAsk, x.UpBid = 0.96, 0.95 }); !sig.OK {
		t.Fatalf("阈值 0.95 下 0.96 该出信号, 得到 ok=%v reason=%q", sig.OK, sig.RejectReason)
	}
}

// TestEngineSdScalesWithAnchor 钉住 sd = hist_bps·anchor/1e4（美元）这一口径
// ——把 bps 当美元用是这条策略最容易出现的量级错误（文档 §4 的 n=17 事故）。
func TestEngineSdScalesWithAnchor(t *testing.T) {
	e := newTestEngine(t, 9.26)
	o := row(t, e, 149, quietRow)
	if want := 9.26 * testAnchor / 1e4; o.Sd != want { // = 92.6 美元
		t.Fatalf("sd 应为 hist_bps·anchor/1e4 = %.4f 美元, 得到 %.4f", want, o.Sd)
	}
}

// ── 换窗与崩溃续跑 ──

// TestEngineBeginWindowResets 换窗必须彻底重置（闩锁/监听段/统计/锚）。
func TestEngineBeginWindowResets(t *testing.T) {
	e := newTestEngine(t, 10)
	row(t, e, 149, quietRow)
	row(t, e, 59, quietRow)
	e.BeginWindow(0, 0)
	if e.State() != stateWatching {
		t.Fatalf("换窗后应回 Watching, 实为 %v", e.State())
	}
	if l := e.Latches(); l.T150 || l.T60 || l.Listening || l.Signal || l.Frozen {
		t.Fatalf("换窗后闩锁应全清, 得到 %+v", l)
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
	noRow(t, e, 149)
	if !e.UpgradeAnchor(testAnchor, 10) {
		t.Fatal("新窗注入锚应被采纳")
	}
	if o := row(t, e, 149); o.Stage != StageT150 {
		t.Fatalf("换窗后段闩锁应复位（可再判第一段）, 得到 %+v", o)
	}
}

// TestEngineResume 崩溃重启续跑: 按磁盘真相回填已完成的段, 不重复判定、不重复下单。
func TestEngineResume(t *testing.T) {
	// 磁盘上已有 t150 判定行（被拒）→ 续跑第二段。
	e := newTestEngine(t, 10)
	e.Resume(true, false)
	if e.State() != stateAwait60 {
		t.Fatalf("只完成第一段应续在 Await60, 实为 %v", e.State())
	}
	if l := e.Latches(); !l.T150 || l.T60 {
		t.Fatalf("回填后 T150 应为真、T60 为假, 得到 %+v", l)
	}
	// 锚未冻结（首行的锚由同一条边界推送确定性重取）。
	if !e.UpgradeAnchor(testAnchor, 10) {
		t.Fatal("Resume 不该冻结锚（否则重启后 UpgradeAnchor 被永久拒收）")
	}
	noRow(t, e, 100)
	if o := row(t, e, 59); o.Stage != StageT60 || !o.OK {
		t.Fatalf("续跑应只做第二段, 得到 %+v", o)
	}

	// 两段都完成 → 直接进监听段（不再判定, 只等 ②）。
	e2 := newTestEngine(t, 10)
	e2.Resume(true, true)
	if e2.State() != stateListening {
		t.Fatalf("两段都完成应续在 Listening, 实为 %v", e2.State())
	}
	if l := e2.Latches(); !l.T150 || !l.T60 || !l.Listening || l.Signal {
		t.Fatalf("回填后前三闸为真、Signal 为假, 得到 %+v", l)
	}
	noRow(t, e2, 59, quietRow)
	if o := row(t, e2, 58); o.Stage != StageListen {
		t.Fatalf("续跑直接进监听段, 得到 %+v", o)
	}

	// 都没完成 → 从头跑。
	e3 := newTestEngine(t, 10)
	e3.Resume(false, false)
	if o := row(t, e3, 149); o.Stage != StageT150 {
		t.Fatalf("无完成段应从头跑, 得到 %+v", o)
	}
}
