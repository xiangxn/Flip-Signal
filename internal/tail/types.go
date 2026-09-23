// Package tail 实现「扫尾盘」策略引擎（规则 ⑤，docs/tail_sweep_2026-09-22.md）：
// 在 5 分钟窗口的最后 60s 买入**热门侧**（ask 高的一侧），当该侧 ask ≥ 0.80 且
// 现货相对锚的位移满足美元腿或 σ 腿时下注。
//
//	dev = sgn·(spot − anchor)          位移（USD，正 = 朝押注方向；sgn: 押 yes +1 / no −1）
//	sd  = hist_bps·anchor/1e4          该窗 1σ 折美元（= 前 ≤18 窗 |tw_close−tw_open| 均值）
//
//	买 ⟺ dev ≥ 63 美元   或   ( 40 美元 ≤ sd ≤ dev )
//
// 与 dog@0.2（internal/flip）的关系: 两者独立、方向相反（那儿买被砸到 0.2 的冷门侧），
// 但共用同一批原语——Tick / Executor / HistState / CanTrade 与 σ 的窗口定义都从
// internal/flip 取（本包只加策略专属的判定与记录）。
//
// ⚠️ 2026-09-22 文档 §4.7: 两族若同时上实盘, **仓位与日亏熔断必须合并计算**——本包
// 没做（各进程一条独立 24U 线, 等效 48U），见 docs/tail_engine_mapping_2026-09-23.md。
package tail

import "github.com/necklace/flip-signal/internal/flip"

// ── 行类型 ──

// 行类型标识（Observation.Kind 取值 = tail_*.jsonl 的 kind 字段）。
// 一个窗口至多三行: 先 frame（rem≤FrameRem 首帧, 只记录）、再 snap（rem≤RemStart
// 首帧, 判定 + 执行）、最后 scan（**仅当 snap 未达标时**才可能有, 只记录）。
// 见 Config.FrameRem 的「两帧折中」说明与 KindScan 的类型注释。
const (
	// KindFrame 原始快照帧: 只记录 §5.3 要求的字段, 不下单、不带规则标记
	//（离线据此复算任意 T 的规则）。
	KindFrame = "frame"
	// KindSnap 决策快照帧（策略本体 T=60）: 带五格规则标记 + ok/reject + 执行字段。
	KindSnap = "snap"
	// KindScan 监听口径对账行（2026-09-23 追加）: 决策快照**之后**第一个 ⑤ 达标的
	// tick——回答「若把一次性快照改成『rem≤60 起持续监听、达标即下单』, 会在哪里成交」。
	//
	// ⚠️ 三个不可混淆的点:
	//   - **绝不下单**: 本行只落盘（exec_state.HandleScan 不碰 Ex、不过风控闸）, 它是一条
	//     「另一个口径本会成交」的反事实样本, 不是仓位。风控闸之所以**不**施加于它:
	//     对照物是回测的监听口径 B（14 天回放里没有熔断器）, 施加熔断会让两边不可比。
	//   - **会结算**: 落盘即进待结算队列, 由 settle.Resolver 三层回退拿官方 outcome
	//     （决策 #19）——否则没有任何地方能知道那一窗最后谁赢, 反事实就永远算不成
	//     P&L。故它满足 isSettlable。所有 P&L
	//     聚合（DailyPnl 日亏熔断 / MaxDrawdown / Judge 五格 / 逐日表）都**必须**按
	//     Kind 过滤, 见 recorder.go 的 isSettlable 与 DailyPnl 注。
	//   - **与 snap 互斥**: 只在 snap 行未达标时才产出（snap 达标则两口径同 tick,
	//     本行冗余）。故任一窗口**至多一条**可结算行（要么 snap、要么 scan）,
	//     Recorder.pending 以 conditionID 为键不会打架。
	//
	// 判定语义 = python 的「监听」口径（16_tail_t150_scan.py 的 tail_ticks）:
	// **逐 tick 独立**求 ⑤, 某个 tick 缺 spot/twap 只是跳过它（不像 snap 那样整窗丢弃）。
	// 缺 spot 时 dev=0 天然过不了规则; 缺 twap 不影响 ⑤（规则不用它）。
	KindScan = "scan"
)

// isKnownKind 判断 kind 是否为本族认识的行类型（载入期的 schema 守卫用）。
func isKnownKind(kind string) bool {
	return kind == KindFrame || kind == KindSnap || kind == KindScan
}

// 拒绝原因常量（Record.RejectReason 取值; 空 + ok=false 不应出现）。
//
// 顺序即判定顺序（decide 内 switch 固定）: missing_spot → missing_twap → no_hist
// → price_low → leg_out。前两条镜像 python 13_tail_sweep.py:91-93 的整窗丢弃语义
// （**不是**「往后找下一个有 spot 的 tick」——搜索在首个 rem≤T 的有效 tick 就停了）。
const (
	// RejectMissingSpot 快照 tick 无有效 Binance spot（≤0 = 缺失/陈旧 >2s）。
	RejectMissingSpot = "missing_spot"
	// RejectMissingTwap 快照 tick 无 TWAP 流值（≤0）。
	//
	// ⚠️ ⑤ 规则本身**不用** twap: 这条腿纯粹为了与回测宇宙一致——python 的
	// snapshots() 对每个 T 都要求 spot 与 twap 同时在场, n=1536 是在该约束下算出来的。
	// 去掉它就等于悄悄放宽了样本宇宙（拿不到对账基准）。纸面期若发现它频繁命中
	//（正常应为 0）, 说明口径该重议, 而不是静默放过。
	RejectMissingTwap = "missing_twap"
	// RejectNoHist σ 不可用（hist_bps ≤ 0, 前 <3 个已完窗）。纯函数防线: 现网由
	// cmd/tail 的前置闸整窗跳过（skip=no_sigma）, 到不了这里。
	RejectNoHist = "no_hist"
	// RejectPriceLow 价格腿不过: 热门侧 ask < PriceMin（含整窗无热门侧的情形）。
	RejectPriceLow = "price_low"
	// RejectLegOut 价格腿过了但两腿都没过: dev < 63 且不满足 (sd ≥ 40 ∧ dev ≥ sd)。
	RejectLegOut = "leg_out"
)

// 风控闸原因常量（Record.GateReason 取值; 空 = 未被闸）。口径与 internal/flip 一致
// （docs/dog020_risk_latency_plan_2026-09-16.md §3）: 两模式同判据, live 拦 POST,
// paper 只标记且行照记照结算（方案 A）。
const (
	// GateDailyLoss 日亏熔断: 当日（UTC）已结算 P&L ≤ risk.max_daily_loss, 当日锁存。
	GateDailyLoss = "daily_loss"
	// GateFirstWindow live 重启后首窗禁单（防重启残留窗双单; paper 无仓位不设）。
	GateFirstWindow = "first_window"
)

// ── 状态机 ──

type engineState int

const (
	stateWatching engineState = iota // 等待本轮的两帧（frame ≤ FrameRem / snap ≤ RemStart）
	// stateScanning 决策快照已产出, 监听段: 只可能再产出一条 **只记录** 的
	// KindScan 对账行（或窗口结束）。判定与下单已在 snap 那一步闭环, 本段不可再
	// 决策、更不可下单——它存在的唯一理由是回答「监听口径本会在哪里成交」。
	stateScanning
	stateDone // 窗口已结束（rem==0 终 tick）或监听对账行已产出/不可能再产出
)

// String 返回状态机可读名（日志用）。
func (s engineState) String() string {
	switch s {
	case stateWatching:
		return "Watching"
	case stateScanning:
		return "Scanning"
	case stateDone:
		return "Done"
	}
	return "Unknown"
}

// ── 输出 ──

// Rules 是同一批快照上四条**原始腿**的判定结果（不与价格腿合并——合并就没法从
// 行上看出「价格腿没过但 dev 过了」）。五格由 Rule1…Rule5 派生。
//
// ⚠️ 价格腿 Price 是**全部五格的前置**（python 13_tail_sweep.py:116 先算 hot、
// 15_tail_sweep_union_sigma.py:60 先过滤 hot 再判 dev/σ），故每个 RuleN 都显式
// AND 上它——这条作用域是本品最容易写错的地方（漏了就会去交易便宜 ask 的窗口,
// 而那正是 dog@0.2 族的地盘）。
//
// 字段命名避开 python 里 `sig` 的一词两义（13 里是 bool 的「hot∧confirm」,
// 15 里是 float 的 σ 倍数）: 这里一律用 Sigma/SigmaUSD40 指 σ 腿。
type Rules struct {
	Price      bool `json:"price"`       // hot_ask ≥ PriceMin
	Dev63      bool `json:"dev63"`       // dev ≥ DevMinUSD（原始, 未与 Price 合并）
	Sigma      bool `json:"sigma"`       // σ 可用 ∧ dev ≥ sd（即 sig ≥ 1.0）
	SigmaUSD40 bool `json:"sigma_usd40"` // σ 可用 ∧ sd ≥ SigmaMinUSD ∧ dev ≥ sd（σ 腿放行）
}

// Rule1 ① = 只要热门侧 ask ≥ 0.80 就买（无过滤基线）。
func (r Rules) Rule1() bool { return r.Price }

// Rule2 ② = 价格腿 ∧ dev ≥ 63 美元。
func (r Rules) Rule2() bool { return r.Price && r.Dev63 }

// Rule3 ③ = 价格腿 ∧ dev ≥ sd（该窗 1σ 折美元）。
func (r Rules) Rule3() bool { return r.Price && r.Sigma }

// Rule4 ④ = 价格腿 ∧ (dev ≥ 63 ∨ dev ≥ sd)。
func (r Rules) Rule4() bool { return r.Price && (r.Dev63 || r.Sigma) }

// Rule5 ⑤ = 价格腿 ∧ (dev ≥ 63 ∨ (sd ≥ 40 美元 ∧ dev ≥ sd)) ← **引擎只下单这一格**
// （docs/tail_sweep_2026-09-22.md §1.3）。
func (r Rules) Rule5() bool { return r.Price && (r.Dev63 || r.SigmaUSD40) }

// Observation 是一次快照观测（帧行/快照行/监听对账行共用本类型, 由 Kind 区分）。
//
// 字段即 docs/tail_sweep_2026-09-22.md §5.3 要求的记录集（四档报价 + spot + twap
// + anchor + hist_bps + rem + book_latency_ms + spot_age_ms）+ 判定派生量。
// 帧行只填原始段（Rules/OK/RejectReason/Shares 留空）——离线可据此复算任意 T 的规则;
// 监听对账行（KindScan）填满判定段但**恒 OK**（引擎只在达标时才产出它）。
type Observation struct {
	Kind   string `json:"kind"`    // KindFrame | KindSnap | KindScan
	FrameT int    `json:"frame_t"` // 本行闸值（frame = Config.FrameRem, snap/scan = Config.RemStart）
	Ts     int64  `json:"ts"`      // 快照 tick 采样时刻（unix 毫秒）
	Rem    int    `json:"rem"`     // 快照 tick 的窗口剩余秒（≤ frame_t）

	// ── 原始快照（权威口径: 所有派生量都应由这些字段现算）──
	YesBid float64 `json:"yes_bid"`
	YesAsk float64 `json:"yes_ask"`
	NoBid  float64 `json:"no_bid"`
	NoAsk  float64 `json:"no_ask"`
	Spot   float64 `json:"spot"`   // Binance BTCUSDT 最新价（0 = 缺失/陈旧 >2s）
	Twap   float64 `json:"twap"`   // Chainlink TWAP-60 流值（0 = 缺失; ⑤ 不用它, 见 RejectMissingTwap）
	Anchor float64 `json:"anchor"` // 本窗锚 = 边界那一秒的 TWAP 推送（恒 >0, 无锚整窗不产出）
	// HistBps 本窗生效的 σ（bps; 前 ≤18 已完窗 |close−anchor| 均值 ÷ anchor × 1e4）。
	HistBps float64 `json:"hist_bps"`

	// ── 判定派生（便捷字段; 权威值由 Spot/Anchor/HistBps 现算）──
	Side   string  `json:"side"`    // 热门侧: yes | no（ask 高的一侧, 平局取 yes）
	HotAsk float64 `json:"hot_ask"` // 热门侧 ask = 成交价口径（≥ PriceMin 才可能下单）
	// Dev = sgn·(Spot − Anchor)，美元（正 = 朝押注方向）。omitempty 的 0 表示
	// 「spot/anchor 缺失未计算」而非「恰好为 0」——以 RejectReason 区分。
	Dev float64 `json:"dev,omitempty"`
	// Sd = HistBps·Anchor/1e4，美元（该窗 1σ 的美元值 = 前 ≤18 窗平均绝对振幅）。
	// 同上, omitempty 的 0 表示 σ 不可用。
	Sd float64 `json:"sd,omitempty"`

	BookLatMs int64 `json:"book_latency_ms"` // 盘口传输延迟（> MaxBookLatMs 的 tick 不是有效 tick, 到不了这里）
	SpotAgeMs int64 `json:"spot_age_ms"`     // spot 距本地接收毫秒（-1 = 尚无推送; 诊断）
	TwapAgeMs int64 `json:"twap_age_ms"`     // TWAP 距上次推送毫秒（诊断）

	// ── 决策（snap 与 scan 行; 帧行全空）──
	Rules        Rules   `json:"rules"`                   // 四条原始腿（①②③④⑤ 由 RuleN() 派生）
	OK           bool    `json:"ok"`                      // 是否构成信号（= Rules.Rule5() 且输入齐备）
	RejectReason string  `json:"reject_reason,omitempty"` // 未构成信号的原因（scan 行恒空——它只在 OK 时产出）
	Shares       float64 `json:"shares,omitempty"`        // 目标股数 = Stake/HotAsk（仅 ok=true; scan 行为**假想**股数）
}

// Record 是一条快照观测的完整落盘记录（纸面/实盘同 schema）。
// 字段 = Observation 全部 + 记录元信息（结算后回填 won/pnl/resolved_at;
// live 行回填 exec_status/order_id/avg_fill_price/cost/exec_note——paper 行无这些字段）。
// 与 flip.Record 同形制但独立定义: 观测段（§5.3 字段表）与 flip 的触底段完全不同,
// 强行共用一个结构体只会让两边字段都变得可选。
type Record struct {
	Observation
	EventType   string  `json:"event_type"` // 恒为 "tail"
	Date        string  `json:"date"`       // UTC 日（按日切分文件名）
	ConditionID string  `json:"condition_id"`
	Slug        string  `json:"slug"`
	EventStart  int64   `json:"event_start"`           // 窗口起点（unix 秒）
	Stake       float64 `json:"stake,omitempty"`       // 每笔投入 USDC（结算 P&L 基准）
	Won         *bool   `json:"won,omitempty"`         // 结算后填充（所押侧是否赢）
	PnL         float64 `json:"pnl,omitempty"`         // 结算后填充（USDC）
	ResolvedAt  string  `json:"resolved_at,omitempty"` // 结算时间（RFC3339）

	// SettleSrc 结算来源（internal/settle 的三层回退, 2026-09-24 起）: push = 边界
	// 推送自算 / official = 官方接口兜底 / gamma = UMA 轮询; 空 = 未记（旧行）。
	// 与 flip.Record 同字段同语义（两族共用同一套结算编排）。
	SettleSrc string `json:"settle_src,omitempty"`

	// GateReason 风控闸原因（GateDailyLoss/GateFirstWindow; 空 = 未被闸）。
	// 两模式都写（与 flip 方案 A 同口径）: paper 被闸行 schema 与正常信号一致
	//（IsFilled 仍 true、照常结算回填 won/pnl）, 分析脚本须显式过滤。
	GateReason string `json:"gate_reason,omitempty"`

	// ── live 执行回填（paper 行恒空）──
	ExecStatus string  `json:"exec_status,omitempty"`    // 空 = paper; 取值见 flip.ExecStatus*
	OrderID    string  `json:"order_id,omitempty"`       // CLOB 订单 id
	FillPrice  float64 `json:"avg_fill_price,omitempty"` // 实际成交均价（≤ 观测 hot_ask; 吃单可价格改善）
	Cost       float64 `json:"cost,omitempty"`           // 实际花费 USDC（paper 无 → Resolve 用 Stake 兜底）
	ExecNote   string  `json:"exec_note,omitempty"`      // rejected/unknown 原因
}

// IsFilled 判断记录是否实际成交（须注册结算轮询）: paper 行（ExecStatus 空 = 恒模拟
// 全额成交）与 live filled/partial 为真; unfilled/rejected/submitting/resting 为假
// （resting = GTC 挂单在簿成交量未定, 注册结算须等 FillTracker 回填）。与 flip 同判据。
func (r *Record) IsFilled() bool {
	switch r.ExecStatus {
	case "", flip.ExecStatusFilled, flip.ExecStatusPartial:
		return true
	}
	return false
}

// ── 窗口健康度 ──

// WindowStats 是本窗 tick 健康度计数（每窗无条件落盘一行, 见 Recorder.LogWindowStats）。
//
// 只计数、不参与任何判定（对账红线: 计数器禁止触碰 ProcessTick 的分支走向）。
// 恒等式: Ticks == TicksValid + BookStale + BookMissing。
type WindowStats struct {
	// Ticks 进入有效性分类的 tick 数（不含 rem==0 终 tick、不含 Done 之后）。
	Ticks int `json:"ticks"`
	// TicksValid 有效 tick（过延迟闸 + 整簿四档门控）——快照只在有效 tick 上取。
	TicksValid int `json:"ticks_valid"`
	// BookStale 延迟超阈而无效的 tick。
	BookStale int `json:"book_stale"`
	// BookMissing 整簿四档报价不全而无效的 tick。
	BookMissing int `json:"book_missing"`
	// Frames 本窗实际产出的行数（frame / snap 各计一; 0 = 本窗无锚或未跑到尾盘闸）。
	Frames int `json:"frames"`
}
