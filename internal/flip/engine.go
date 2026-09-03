package flip

import "sync"

// maxLat 与回测 MAX_LAT=300 一致: 盘口传输延迟超此值的 tick 无效
// （不参与触发检查；作为 0 值槽位占用窗口索引，不贡献急跌窗口 max）。
const maxLat = 300

// lookMax 是观测列 m_τ 的最大回看窗（tick 槽位）——急跌窗 ring 至少存这么深
// （python LOOKS=(20,30,45)，另有 60 仅作子版本参考未落地）。
const lookMax = 45

// Engine 是「狗@0.2」触底观测状态机。
//
// 每 300s 窗口重置一次（或每窗新建实例）：Watching 中每秒推入 ProcessTick，
// 首个有效 tick 上某侧 ask ∈ (0, TriggerAskMax] 即产出一次 Observation
// （一次性判定四腿，成功/失败都出观测），随即转 Done——本窗不再检测，
// 与回测「每事件仅首个观测、无重试」口径刻意一致。
//
// 判定输入全部经 Tick / BeginWindow 注入，本包零外部依赖、无副作用可测：
//   - 有效 tick = BookLatMs ≤ 300 且 UP(=yes)/DOWN(=no) 双侧 bid/ask 报价齐全
//     （整簿快照门控，镜像回测 :79；实测缺失为整行全空，只挡无快照行）
//   - 急跌窗 = 索引槽位 ring（先判后插：触发 tick 不进窗；无效 tick 压 0 占槽，
//     窗头自然截断——窗口未满取现有值，与回测合法短窗一致）
//   - dist = sgn·(price − anchor)/anchor·1e4 / histBps，sgn: dog=yes +1 / no −1
//   - rem==0 终 tick 处理完置 Done（不产出观测）
type Engine struct {
	mu sync.Mutex

	cfg   Config
	state engineState

	anchor  float64 // 窗口开盘 Chainlink TWAP-60 值（≤0 = 缺失）
	histBps float64 // σ: 前 ≤18 个已完窗 |tw_close−tw_open| 均值换算 bps（≤0 = 不可用）

	upAsks   []float64 // up/down 并行的 45+ 槽 ask ring（无效 tick 压 0）
	downAsks []float64
}

// NewEngine 创建一个处于 Watching 态的空引擎（窗口上下文由 BeginWindow 注入）。
func NewEngine(cfg Config) *Engine {
	return &Engine{cfg: cfg, state: stateWatching}
}

// BeginWindow 重置引擎并注入窗口上下文（窗口起点瞬间采样）：
// anchor 为开盘 Chainlink TWAP-60 值、histBps 为该时刻可用的 σ（bps），≤0 表示不可用。
func (e *Engine) BeginWindow(anchor, histBps float64) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.state = stateWatching
	e.anchor = anchor
	e.histBps = histBps
	e.upAsks = e.upAsks[:0]
	e.downAsks = e.downAsks[:0]
}

// State 返回当前状态（Dashboard 展示用）。
func (e *Engine) State() engineState {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.state
}

// Config 返回引擎参数副本。
func (e *Engine) Config() Config {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.cfg
}

// ProcessTick 处理一个 1s tick。首个有效触底 tick 返回观测（判定后转 Done，
// 触发 tick 不进急跌窗）；其余 tick 返回 nil。
func (e *Engine) ProcessTick(t Tick) *Observation {
	e.mu.Lock()
	defer e.mu.Unlock()

	if e.state != stateWatching {
		return nil // 已判定/已结束，本窗不再观测（无重试）
	}
	if t.Rem <= 0 {
		e.state = stateDone // rem==0 终 tick：窗口结束
		return nil
	}

	// tick 无效（延迟过高）：占槽不检——与回测一致，其后触发仍按索引计数。
	if t.BookLatMs > maxLat {
		e.pushSlots(t)
		return nil
	}
	// 整簿快照门控：UP/DOWN 四字段报价齐全才算有效 tick（镜像回测 :79）。
	// 实测报价缺失是整行全空（两侧同秒为 0），本检查只挡无快照行；
	// 任一侧报价不全 → 不参与触发（ask=0 本就无法触底）。
	if !(t.UpBid > 0 && t.UpAsk > 0 && t.DownBid > 0 && t.DownAsk > 0) {
		e.pushSlots(t)
		return nil
	}

	var obs *Observation
	switch {
	case t.UpAsk <= e.cfg.TriggerAskMax:
		// UP ask 触底 → 狗侧 yes（与回测判断顺序一致：两侧都 ≤0.2 取 yes）
		obs = e.decide(t, SideYes)
	case t.DownAsk > 0 && t.DownAsk <= e.cfg.TriggerAskMax:
		// DOWN ask 触底 → 狗侧 no（0<ask 守卫：盘口缺失的 0 绝不能触发）
		obs = e.decide(t, SideNo)
	}
	if obs != nil {
		e.state = stateDone
		return obs
	}
	e.pushSlots(t)
	return nil
}

// decide 在首个触底 tick 上一次判定（填充观测、检查四腿）。
// reject_reason 顺序固定：rem_low → no_hist → missing_spot → missing_anchor →
// no_crash → dist_out（文档化，复验按原因计数）。
func (e *Engine) decide(t Tick, side string) *Observation {
	cfg := e.cfg
	sgn := 1.0
	if side == SideNo {
		sgn = -1.0
	}
	fill := t.UpAsk
	if side == SideNo {
		fill = t.DownAsk
	}
	o := &Observation{
		Ts: t.Ts, Side: side, Rem: t.Rem, Fill: fill,
		BookLatMs: t.BookLatMs, TwapAgeMs: t.TwapAgeMs,
		// 决策原始输入随行快照（诊断: 浅洞带分解 / anchor·σ 口径差比对）
		Anchor:    e.anchor,
		HistBps:   e.histBps,
		Spot:      t.BinPrice,
		TwapPrice: t.TwapPrice,
	}

	// 回看窗口 max（先判后插：ring 尚未含本 tick；0 = 无有效 ask 同 NaN）
	ring := e.upAsks
	if side == SideNo {
		ring = e.downAsks
	}
	o.M20 = ringMax(ring, 20)
	o.M30 = ringMax(ring, 30)
	o.M45 = ringMax(ring, lookMax)

	// 特征与判定解耦：双口径 dist 在输入齐备时始终记录——python extract 同款
	// 逐行全特征（dist_s/dist_t 与 reject 分类无关，供按原因诊断复评）。
	if e.histBps > 0 && e.anchor > 0 && t.BinPrice > 0 {
		o.DistS = sgn * (t.BinPrice - e.anchor) / e.anchor * 1e4 / e.histBps
		if t.TwapPrice > 0 {
			// 观察腿 dist_t（TWAP 口径，不参与决策，供双层版复评）
			o.DistT = sgn * (t.TwapPrice - e.anchor) / e.anchor * 1e4 / e.histBps
		}
	}

	// reject_reason 顺序固定：rem_low → no_hist → missing_spot → missing_anchor
	// → no_crash → dist_out（文档化，复验按原因计数）。
	switch {
	case t.Rem <= cfg.RemMin:
		o.RejectReason = RejectRemLow
	case !(e.histBps > 0):
		o.RejectReason = RejectNoHist
	case !(t.BinPrice > 0):
		o.RejectReason = RejectMissingSpot
	case !(e.anchor > 0):
		o.RejectReason = RejectMissingAnchor
	case ringMax(ring, cfg.CrashWindow) < cfg.CrashMinAsk:
		// 急跌腿：窗口内曾 ≥ CrashMinAsk
		o.RejectReason = RejectNoCrash
	case !(o.DistS > cfg.DistLo && o.DistS < cfg.DistHi):
		// 浅洞腿：dist_s ∈ (DistLo, DistHi) 开区间
		o.RejectReason = RejectDistOut
	default:
		o.OK = true
		o.Shares = cfg.Stake / fill
	}
	return o
}

// pushSlots 将本 tick 追加为 up/down 两个并行的索引槽位（ask 保留或压 0），
// ring 超深则丢弃最老槽位。无效 tick（延迟 > maxLat）压 0 占槽。
func (e *Engine) pushSlots(t Tick) {
	up, down := 0.0, 0.0
	if t.BookLatMs <= maxLat {
		up, down = t.UpAsk, t.DownAsk
	}
	e.upAsks = pushRing(e.upAsks, up, max(e.cfg.CrashWindow, lookMax))
	e.downAsks = pushRing(e.downAsks, down, max(e.cfg.CrashWindow, lookMax))
}

// pushRing 追加一个槽位并裁剪到 cap。
func pushRing(ring []float64, v float64, cap int) []float64 {
	ring = append(ring, v)
	if len(ring) > cap {
		ring = append(ring[:0], ring[len(ring)-cap:]...)
	}
	return ring
}

// ringMax 返回最近 n 个槽位中的最大值（0 槽位不贡献；空窗/全 0 = 0）。
func ringMax(ring []float64, n int) float64 {
	if n > len(ring) {
		n = len(ring)
	}
	m := 0.0
	for _, v := range ring[len(ring)-n:] {
		if v > m {
			m = v
		}
	}
	return m
}
