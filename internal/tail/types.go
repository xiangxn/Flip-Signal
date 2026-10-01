// Package tail 实现「扫尾盘」策略引擎（规则 ⑤，docs/tail_sweep_2026-09-22.md）：
// 在 5 分钟窗口的尾盘买入**热门侧**（有效价高的一侧），当该侧价格 ≥ 0.80 且现货相对
// 锚的位移满足美元腿或 σ 腿时下注。
//
//	dev = sgn·(spot − anchor)          位移（USD，正 = 朝押注方向；sgn: 押 yes +1 / no −1）
//	sd  = hist_bps·anchor/1e4          该窗 1σ 折美元（= 前 ≤18 窗 |tw_close−tw_open| 均值）
//
//	⑤ = hot_ask ≥ 0.80 且 ( dev ≥ 63 美元   或   ( sd ≥ 40 美元  且  dev ≥ sd ) )
//	② = hot_ask ≥ 0.80 且   dev ≥ 63 美元
//
// ⚠️ 2026-09-26 起 **T=150 段的价格腿是严格大于**（`hot_ask > 0.80`）, 上面这两行
// 的 ≥ 只对 T=60 段与监听段成立——见 decide.PriceLeg（本包两条段相关腿之一）与
// docs/tail_integrated_2026-09-24.md §6。
//
// ⚠️ 2026-09-29 起 **T=150 段另有一道入场闸**（决策 #29）: ⑤ 达标之外还要
// `walk = sgn·(twap − anchor) ≥ cfg.WalkMinUSD`（默认 43 美元; 已写进结算线的位移,
// 见 decide.WalkLeg;
// `dev = basis + walk`, basis 是还没写进去的缺口 = 领先量）。本段不达标 ⇒ 不出信号、
// **链继续**; 段 2/3 一字不动。
//
// ⚠️ 2026-10-01 起 **T=60 段与监听段另有一道价格地板**（决策 #32; 同日 #33 下延到
// T=60 段）: 这两段除各自规则外还要热门侧有效价 `> cfg.FloorMinPrice`
//（默认 0.83, 见 decide.FloorLeg）。被拦的 tick 落一行**影子行**
//（reject_reason = floor_low）且**链继续**（T=60 被拦 ⇒ 走到监听段; 监听段被拦 ⇒
// 同段等下一个更贵的 tick）; T=150 段一字不动。
//
// 以上三道是**全部**的段相关腿（价格腿 PriceLeg 的比较符 + 入场闸 walk + 价格地板）,
// 其余规则三段完全一致。
//
// 2026-09-24 起每窗走**三段递进判定链**（用户决定, docs/tail_integrated_2026-09-24.md §1）:
// rem≤150 判 ⑤ → 不达标则 rem≤60 再判 ⑤ → 再不达标则此后每秒判 ②（达标即下单）。
// 任一段出信号即整窗只下一单。
//
// 与 dog@0.2（internal/flip）的关系: 两者独立、方向相反（那儿买被砸到 0.2 的冷门侧），
// 但共用同一批原语——Tick / Executor / HistState / CanTrade 与 σ 的窗口定义都从
// internal/flip 取（本包只加策略专属的判定与记录）。
//
// ⚠️ 2026-09-22 文档 §4.7: 两族若同时上实盘, **仓位与日亏熔断必须合并计算**——本包
// 没做（各进程一条独立 24U 线, 等效 48U），见 docs/tail_engine_mapping_2026-09-23.md。
package tail

import "github.com/necklace/flip-signal/internal/flip"

// ── 行类型与判定段 ──

// KindSnap 是本族**唯一**产出中的行类型（观测/判定/信号行共用, tail_*.jsonl 的
// kind 字段）。三段链的每一条行都是它, 由 Stage 区分属于哪一段。
//
// 其余两个 kind 是 **legacy-only**（2026-09-24 之前的两帧折中留下的, 引擎不再产出,
// 但 isKnownKind 继续认——data/v4-tail/ 里那些行必须照旧载入/结算）:
const (
	// KindSnap 决策/信号行（策略本体）: 带四腿规则标记 + ok/reject + 执行字段。
	KindSnap = "snap"
	// KindFrame legacy（旧）: rem ≤ frame_rem 的「原始帧」, 只记录不下单。
	KindFrame = "frame"
	// KindScan legacy（旧）: 监听口径对账行（只记录、绝不下单的反事实样本）。
	// ⚠️ 它从不代表仓位（HasPosition 恒 false）, 且不再出现在任何页面上。
	KindScan = "scan"
)

// 判定段（Observation.Stage 取值）——三段递进链的哪一段产出了这一行。
//
// ⚠️ 空串 = **legacy 行**（2026-09-24 之前落盘的两帧折中行, 没有这个键）。
// 落盘与 dashboard 分桶都按「空 = 旧行」处理（旧行不属任何新段, 只进总数）。
const (
	// StageT150 第一段: rem ≤ t150_rem 的首个可判定 tick 上判 ⑤。
	StageT150 = "t150"
	// StageT60 第二段: 第一段没出信号, rem ≤ t60_rem 的首个可判定 tick 上再判 ⑤。
	// 本段除了成功（信号行）与不达标（判定行）两种落行外, 还有第三种: ⑤ 达标但被
	// **价格地板**拦下的 floor_low **影子行**（决策 #33, 每窗至多一条, 与监听段共用
	// 闩锁）——那一 tick 在不放地板的世界里本会成交, 链继续到监听段。
	StageT60 = "t60"
	// StageListen 第三段（监听）: 前两段都没信号后, 此后**每秒**判 ②（不含 σ 腿）。
	// 本段落行的只有两种 tick: ② 达标的（信号行）与 ② 达标但被**价格地板**拦下的
	//（floor_low 影子行, 每窗至多一条, 见 RejectFloorLow）; 其余被拒的 tick 不落盘
	//（每窗约 60 个 tick, 全落会淹没信号表）。
	StageListen = "listen"
)

// isKnownKind 判断 kind 是否为本族认识的行类型（载入期的 schema 守卫用）。
func isKnownKind(kind string) bool {
	return kind == KindSnap || kind == KindFrame || kind == KindScan
}

// 拒绝原因常量（Record.RejectReason 取值; 空 + ok=false 不应出现）。
//
// 顺序即判定顺序（decide 内 switch 固定）:
// missing_spot → no_hist → price_low → **leg_out → walk_low**。
// ⚠️ floor_low（价格地板）**不在**这条链里——它只出现在影子行（T=60/监听段,
// 见 RejectFloorLow）, 判定行走不到它。
// 前两条是**纯函数防线**（现网到不了）: 缺 spot 的 tick 根本不落行（引擎等下一个
// 可判定的 tick）, σ 未就绪由 cmd/tail 整窗跳过（skip=no_sigma）。
//
// ⚠️ 2026-09-24 起去掉 `missing_twap` 腿（⑤ 与 ② 都不用 twap）——历史 14 天里它
// **零命中**（判定 tick 上 spot/twap 从不缺失, 见 §5）。旧行里仍可能出现该值。
const (
	// RejectMissingSpot 判定 tick 无有效 Binance spot（≤0 = 缺失/陈旧 >2s）。
	RejectMissingSpot = "missing_spot"
	// RejectNoHist σ 不可用（hist_bps ≤ 0, 前 <3 个已完窗）。纯函数防线: 现网由
	// cmd/tail 的前置闸整窗跳过（skip=no_sigma）, 到不了这里。
	RejectNoHist = "no_hist"
	// RejectPriceLow 价格腿不过（含整窗无任何报价的情形）。判据随段而变: T=150 段是
	// `有效价 ≤ PriceMin`（严格大于的反面, 0.80 那一格落在这一侧）, 其余段是 `< PriceMin`。
	RejectPriceLow = "price_low"
	// RejectLegOut 价格腿过了但位移腿/σ 腿都没过: dev < 63 且不满足 (sd ≥ 40 ∧ dev ≥ sd)。
	RejectLegOut = "leg_out"
	// RejectWalkLow **T=150 段入场闸**（2026-09-29 决策 #29）: ⑤ 达标, 但
	// `walk = sgn·(twap − anchor) < cfg.WalkMinUSD`（默认 43, 见 decide.WalkLeg）。
	// 本段不出信号, **链继续**走到 T=60/监听（改道, 不是整窗丢弃）。
	//
	// ⚠️ 判据排在 leg_out **之后** ⇒ 它只表示「**闸是唯一的拦路者**」: 去掉闸这一行
	// 就会是信号（14 天 = 250 行, 即离线反事实里被闸掉的 250 个 T150 信号）;
	// 反之 dev/σ 腿本来就不达标的行一律读 leg_out——**离线统计别按「walk < 43 的行数」
	// 数它**, 那会把两种不同的行混在一起。
	RejectWalkLow = "walk_low"
	// RejectFloorLow **价格地板**（2026-10-01 决策 #32 监听段; 同日 #33 下延到 T=60 段）:
	// 该段规则达标（T=60 ⇒ ⑤ 达标; 监听 ⇒ ② 达标）, 但热门侧有效价
	// `≤ cfg.FloorMinPrice`（默认 0.83, 见 decide.FloorLeg）。
	// 本 tick **本会是一笔信号**, 被廉价角拦下——落一行**影子行**（ok=false）、
	// 链**继续**（T=60 被拦 ⇒ 走到监听段; 监听段被拦 ⇒ 同段等下一个更贵的 tick）。
	//
	// ⚠️ 每窗至多一条（两段共用同一个闩锁）: 影子行的用途是「**不放地板会成交的
	// 那一笔**长什么样」的离线反事实——无地板时链在首个达标的段就结束, 之后的廉价
	// tick 根本不会成为信号, 落它们只会把反事实读脏（并且监听段每窗 ~60 个 tick
	// 会淹没决策表）。
	//
	// ⚠️ 判据排在该段规则达标**之后** ⇒ 它只表示「**地板是唯一的拦路者**」: 去掉地板
	// 这一行就会是信号。别按「`fill ≤ 0.83` 的 tick 数」数它——规则未达标的廉价
	// tick 一条都不落。
	RejectFloorLow = "floor_low"
	// RejectMissingTwap **legacy-only**: 旧口径的快照宇宙要求 spot 与 twap 同时在场,
	// 现口径不再产出（⑤ 不用 twap）。保留常量是为了让读旧数据/审计脚本时一眼能认。
	RejectMissingTwap = "missing_twap"
)

// 风控闸原因常量（Record.GateReason 取值; 空 = 未被闸）。口径与 internal/flip 一致
// 的部分只有**判据本身**（GateDailyLoss + CanTrade 纯函数）——
// 后果不同: 2026-09-24 起 tail 两模式统一成「被闸 = 未成交」的 rejected 行
// （a.md 第 3/4 条把「被风控拦」明确归入未成交, 见 docs/tail_integrated_2026-09-24.md §3.3）,
// 而 flip 仍是方案 A（被闸行照常结算, 只标记不拦单）。
const (
	// GateDailyLoss 日亏熔断: 当日（UTC）已结算 P&L ≤ risk.max_daily_loss, 当日锁存。
	// 本族**唯一**还会新产出的闸原因。
	GateDailyLoss = "daily_loss"
	// GateFirstWindow **legacy 只读, 引擎不再产出**（2026-09-29 决策 #28 删掉 live 首窗禁单;
	// flip 侧仍有）。保留常量只为让读历史行/Dashboard 标签一眼能认——
	// data/tail-live 09-24~27 里还有 5 条这样的行（全部 ≥0.94 且全赢）。
	GateFirstWindow = "first_window"
)

// ── 状态机 ──

type engineState int

const (
	stateWatching engineState = iota // 等待第一段判定点（首个可判定 tick 且 rem ≤ t150_rem）
	// stateAwait60 第一段判定已做（被拒）: 等第二段判定点（首个可判定 tick 且 rem ≤ t60_rem）。
	stateAwait60
	// stateListening 监听段: 此后每秒判 ②, 达标即落信号行并转 Done。
	stateListening
	stateDone // 窗口已结束（rem==0 终 tick）或本窗已出信号（整窗只下一单）
)

// String 返回状态机可读名（日志用）。
func (s engineState) String() string {
	switch s {
	case stateWatching:
		return "Watching"
	case stateAwait60:
		return "Await60"
	case stateListening:
		return "Listening"
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
	// Price 价格腿, **比较符取决于本行的 stage**（T=150 严格大于, 其余 ≥, 见 PriceLeg）
	// ——它是落盘行的一部分, 必须与 reject_reason 自洽: `price = false` ⇔ price_low。
	Price      bool `json:"price"`
	Dev63      bool `json:"dev63"`       // dev ≥ DevMinUSD（原始, 未与 Price 合并）
	Sigma      bool `json:"sigma"`       // σ 可用 ∧ dev ≥ sd（即 sig ≥ 1.0）
	SigmaUSD40 bool `json:"sigma_usd40"` // σ 可用 ∧ sd ≥ SigmaMinUSD ∧ dev ≥ sd（σ 腿放行）
}

// Rule1 ① = 只要热门侧有效价 ≥ 0.80 就买（无过滤基线）。
func (r Rules) Rule1() bool { return r.Price }

// Rule2 ② = 价格腿 ∧ dev ≥ 63 美元（**监听段的规则**, a.md 第 1 条第三句）。
func (r Rules) Rule2() bool { return r.Price && r.Dev63 }

// Rule3 ③ = 价格腿 ∧ dev ≥ sd（该窗 1σ 折美元）。
func (r Rules) Rule3() bool { return r.Price && r.Sigma }

// Rule4 ④ = 价格腿 ∧ (dev ≥ 63 ∨ dev ≥ sd)。
func (r Rules) Rule4() bool { return r.Price && (r.Dev63 || r.Sigma) }

// Rule5 ⑤ = 价格腿 ∧ (dev ≥ 63 ∨ (sd ≥ 40 美元 ∧ dev ≥ sd)) ← **第一/二段下单的规则**
// （docs/tail_sweep_2026-09-22.md §1.3）。价格腿取 r.Price（**该段适用的那一个**,
// t150 行是严格大于）, 故同一个 Rule5 在三段上是同一套代码。
func (r Rules) Rule5() bool { return r.Price && (r.Dev63 || r.SigmaUSD40) }

// Observation 是一次快照观测（判定行与信号行共用本类型, 由 OK/Stage 区分）。
//
// 字段即 docs/tail_sweep_2026-09-22.md §5.3 要求的记录集（四档报价 + spot + twap
// + anchor + hist_bps + rem + book_latency_ms + spot_age_ms）+ 判定派生量。
// 判定行带四腿与 ok/reject（**成功与否都落盘**）; 监听段的信号行恒 ok。
type Observation struct {
	Kind   string `json:"kind"`            // 恒 KindSnap（KindFrame/KindScan 为 legacy-only）
	Stage  string `json:"stage,omitempty"` // StageT150 | StageT60 | StageListen; 空 = legacy 旧行
	FrameT int    `json:"frame_t"`         // 本段的时间腿（t150 = Config.T150Rem, t60/listen = Config.T60Rem）
	Ts     int64  `json:"ts"`              // 快照 tick 采样时刻（unix 毫秒）
	Rem    int    `json:"rem"`             // 快照 tick 的窗口剩余秒（≤ 本段的时间腿）

	// ── 原始快照（权威口径: 所有派生量都应由这些字段现算）──
	YesBid float64 `json:"yes_bid"`
	YesAsk float64 `json:"yes_ask"`
	NoBid  float64 `json:"no_bid"`
	NoAsk  float64 `json:"no_ask"`
	Spot   float64 `json:"spot"` // Binance BTCUSDT 最新价（0 = 缺失/陈旧 >2s）
	// Twap Chainlink TWAP-60 流值（0 = 缺失）。⚠️ T=150 段的入场闸由它算 walk
	// （decide.WalkLeg: walk = sgn·(Twap−Anchor) ≥ cfg.WalkMinUSD, 默认 43）, 其余段仅诊断。
	Twap   float64 `json:"twap"`
	Anchor float64 `json:"anchor"` // 本窗锚 = 边界那一秒的 TWAP 推送（恒 >0, 无锚整窗不产出）
	// HistBps 本窗生效的 σ（bps; 前 ≤18 已完窗 |close−anchor| 均值 ÷ anchor × 1e4）。
	HistBps float64 `json:"hist_bps"`

	// ── 判定派生（便捷字段; 权威值由 Spot/Anchor/HistBps 现算）──
	Side   string  `json:"side"`    // 热门侧: yes | no（有效价高的一侧, 平局取 yes）
	HotAsk float64 `json:"hot_ask"` // 热门侧**有效价**（ask 优先、bid 兜底）= 成交价口径
	// HotSrc 有效价来源: ask | bid（空 = legacy 行, 那时只有 ask 口径）。
	// `bid` 是可审计的红旗——那一侧 ask 被整侧撤空, live 挂单大概率不成交。
	HotSrc string `json:"hot_src,omitempty"`
	// Dev = sgn·(Spot − Anchor)，美元（正 = 朝押注方向）。omitempty 的 0 表示
	// 「spot 缺失未计算」而非「恰好为 0」——以 RejectReason 区分。
	Dev float64 `json:"dev,omitempty"`
	// Sd = HistBps·Anchor/1e4，美元（该窗 1σ 的美元值 = 前 ≤18 窗平均绝对振幅）。
	// 同上, omitempty 的 0 表示 σ 不可用。
	Sd float64 `json:"sd,omitempty"`

	BookLatMs int64 `json:"book_latency_ms"` // 盘口传输延迟（> MaxBookLatMs 的 tick 不是有效 tick, 到不了这里）
	SpotAgeMs int64 `json:"spot_age_ms"`     // spot 距本地接收毫秒（-1 = 尚无推送; 诊断）
	TwapAgeMs int64 `json:"twap_age_ms"`     // TWAP 距上次推送毫秒（诊断）

	// ── 决策 ──
	Rules        Rules   `json:"rules"`                   // 四条原始腿（①②③④⑤ 由 RuleN() 派生）
	OK           bool    `json:"ok"`                      // 是否构成信号（t150/t60 = Rule5 ∧ 输入齐备; listen = Rule2 ∧ 地板）
	RejectReason string  `json:"reject_reason,omitempty"` // 未构成信号的原因（影子行恒为 floor_low）
	Shares       float64 `json:"shares,omitempty"`        // 目标股数 = Stake/HotAsk（仅 ok=true）
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
	Won         *bool   `json:"won,omitempty"`         // 结算后填充（所押侧是否赢; **含未成交行**——它们照显结果）
	PnL         float64 `json:"pnl,omitempty"`         // 结算后填充（USDC; 未成交行恒 0, 见 recomputePnL）
	ResolvedAt  string  `json:"resolved_at,omitempty"` // 结算时间（RFC3339）

	// SettleSrc 结算来源（internal/settle 的三层回退, 2026-09-24 起）: push = 边界
	// 推送自算 / official = 官方接口兜底 / gamma = UMA 轮询; 空 = 未记（旧行）。
	// 与 flip.Record 同字段同语义（两族共用同一套结算编排）。
	SettleSrc string `json:"settle_src,omitempty"`

	// GateReason 风控闸原因（现口径恒 GateDailyLoss; GateFirstWindow 只存在于历史行;
	// 空 = 未被闸）。
	// ⚠️ 2026-09-24 起两模式统一: 被闸行 = ExecStatusRejected + 无仓位
	//（a.md 把「被风控拦」归入未成交）——判据仍是两模式同源, 差别只在 flip 侧仍走方案 A。
	GateReason string `json:"gate_reason,omitempty"`

	// ── live 执行回填（paper 行恒空）──
	ExecStatus string  `json:"exec_status,omitempty"`    // 空 = paper; 取值见 flip.ExecStatus*
	OrderID    string  `json:"order_id,omitempty"`       // CLOB 订单 id
	FillPrice  float64 `json:"avg_fill_price,omitempty"` // 实际成交均价（≤ 观测 hot_ask; 吃单可价格改善）
	Cost       float64 `json:"cost,omitempty"`           // 实际花费 USDC（paper 无 → 结算用 Stake 兜底）
	ExecNote   string  `json:"exec_note,omitempty"`      // rejected/unknown 原因
}

// IsFilled 判断记录是否实际成交: paper 行（ExecStatus 空 = 恒模拟全额成交）与 live
// filled/partial 为真; unfilled/rejected/submitting/resting 为假（resting = GTC 挂单
// 在簿、成交量未定）。与 flip 同判据。
//
// ⚠️ 统计口径请用 HasPosition（它还排除了 legacy 的 scan 对账行）。
func (r *Record) IsFilled() bool {
	switch r.ExecStatus {
	case "", flip.ExecStatusFilled, flip.ExecStatusPartial:
		return true
	}
	return false
}

// HasPosition 判断记录是否代表**真实仓位**——胜负分类与 P&L 聚合的唯一准入判据
// （dashboard 的 胜/负/待结算/未成交 四类与 DailyPnl/MaxDrawdown/LiveSummary 全用它）。
//
// = 实际成交 ∧ 不是 legacy 对账行。后者（kind=scan）从未下单, 行里的 shares 只是
// 假想股数——混进胜率会拿一条不存在的仓位去记账（旧代码靠「聚合时按 Kind 过滤」
// 兜住, 现在这条纪律收敛到一个方法里, 免得每个消费点各写一遍）。
func (r *Record) HasPosition() bool {
	return r.Kind != KindScan && r.IsFilled()
}

// ── 窗口健康度 ──

// WindowStats 是本窗 tick 健康度计数（每窗无条件落盘一行, 见 Recorder.LogWindowStats）。
//
// 只计数、不参与任何判定（对账红线: 计数器禁止触碰 ProcessTick 的分支走向）。
// 恒等式: Ticks == TicksValid + BookStale + BookMissing
// （SpotMissing 是 TicksValid 的**子集**——它过了盘口门控但 spot 缺失, 故不参与上式）。
type WindowStats struct {
	// Ticks 进入有效性分类的 tick 数（不含 rem==0 终 tick、不含 Done 之后）。
	Ticks int `json:"ticks"`
	// TicksValid 有效 tick（过延迟闸 + 盘口有效价 > 0）——判定只在有效 tick 上做。
	TicksValid int `json:"ticks_valid"`
	// BookStale 延迟超阈而无效的 tick。
	BookStale int `json:"book_stale"`
	// BookMissing **四档全空**而无效的 tick（2026-09-24 起: 缺一侧不再是无效理由,
	// 有效价 ask→bid 兜底, 见 decide.HotBook）。
	BookMissing int `json:"book_missing"`
	// SpotMissing 有效 tick 但 spot 缺失（≤0）——该 tick 不推进任何段、不落行,
	// 等下一个可判定 tick（2026-09-24 起的口径; 历史上这类 tick 一次也没有）。
	SpotMissing int `json:"spot_missing"`
	// Rows 本窗实际产出的行数（判定行/信号行各计一; 0 = 本窗无锚、未跑到 150s 闸
	// 或三段都没出信号）。JSON 键沿用 `frames`（旧统计文件与消费端都按它读）。
	Rows int `json:"frames"`
}
