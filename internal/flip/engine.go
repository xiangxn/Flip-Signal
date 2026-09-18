package flip

import "sync"

// lookMax 是观测列 m_τ 的最大回看窗（tick 槽位）——急跌窗 ring 至少存这么深
// （python LOOKS=(20,30,45)，另有 60 仅作子版本参考未落地）。
const lookMax = 45

// 丢信号原因（LostTrigger.Reason 取值）。
const (
	// LostReasonStaleBook 盘口延迟超阈（BookLatMs > MaxBookLatMs）。
	LostReasonStaleBook = "stale_book"
	// LostReasonBookMissing 整簿四字段报价不全（无快照行）。
	LostReasonBookMissing = "book_missing"
	// LostReasonAnchorPending 锚未就绪（边界采样缺失/陈旧，恢复通道尚未回填）：
	// 本 tick 连判定都进不去——dist_s 无锚无法计算，触底只能丢弃留痕。
	LostReasonAnchorPending = "anchor_pending"
)

// lostReason 归一 tick 的丢信号理由: 锚未就绪时锚闸是主因（该 tick 无论盘口
// 质量如何都判不了），记数据质量原因会掩盖「锚缺失期间全丢」这一事实；
// 盘口质量问题本身的计数（BookStale/BookMissing）不受影响，两者不丢信息。
func lostReason(anchorPending bool, qualityReason string) string {
	if anchorPending {
		return LostReasonAnchorPending
	}
	return qualityReason
}

// LostTrigger 是一个「本会触发但被数据质量闸挡掉」的 tick 明细。
//
// 为什么需要它（2026-09-16 补, docs/dog020_risk_latency_plan_2026-09-16.md §1.3）:
// 无效 tick 走 pushSlots 直接 return，若此刻某侧 ask 已砸到 ≤0.20，这个信号就凭空
// 消失——不进观测、不进 reject 原因统计、Dashboard 也看不到。09-15 OOS 复验的
// 信号频率闸门（实测 36.5/日 vs 计划 44±5）一直查不下去，缺的就是这条痕迹。
type LostTrigger struct {
	Ts        int64   `json:"ts"`          // 该 tick 采样时刻（unix 毫秒）
	Side      string  `json:"side"`        // 本会触发的狗侧（yes/no）
	Rem       int     `json:"rem"`         // 窗口剩余秒
	Ask       float64 `json:"ask"`         // 该侧 ask（≤ TriggerAskMax）
	BookLatMs int64   `json:"book_lat_ms"` // 该 tick 盘口延迟
	Reason    string  `json:"reason"`      // stale_book | book_missing | anchor_pending
}

// WindowStats 是本窗 tick 健康度统计（每窗无条件落盘一行, 见 Recorder.LogWindowStats）。
//
// 只计数、不参与任何判定——btreplay 与 01_backtest_r1.py 逐位对账是红线，
// 计数器禁止触碰 pushSlots / decide 的任何分支走向。
type WindowStats struct {
	// AnchorMissing 锚缺失: 本窗**结束时**锚仍未就绪（整窗不观测，镜像回测锚缺失
	// 事件跳过）。窗口内恢复通道回填锚后清除——恢复前占槽的 tick 照常计数，
	// 故本标记与非 0 计数可同时出现（见 ProcessTick 锚未就绪分支）。
	AnchorMissing bool `json:"anchor_missing,omitempty"`
	// Ticks 进入有效性分类的 tick 数（不含 rem==0 终 tick、不含 Done 后的 tick）。
	Ticks int `json:"ticks"`
	// TicksValid 有效 tick（过延迟闸 + 整簿门控）。
	TicksValid int `json:"ticks_valid"`
	// BookStale 延迟超阈而无效的 tick（占槽不检）。
	BookStale int `json:"book_stale"`
	// BookMissing 整簿四字段不全而无效的 tick（占槽不检）。
	BookMissing int `json:"book_missing"`
	// LostTriggers 本会触发但被上述两闸挡掉的 tick 明细（诊断核心）。
	LostTriggers []LostTrigger `json:"lost_triggers,omitempty"`
}

// 恒等（每窗落盘后可直接核对）: Ticks == TicksValid + BookStale + BookMissing

// Engine 是「狗@0.2」触底观测状态机。
//
// 每 300s 窗口重置一次（或每窗新建实例）：Watching 中每秒推入 ProcessTick，
// 首个有效 tick 上某侧 ask ∈ (0, TriggerAskMax] 即产出一次 Observation
// （一次性判定四腿，成功/失败都出观测），随即转 Done——本窗不再检测，
// 与回测「每事件仅首个观测、无重试」口径刻意一致。
//
// 判定输入全部经 Tick / BeginWindow 注入，本包零外部依赖、无副作用可测：
//   - 锚未就绪窗口（anchor ≤ 0，窗口级）**收集但闸住观测**：tick 照常占槽进
//     ring（crash 腿 m_45 与回测 1:1——回测里锚恒可用），但不做触发判定
//     （dist_s 无锚无法计算），期间触底记 lost_triggers(anchor_pending)。
//     SetAnchor 回填锚后本窗恢复判定能力（cmd/flip 的恢复通道：官方开盘价
//   - 边界级推送）；始终未回填 = 整窗不产出观测，镜像回测 :69 锚缺失事件
//     跳过（观测宇宙 1:1；missing_anchor 仅存 decide 纯函数防线，现网不可达）
//   - 有效 tick = BookLatMs ≤ Config.MaxBookLatMs（默认 300, 回测 MAX_LAT）且
//     UP(=yes)/DOWN(=no) 双侧 bid/ask 报价齐全（整簿快照门控，镜像回测 :79；
//     实测缺失为整行全空，只挡无快照行）
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

	stats WindowStats // 本窗 tick 健康度（纯计数, 不参与判定; 每窗重置）
}

// NewEngine 创建一个处于 Watching 态的空引擎（窗口上下文由 BeginWindow 注入）。
func NewEngine(cfg Config) *Engine {
	return &Engine{cfg: cfg, state: stateWatching}
}

// BeginWindow 重置引擎并注入窗口上下文（窗口起点瞬间采样）：
// anchor 为开盘 Chainlink TWAP-60 值、histBps 为该时刻可用的 σ（bps），≤0 表示不可用
// （初值不够准时可由 UpgradeAnchor 在窗口内升级/回填）。
func (e *Engine) BeginWindow(anchor, histBps float64) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.state = stateWatching
	e.anchor = anchor
	e.histBps = histBps
	e.upAsks = e.upAsks[:0]
	e.downAsks = e.downAsks[:0]
	e.stats = WindowStats{} // 本窗健康度重新计数（LostTriggers 底层数组一并丢弃）
}

// UpgradeAnchor 把本窗锚升级到更可信来源（推送缓存重选 / 官方开盘价），返回是否被采纳。
//
// 相对「窗口级常量锚」的唯一让步: 锚在**产出观测前**可变（2026-09-18 锚升级通道，
// 见 docs/dog020_anchor_upgrade_2026-09-18.md）。判定入口即冻结——state != Watching
// 覆盖两种 Done（产出观测、rem==0 终 tick），已落盘的观测行不可追溯改写，其后到达
// 的新值一律丢弃并返回 false。
//
// anchor 与 histBps 必须同源（histBps = 调用方用同一个 anchor 算出的 σ）: dist_s 的
// 分子是锚、分母是 σ，只换其一会让本窗判定基准自相矛盾。
//
// 升级**不追溯**升级前的触底（那些 tick 判定用的还是旧锚；与「无效 tick 不触发」同构）。
// 锚缺失窗口（anchor ≤ 0）首次升级后本窗恢复判定能力——语义同原 SetAnchor: 升级前
// 占槽的 tick 仍在 ring 中，故 crash 腿看到的仍是完整真实盘口历史。
func (e *Engine) UpgradeAnchor(anchor, histBps float64) bool {
	if !(anchor > 0) {
		return false
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.state != stateWatching {
		return false
	}
	e.anchor = anchor
	e.histBps = histBps
	e.stats.AnchorMissing = false
	return true
}

// WindowAnchor 返回本窗最终生效的锚与 σ（bps）：正常窗口 = BeginWindow 注入值，
// 锚未就绪窗口 = 恢复回填值（始终未回填则 0, 0）。
// 窗口结束时读取（σ 追加判定 + 健康度落盘），恢复 goroutine 可能已并发回填——
// 调用方须先确保恢复通道已退出（cmd/flip 用 cancel + join 保证）。
func (e *Engine) WindowAnchor() (anchor, histBps float64) {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.anchor, e.histBps
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

	// 锚未就绪（边界采样缺失/陈旧，恢复通道可能稍后回填）：本 tick 照常占槽与
	// 计数（无效 tick 仍压 0 占槽），只是不做触发判定——dist_s 无锚无法计算，
	// 强判即失真。占槽保证锚回填后 crash 腿 m_45 看到的是完整真实盘口历史
	// （回测里锚恒可用，此处对齐）；恢复前的触底不追溯，只留痕
	// （lost_triggers.anchor_pending）。
	anchorPending := e.anchor <= 0
	if anchorPending {
		e.stats.AnchorMissing = true
	}

	// 以下计数器只累加，不改变任何分支走向（btreplay 逐位对账红线）。
	e.stats.Ticks++
	// tick 无效（延迟过高）：占槽不检——与回测一致，其后触发仍按索引计数。
	if t.BookLatMs > e.cfg.MaxBookLatMs {
		e.stats.BookStale++
		e.lostTrigger(t, lostReason(anchorPending, LostReasonStaleBook))
		e.pushSlots(t)
		return nil
	}
	// 整簿快照门控：UP/DOWN 四字段报价齐全才算有效 tick（镜像回测 :79）。
	// 实测报价缺失是整行全空（两侧同秒为 0），本检查只挡无快照行；
	// 任一侧报价不全 → 不参与触发（ask=0 本就无法触底）。
	if !(t.UpBid > 0 && t.UpAsk > 0 && t.DownBid > 0 && t.DownAsk > 0) {
		e.stats.BookMissing++
		e.lostTrigger(t, lostReason(anchorPending, LostReasonBookMissing))
		e.pushSlots(t)
		return nil
	}
	e.stats.TicksValid++
	if anchorPending {
		// 盘口有效但判不了：占槽（供恢复后 crash 腿回看）+ 触底留痕
		e.lostTrigger(t, LostReasonAnchorPending)
		e.pushSlots(t)
		return nil
	}

	var obs *Observation
	switch {
	case t.UpAsk <= e.cfg.TriggerAskMax && t.DownAsk <= e.cfg.TriggerAskMax:
		// 交叉态：两侧 ask 同时 ≤0.2（14 天 0 次——互补套利结构近不可能，
		// 出现即有一侧报价陈旧）。狗侧取 sgn·(spot−anchor)<0 的一侧——浅洞带
		// 可能成立侧（另一侧 dist 必带外）；不可判（spot≤0 缺失或 spot=锚）
		// 退回 yes，任一侧同归 dist_out，无差异。
		side := SideYes
		if t.BinPrice > e.anchor {
			side = SideNo
		}
		obs = e.decide(t, side)
	case t.UpAsk <= e.cfg.TriggerAskMax:
		// UP ask 触底 → 狗侧 yes（ask>0 由整簿门控保证）
		obs = e.decide(t, SideYes)
	case t.DownAsk <= e.cfg.TriggerAskMax:
		// DOWN ask 触底 → 狗侧 no（ask>0 由整簿门控保证，无需 0 守卫）
		obs = e.decide(t, SideNo)
	}
	if obs != nil {
		e.state = stateDone
		return obs
	}
	e.pushSlots(t)
	return nil
}

// lostTrigger 记录一个「本会触发但被数据质量闸挡掉」的 tick（无触发则不记）。
// 只累加 e.stats.LostTriggers，不改变本 tick 的任何处理路径。
func (e *Engine) lostTrigger(t Tick, reason string) {
	side := e.touchSide(t)
	if side == "" {
		return
	}
	ask := t.UpAsk
	if side == SideNo {
		ask = t.DownAsk
	}
	e.stats.LostTriggers = append(e.stats.LostTriggers, LostTrigger{
		Ts: t.Ts, Side: side, Rem: t.Rem, Ask: ask,
		BookLatMs: t.BookLatMs, Reason: reason,
	})
}

// touchSide 返回该 tick 的触底狗侧（无触底返回 ""）。
//
// 判据与 ProcessTick 内联的触发 switch **逐条对齐**（交叉态取 sgn·(spot−anchor)<0
// 侧、不可判退回 yes），只多一条 `> 0` 守卫——实际触发路径由整簿门控保证四字段
// 为正，而本函数用于无效 tick（ask 可能为 0，正是 book_missing 的来源），故必须
// 自行守卫。两处必须同步修改，TestTouchSideMirrorsTrigger 钉住等价性。
func (e *Engine) touchSide(t Tick) string {
	upTouch := t.UpAsk > 0 && t.UpAsk <= e.cfg.TriggerAskMax
	downTouch := t.DownAsk > 0 && t.DownAsk <= e.cfg.TriggerAskMax
	switch {
	case upTouch && downTouch:
		if t.BinPrice > e.anchor {
			return SideNo
		}
		return SideYes
	case upTouch:
		return SideYes
	case downTouch:
		return SideNo
	}
	return ""
}

// WindowStats 返回本窗健康度统计的拷贝（含 LostTriggers 深拷贝；Dashboard / 落盘用）。
func (e *Engine) WindowStats() WindowStats {
	e.mu.Lock()
	defer e.mu.Unlock()
	st := e.stats
	st.LostTriggers = append([]LostTrigger(nil), e.stats.LostTriggers...)
	return st
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
		BookLatMs: t.BookLatMs, TwapAgeMs: t.TwapAgeMs, SpotAgeMs: t.SpotAgeMs,
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
	case !(o.DistS > loSide(cfg, side) && o.DistS < cfg.DistHi):
		// 浅洞腿：dist_s ∈ (lo(side), DistHi) 开区间——侧别带
		//（no=顶部恐慌族 spot 领先 TWAP → −1.0 深一档; yes=破位中继 → −0.6;
		// 见 DefaultConfig 注释）
		o.RejectReason = RejectDistOut
	default:
		o.OK = true
		o.Shares = cfg.Stake / fill
	}
	return o
}

// pushSlots 将本 tick 追加为 up/down 两个并行的索引槽位（ask 保留或压 0），
// ring 超深则丢弃最老槽位。无效 tick（延迟 > MaxBookLatMs）压 0 占槽。
func (e *Engine) pushSlots(t Tick) {
	up, down := 0.0, 0.0
	if t.BookLatMs <= e.cfg.MaxBookLatMs {
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

// loSide 返回该狗侧的浅洞带下界（侧别带: yes 窄 −0.6 / no 宽 −1.0）。
func loSide(cfg Config, side string) float64 {
	if side == SideNo {
		return cfg.DistLoNo
	}
	return cfg.DistLoYes
}
