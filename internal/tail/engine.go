package tail

import (
	"sync"

	"github.com/necklace/flip-signal/internal/flip"
)

// Engine 是「扫尾盘」的三段判定状态机。
//
// 每 300s 窗口重置一次（或每窗新建实例）: 在**有效 tick** 上按时间腿递进判三次，
// **任一段出信号即整窗只下一单**（此后不再判定）:
//
//	Watching  ──首个可判定 tick 且 rem ≤ t150_rem──▶ [判 ⑤]
//	   ├─ 达标 → 落信号行（下单）→ Done
//	   └─ 不达标 → Await60
//	Await60   ──首个可判定 tick 且 rem ≤ t60_rem──▶ [判 ⑤]
//	   ├─ 达标 → 落信号行（下单）→ Done
//	   └─ 不达标 → Listening
//	Listening ──此后**每秒**: 可判定 tick ∧ ② 达标──▶ 落信号行（下单）→ Done
//	   └─ rem == 0 → Done
//
// 本包零第三方依赖、无 I/O, 判定输入全部经 Tick / BeginWindow 注入, 可独立测试：
//   - **可判定 tick**（本文件里一律简称「可判定」）= `BookLatMs ≤ MaxBookLatMs`
//     ∧ 盘口有效价 > 0（`ask` 优先、`bid` 兜底, 见 decide.HotBook）
//     ∧ **spot > 0** ∧ rem > 0。无效 tick 占 Ticks 计数但**不推进任何段**——
//     对应 python 在 `rem ≤ T` 判断之前的 `continue`, 即「首个 tick」指首个**可判定** tick。
//     ⚠️ 缺 spot 的 tick 走「跳过」而非「整窗丢弃」（2026-09-24 起）: 旧口径一缺
//     spot 就把该窗判成 missing_spot 并 Done, 而历史 14 天里判定 tick 上 spot 从不
//     缺失（§5）——取更稳的那条, 差异是 0 窗。
//   - **锚未就绪**（anchor ≤ 0, 取锚通道尚未精确命中边界那一秒的推送）: 同样不推进
//     任何段。时序上几乎不会发生——取锚通道在边界 +20s 结束, 而最早的判定点在
//     rem≤150（边界 +150s）,**锚在产出任何行之前早已定局**。始终未取到 = 本窗一行
//     不产出（cmd/tail 在 tailstats 记 anchor_missing=true）, 与 flip 决策 #15
//     「宁可丢窗也不拿近似锚判定」同一原则（观测行 anchor 恒 >0）。
//   - **迟到接入**（首个可判定 tick 已 rem ≤ t60_rem）: 跳过第一段（那一 tick 本就
//     不存在, 不伪造 t150 行）, 直接在 T=60 判一次 ⑤。
//   - **崩溃重启续跑**（见 Resume）: 已完成的段不重复判, 未完成的段照常跑。
//   - rem ≤ 0 终 tick 处理完置 Done（不产出观测）。
type Engine struct {
	mu sync.Mutex

	cfg   Config
	state engineState

	anchor  float64 // 本窗锚 = 边界那一秒的 TWAP 推送值（≤0 = 尚未取到）
	histBps float64 // σ: 前 ≤18 个已完窗 |tw_close−tw_open| 均值换算 bps（≤0 = 不可用）

	t150Sent bool // 第一段（rem≤T150Rem 判 ⑤）已做过
	t60Sent  bool // 第二段（rem≤T60Rem 判 ⑤）已做过
	// listenEntered 已进入监听段（单调, 出信号/闭市都不回退）。与 `state ==
	// stateListening` 的差别只在「本段出了信号」的那一刻: 那时 state 已转 Done,
	// 但 dashboard 的闩锁仍应显示「已进入监听段」（Latches.Listening 的语义）。
	listenEntered bool
	// signal 本窗已出信号（= 已落信号行并下单）: 单调, 置真即 Done——三段链的
	// 「整窗只下一单」就靠它, 也是 ProcessTick 唯一的幂等守卫。
	signal  bool
	emitted bool // 本窗已产出过任何行 → 锚冻结（UpgradeAnchor 拒收）

	stats WindowStats // 本窗 tick 健康度（纯计数, 不参与判定; 每窗重置）
}

// Latches 是本窗进度 + 锚冻结状态的只读快照（dashboard 展示本窗进度用）:
// T150/T60 = 该段判定已做（无论结果）/ Listening = 已进入监听段（**单调**: 本段出了
// 信号转了 Done 之后仍为真, 否则页面会显示「监听 · 无（本窗未达标）」而信号恰恰来自
// 监听段）/ Signal = 已出信号（= 已下单）/ Frozen = 已产出过行（锚自此冻结, 也是
// UpgradeAnchor 此后拒收的判据）。判定路径不读它。
type Latches struct {
	T150      bool `json:"t150"`
	T60       bool `json:"t60"`
	Listening bool `json:"listening"`
	Signal    bool `json:"signal"`
	Frozen    bool `json:"frozen"`
}

// NewEngine 创建一个处于 Watching 态的空引擎（窗口上下文由 BeginWindow 注入）。
func NewEngine(cfg Config) *Engine {
	return &Engine{cfg: cfg, state: stateWatching}
}

// BeginWindow 重置引擎并注入窗口上下文。与 flip 同口径: cmd/tail 恒以
// `BeginWindow(0, 0)` 开局（**不设过渡锚**——t=0 的 Latest() 是「边界前一秒」的
// 到达口径近似, 与官方 openPrice 差 p90 0.24bps）, 锚一律由 UpgradeAnchor 在
// 窗口内精确命中边界那一秒的推送后注入。
func (e *Engine) BeginWindow(anchor, histBps float64) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.state = stateWatching
	e.anchor = anchor
	e.histBps = histBps
	e.t150Sent = false
	e.t60Sent = false
	e.listenEntered = false
	e.signal = false
	e.emitted = false
	e.stats = WindowStats{}
}

// Resume 按磁盘真相回填**已完成**的段（崩溃重启重入同一窗口时调用, 紧随 BeginWindow）。
//
//	t150Done/t60Done = 该窗磁盘上已有对应 stage 的判定行（Recorder.HasStage）。
//	两个都完成 ⇒ 直接进监听段; 都没完成 ⇒ 从头跑。
//
// ⚠️ **`emitted` 保持 false**（不在这里置真）: 它的语义是「锚已冻结」, 而重启后锚值
// 由同一条边界推送**确定性重取**（PushNearest 精确匹配那一秒, 决策 #15）——若因
// 「磁盘上已有行」就置真, UpgradeAnchor 会被自己的冻结判据永久拒收, 锚再也进不来,
// 本窗一行都产不出。
//
// 调用前置条件: 该窗**尚无 OK 行**（cmd/tail 用 Recorder.HasSignal 判, 有信号就整窗
// 跳过）——所以本函数不必也不能设置 signal（signal 是「已下单」, 不是「已判定」）。
func (e *Engine) Resume(t150Done, t60Done bool) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.t150Sent = t150Done
	e.t60Sent = t60Done
	switch {
	case t60Done:
		// 两段都做完了（t60Done 蕴含 t150Done 已完成, 否则不会到 T=60）
		e.state = stateListening
		e.listenEntered = true
	case t150Done:
		e.state = stateAwait60
	default:
		e.state = stateWatching
	}
}

// UpgradeAnchor 注入本窗锚与 σ（取锚通道精确命中边界那一秒的推送后调用）, 返回是否被采纳。
//
// 相对「窗口级常量锚」的唯一让步: 锚在**产出任何行之前**可变。首行落盘即冻结——
// 本窗各行必须共用同一个锚, 否则前后两行的 dev 基准不同源、无法互相解释（且 dev 的
// 分子锚、分母 σ 必须同源同换, 只换其一会让判定基准自相矛盾）。
//
// 注入**不追溯**注入前的 tick（那些 tick 锚未就绪、根本没做判定）。anchor ≤ 0 一律拒收。
func (e *Engine) UpgradeAnchor(anchor, histBps float64) bool {
	if !(anchor > 0) {
		return false
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.state == stateDone || e.emitted {
		return false // 已产出观测（锚已冻结）或窗口已结束
	}
	e.anchor = anchor
	e.histBps = histBps
	return true
}

// WindowAnchor 返回本窗最终生效的锚与 σ（bps）: 精确命中过 = 边界那一秒的评估值;
// 始终未命中 = (0, 0)（本窗不产出观测、也不计入 σ）。
// cmd/tail 在取锚通道 cancel + join 之后调用（保证无并发写）。
func (e *Engine) WindowAnchor() (anchor, histBps float64) {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.anchor, e.histBps
}

// State 返回当前状态（日志用）。
func (e *Engine) State() engineState {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.state
}

// Config 返回引擎参数副本（cmd/tail 取 Stake/T60Rem 等）。
func (e *Engine) Config() Config {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.cfg
}

// ProcessTick 处理一个 1s tick, 返回本 tick 产出的行（至多一行, nil = 无产出）。
//
// 三段递进的分支走向见 Engine 类型注释。返回的行**只有两种形态**:
//   - 判定行（stage = t150/t60）: 成功与否都产出（OK=false 带 RejectReason）;
//   - 信号行（stage = listen）: 只在 ② 达标时产出。
//
// 无论哪种, **只有 OK=true 的行会被执行编排下单**（cmd/tail 单点 dispatch）。
func (e *Engine) ProcessTick(t flip.Tick) *Observation {
	e.mu.Lock()
	defer e.mu.Unlock()

	if e.state == stateDone {
		return nil // 窗口已结束 / 本窗已下单, 不再观测（无重试）
	}
	if t.Rem <= 0 {
		e.state = stateDone // rem==0 终 tick: 窗口结束
		return nil
	}

	// 以下计数器只累加, 不改变任何分支走向（对账红线）。
	e.stats.Ticks++
	if t.BookLatMs > e.cfg.MaxBookLatMs {
		e.stats.BookStale++
		return nil
	}
	// 盘口有效价（ask 优先、bid 兜底）: 四档全空才算「无有效盘口」而无效。
	side, px, src := HotBook(t.UpBid, t.UpAsk, t.DownBid, t.DownAsk)
	if !(px > 0) {
		e.stats.BookMissing++
		return nil
	}
	e.stats.TicksValid++
	// 锚未就绪 / 缺 spot: 占槽与计数照常, 但不推进任何段（dev 无锚或缺价无法计算,
	// 强判即失真）。缺 spot 的 tick 等下一个可判定 tick——不整窗丢弃（见类型注释）。
	if !(e.anchor > 0) {
		return nil
	}
	if !(t.BinPrice > 0) {
		e.stats.SpotMissing++
		return nil
	}

	var o Observation
	advance := false
	switch e.state {
	case stateWatching:
		if t.Rem > e.cfg.T150Rem {
			return nil // 还没到第一段判定点（T=150）——早于它的 tick 只占槽, 不判定
		}
		if t.Rem > e.cfg.T60Rem {
			e.t150Sent = true
			e.state = stateAwait60
			o = e.decision(t, side, px, src, StageT150, e.cfg.T150Rem)
			advance = true
			break
		}
		// 迟到接入（首个可判定 tick 已在 T=60 段）: 整段跳过, **不伪造 t150 行**
		//（那一 tick 本来就不存在）, 直接在 T=60 判一次。t150Sent 保持 false——
		// 本窗确实没有 T=150 判定行, 页面上如实显示「T150 · 未判」。
		fallthrough
	case stateAwait60:
		if t.Rem > e.cfg.T60Rem {
			return nil // 还没到 T=60 判定点（第一段刚判完, rem 尚在 60~150 之间）
		}
		e.t60Sent = true
		e.state = stateListening
		e.listenEntered = true
		o = e.decision(t, side, px, src, StageT60, e.cfg.T60Rem)
		advance = true
	case stateListening:
		// 监听段: 每秒判 ②, **只在达标时落行**（被拒的 tick 不落盘——每窗约 60 个
		// tick, 全落会淹没信号表）。
		cand := e.snapshot(t, side, px, src, StageListen, e.cfg.T60Rem)
		if !cand.Rules.Rule2() {
			return nil
		}
		cand.OK = true
		cand.Shares = e.cfg.Stake / px
		o = cand
		advance = true
	}
	if !advance {
		return nil
	}
	if o.OK {
		e.signal = true
		e.state = stateDone // 整窗只下一单: 出信号即止
	}
	e.emitted = true // 锚自此冻结（本窗各行共用同一锚）
	e.stats.Rows++
	return &o
}

// WindowStats 返回本窗健康度统计的拷贝（落盘用）。
func (e *Engine) WindowStats() WindowStats {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.stats
}

// Latches 返回本窗进度与锚冻结状态（只读, dashboard 展示本窗进度用）。
// 判定路径不读它。
func (e *Engine) Latches() Latches {
	e.mu.Lock()
	defer e.mu.Unlock()
	return Latches{
		T150:      e.t150Sent,
		T60:       e.t60Sent,
		Listening: e.listenEntered,
		Signal:    e.signal,
		Frozen:    e.emitted,
	}
}

// decision 采一条判定行并求 ⑤（t150/t60 段; 判定行**成功与否都落盘**）。
// 调用方已持锁、已保证本 tick 为可判定 tick（盘口有效价 > 0 ∧ spot > 0 ∧ anchor > 0）。
//
// 判定顺序固定（文档化, 复验按原因计数）: missing_spot → no_hist → price_low → leg_out。
// 前两条是纯函数防线: 缺 spot 的 tick 在 ProcessTick 就被挡下（不落行）, σ 未就绪由
// cmd/tail 整窗跳过——这里保留分支是为了让判定函数自身封闭（不依赖调用方的前置条件）。
func (e *Engine) decision(t flip.Tick, side string, px float64, src, stage string, frameT int) Observation {
	o := e.snapshot(t, side, px, src, stage, frameT)
	switch {
	case !(t.BinPrice > 0):
		o.RejectReason = RejectMissingSpot
	case !(e.histBps > 0):
		o.RejectReason = RejectNoHist
	case !o.Rules.Price:
		o.RejectReason = RejectPriceLow
	case !o.Rules.Rule5():
		o.RejectReason = RejectLegOut
	default:
		o.OK = true
		// 纸面目标股数 = Stake/有效价（精确除, 与回测 shares = stake/fill 恒等）;
		// live 的实际下单量由 trading.OrderSpecForObs 取 floor2。
		o.Shares = e.cfg.Stake / px
	}
	return o
}

// snapshot 采集一条快照行（原始字段 + 派生量 + 四腿; 判定由 decision/监听段各自完成）。
// 调用方已持锁、已保证 anchor > 0 且本 tick 为可判定 tick。
func (e *Engine) snapshot(t flip.Tick, side string, px float64, src, stage string, frameT int) Observation {
	o := Observation{
		Kind: KindSnap, Stage: stage, FrameT: frameT,
		Ts: t.Ts, Rem: t.Rem,
		YesBid: t.UpBid, YesAsk: t.UpAsk, NoBid: t.DownBid, NoAsk: t.DownAsk,
		Spot: t.BinPrice, Twap: t.TwapPrice,
		Anchor: e.anchor, HistBps: e.histBps,
		Side: side, HotAsk: px, HotSrc: src,
		BookLatMs: t.BookLatMs, SpotAgeMs: t.SpotAgeMs, TwapAgeMs: t.TwapAgeMs,
	}
	// 派生量: spot 在场才算（缺则留 0 = 未计算——但那种 tick 走不到这里）。
	// 判定行同样计算: 离线复算与 dashboard 现窗口读数都靠这些字段。
	o.Dev = DevUSD(side, t.BinPrice, e.anchor)
	o.Sd = SigmaUSD(e.histBps, e.anchor)
	// 价格腿按段取比较符（T=150 严格大于）——必须传 stage, 否则行里的
	// rules.price 会与 reject_reason 自相矛盾（见 EvalRules/PriceLeg）。
	o.Rules = EvalRules(e.cfg, stage, px, o.Dev, o.Sd, e.histBps > 0)
	return o
}
