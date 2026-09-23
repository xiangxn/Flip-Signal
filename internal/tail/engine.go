package tail

import (
	"sync"

	"github.com/necklace/flip-signal/internal/flip"
)

// Engine 是「扫尾盘」的快照状态机。
//
// 每 300s 窗口重置一次（或每窗新建实例）: 有效 tick 上按两个**独立的一次性闩锁**
// 取两帧——rem ≤ Config.FrameRem 首帧（原始快照, 只记录）与 rem ≤ Config.RemStart
// 首帧（决策快照, 判定 ⑤ 并执行）。产出决策快照后转 Done, 本窗不再检测。
//
// 本包零第三方依赖、无 I/O, 判定输入全部经 Tick / BeginWindow 注入, 可独立测试：
//   - **有效 tick** = BookLatMs ≤ MaxBookLatMs ∧ UP(≈yes)/DOWN(≈no) 双侧四档报价
//     齐全 ∧ rem > 0（镜像回测快照搜索的 gate: pm 存在 + 延迟 + 四档 > 0 + rem > 0）。
//     无效 tick 占 Ticks 计数但**不推进任何闩锁**——对应 python 在 `rem ≤ T` 判断
//     之前的 `continue`, 即「首帧」指的是首个**有效** tick。
//   - **锚未就绪**（anchor ≤ 0, 取锚通道尚未精确命中边界那一秒的推送）: 同样不推进
//     闩锁。时序上几乎不会发生——取锚通道在边界 +20s 就结束, 而最早的闩锁在
//     rem≤150（边界 +150s）,**锚在产出任何行之前早已定局**。始终未取到 = 本窗一行
//     不产出（cmd/tail 在 tailstats 记 anchor_missing=true）, 与 flip 决策 #15
//     「宁可丢窗也不拿近似锚判定」同一原则（观测行 anchor 恒 >0）。
//   - **两个闩锁落在同一 tick**（首个有效 tick 已 rem ≤ RemStart, 例如进程在尾盘才
//     接上）: 两行同发、内容同源、FrameT 不同——与 python 对每个 T 各取一次首帧等价
//     （两个 T 的 snapshots() 本就会选中同一条 tick）。
//   - **帧行绝不下单**: 只有 Kind=KindSnap 且 OK 的行进执行编排（策略本体是 T=60）。
//   - rem ≤ 0 终 tick 处理完置 Done（不产出观测）。
type Engine struct {
	mu sync.Mutex

	cfg   Config
	state engineState

	anchor  float64 // 本窗锚 = 边界那一秒的 TWAP 推送值（≤0 = 尚未取到）
	histBps float64 // σ: 前 ≤18 个已完窗 |tw_close−tw_open| 均值换算 bps（≤0 = 不可用）

	frameSent bool // rem≤FrameRem 首帧已产出
	snapSent  bool // rem≤RemStart 首帧（决策快照）已产出
	emitted   bool // 本窗已产出过任何行 → 锚冻结（UpgradeAnchor 拒收）

	stats WindowStats // 本窗 tick 健康度（纯计数, 不参与判定; 每窗重置）
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
	e.frameSent = false
	e.snapSent = false
	e.emitted = false
	e.stats = WindowStats{}
}

// UpgradeAnchor 注入本窗锚与 σ（取锚通道精确命中边界那一秒的推送后调用）, 返回是否被采纳。
//
// 相对「窗口级常量锚」的唯一让步: 锚在**产出任何行之前**可变。首行落盘即冻结——
// 本窗的两行必须共用同一个锚, 否则帧行与快照行的 dev 基准不同源、无法互相解释
// （且 dev 的分子锚、分母 σ 必须同源同换, 只换其一会让判定基准自相矛盾）。
//
// 注入**不追溯**注入前的 tick（那些 tick 锚未就绪、根本没做判定）。anchor ≤ 0 一律拒收。
func (e *Engine) UpgradeAnchor(anchor, histBps float64) bool {
	if !(anchor > 0) {
		return false
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.state != stateWatching || e.emitted {
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

// Config 返回引擎参数副本（cmd/tail 取 Stake/RemStart 等）。
func (e *Engine) Config() Config {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.cfg
}

// ProcessTick 处理一个 1s tick, 返回本 tick 产出的观测（0 行 / 1 行 / 2 行）。
//
// 两行同发只出现在「首个有效 tick 已 rem ≤ RemStart」的情形（见 Engine 类型注释）。
// 调用方按 Kind 分流: frame 只落盘, snap 进执行编排。
func (e *Engine) ProcessTick(t flip.Tick) []Observation {
	e.mu.Lock()
	defer e.mu.Unlock()

	if e.state != stateWatching {
		return nil // 已判定/已结束, 本窗不再观测（无重试）
	}
	if t.Rem <= 0 {
		e.state = stateDone // rem==0 终 tick: 窗口结束
		return nil
	}

	// 以下计数器只累加, 不改变任何分支走向（对账红线）。
	e.stats.Ticks++
	// tick 无效（延迟过高 / 整簿报价不全）: 不推进闩锁, 等下一个有效 tick 取首帧。
	if t.BookLatMs > e.cfg.MaxBookLatMs {
		e.stats.BookStale++
		return nil
	}
	if !(t.UpBid > 0 && t.UpAsk > 0 && t.DownBid > 0 && t.DownAsk > 0) {
		e.stats.BookMissing++
		return nil
	}
	e.stats.TicksValid++

	// 锚未就绪: 占槽与计数照常, 但不推进闩锁（dev 无锚无法计算, 强判即失真）。
	if !(e.anchor > 0) {
		return nil
	}

	var out []Observation
	if !e.frameSent && t.Rem <= e.cfg.FrameRem {
		e.frameSent = true
		out = append(out, e.snapshot(t, KindFrame, e.cfg.FrameRem))
	}
	if !e.snapSent && t.Rem <= e.cfg.RemStart {
		e.snapSent = true
		out = append(out, e.snapshot(t, KindSnap, e.cfg.RemStart))
		e.state = stateDone // 决策快照已产出, 本窗不再观测
	}
	if len(out) > 0 {
		e.emitted = true // 锚自此冻结（两行共用同一锚）
		e.stats.Frames += len(out)
	}
	return out
}

// WindowStats 返回本窗健康度统计的拷贝（落盘用）。
func (e *Engine) WindowStats() WindowStats {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.stats
}

// snapshot 采集一条快照行（原始字段 + 派生量; snap 行另做判定）。
// 调用方已持锁、已保证 anchor > 0 且本 tick 为有效 tick。
func (e *Engine) snapshot(t flip.Tick, kind string, frameT int) Observation {
	// 热门侧 = ask 高的一侧（python: `side = "yes" if ya >= na else "no"`——平局取 yes）。
	side := flip.SideYes
	if t.DownAsk > t.UpAsk {
		side = flip.SideNo
	}
	sgn, hotAsk := 1.0, t.UpAsk
	if side == flip.SideNo {
		sgn, hotAsk = -1.0, t.DownAsk
	}

	o := Observation{
		Kind: kind, FrameT: frameT,
		Ts: t.Ts, Rem: t.Rem,
		YesBid: t.UpBid, YesAsk: t.UpAsk, NoBid: t.DownBid, NoAsk: t.DownAsk,
		Spot: t.BinPrice, Twap: t.TwapPrice,
		Anchor: e.anchor, HistBps: e.histBps,
		Side: side, HotAsk: hotAsk,
		BookLatMs: t.BookLatMs, SpotAgeMs: t.SpotAgeMs, TwapAgeMs: t.TwapAgeMs,
	}
	// 派生量: 输入齐备才算（缺则留 0 = 未计算, 由 RejectReason 区分）。
	// 帧行同样计算——T=150 的规则形态正是靠这些字段离线复算。
	if t.BinPrice > 0 {
		o.Dev = sgn * (t.BinPrice - e.anchor)
	}
	if e.histBps > 0 {
		o.Sd = e.histBps * e.anchor / 1e4
	}
	if kind != KindSnap {
		return o // 帧行只记录, 不带规则与判定
	}

	// 判定顺序固定（文档化, 复验按原因计数）:
	// missing_spot → missing_twap → no_hist → price_low → leg_out。
	// 前两条镜像 python 的整窗丢弃（**不**顺延到下一个有 spot 的 tick）。
	o.Rules = EvalRules(e.cfg, hotAsk, o.Dev, o.Sd, e.histBps > 0)
	switch {
	case !(t.BinPrice > 0):
		o.RejectReason = RejectMissingSpot
	case !(t.TwapPrice > 0):
		o.RejectReason = RejectMissingTwap
	case !(e.histBps > 0):
		// 纯函数防线: 现网由 cmd/tail 前置闸整窗跳过（skip=no_sigma）, 到不了这里。
		o.RejectReason = RejectNoHist
	case !o.Rules.Price:
		o.RejectReason = RejectPriceLow
	case !o.Rules.Rule5():
		o.RejectReason = RejectLegOut
	default:
		o.OK = true
		// 纸面目标股数 = Stake/hotAsk（精确除, 与回测 shares = stake/fill 恒等）;
		// live 的实际下单量由 trading.OrderSpecForObs 取 floor2。
		o.Shares = e.cfg.Stake / hotAsk
	}
	return o
}
