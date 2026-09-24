// Package flip 实现「狗@0.2」策略引擎：买入被砸到 0.2 的下狗。
//
// 检测 Polymarket UP/DOWN 盘口某侧 ask 首次 ≤0.20 的 tick（触底观测），
// 一次性判定三腿（急跌 × 浅洞 × 时间，docs/engine_plan_dog020_2026-09-02.md）:
//
//	急跌   m_45: 触发前 45 tick 内同侧 ask 曾 ≥ 0.40（窗口 max）
//	浅洞   dist_s ∈ (lo(side), 0): spot 在狗败侧浅坑——侧别带 lo: yes −0.6 / no −1.0
//	       （2026-09-03 组合版定带, 见 01_backtest_r1.py BAND_YC/BAND_NO）
//	时间   rem > 180
//
// dist 定义: dist = sgn·(price − anchor)/anchor·1e4 / hist_bps
// sgn: dog=yes +1 / dog=no −1；hist_bps = 前 ≤18 窗 |tw_close−tw_open| 均值。
// 成交口径: fill = 触发 tick 狗侧 ask（≤0.20，无滑点），shares = stake/fill。
// 纯计算层零外部依赖，状态机支持实时运行（1s tick 粒度）。
package flip

// 词面映射常量（回测/引擎/记录共同口径，防 dog↔outcome 反向写错）:
// PM 市场 outcome: 0=Up=yes token，1=Down=no token。
const (
	// OutcomeUp 是官方结算结果 Up（= yes 赢）。
	OutcomeUp = 0
	// OutcomeDown 是官方结算结果 Down（= no 赢）。
	OutcomeDown = 1
	// SideYes 记录侧: yes（Up）。
	SideYes = "yes"
	// SideNo 记录侧: no（Down）。
	SideNo = "no"
)

// 拒绝原因常量（记录与 Dashboard 共用；顺序与判定顺序一致，见 engine.go）。
const (
	// RejectRemLow 时间腿不过: 触发时 rem ≤ RemMin。
	RejectRemLow = "rem_low"
	// RejectNoHist σ 窗口不足: hist_bps 不可用（<3 个已完窗口）。
	RejectNoHist = "no_hist"
	// RejectMissingSpot 浅洞腿输入缺失: 本 tick 无有效 Binance spot。
	RejectMissingSpot = "missing_spot"
	// RejectMissingAnchor 锚缺失: 窗口开盘 TWAP 值不可用（≤0）。
	RejectMissingAnchor = "missing_anchor"
	// RejectNoCrash 急跌腿不过: m_45 窗口内 ask 从未 ≥ CrashMinAsk。
	RejectNoCrash = "no_crash"
	// RejectDistOut 浅洞腿不过: dist_s 不在 (lo(side), DistHi) 开区间
	//（侧别带: yes DistLoYes / no DistLoNo）。
	RejectDistOut = "dist_out"
)

// 风控闸原因常量（Record.GateReason 取值; 空 = 未被闸）。
// 口径见 docs/dog020_risk_latency_plan_2026-09-16.md §3。
const (
	// GateDailyLoss 日亏熔断: 当日（UTC）已结算 P&L ≤ MaxDailyLoss。
	// 触发即当日锁存（此前已封过一笔也保持停单, 见 ExecState.breakerTripped）。
	GateDailyLoss = "daily_loss"
	// GateFirstWindow live 重启后首窗禁单（防重启残留窗双单; paper 无仓位不设）。
	GateFirstWindow = "first_window"
)

// 策略配置 Config 与默认值工厂 DefaultConfig 在 config.go。
// 本文件只放引擎的数据/状态类型。

// ── 状态机 ──

type engineState int

const (
	stateWatching engineState = iota // 等待首个触底（ask≤0.20）观测
	stateDone                        // 已判定（观测产出）或窗口已结束
)

// StateLabel 返回状态机可读名（Dashboard 用）。
func (s engineState) String() string {
	switch s {
	case stateWatching:
		return "Watching"
	case stateDone:
		return "Done"
	}
	return "Unknown"
}

// ── 输入 ──

// Tick 是每秒一条的引擎输入（盘口 + 决策所需快照，来源 cmd/flip main 采样）。
// 盘口缺失（WS 断流/陈旧）时 bid/ask 为 0；spot/TWAP 缺失时价格 ≤0。
type Tick struct {
	Ts        int64   // 本地采样时刻（unix 毫秒）
	Rem       int     // 窗口剩余秒数（0 = 窗口结束）
	UpBid     float64 // UP(=yes) 最优买价
	UpAsk     float64 // UP(=yes) 最优卖价
	DownBid   float64 // DOWN(=no) 最优买价
	DownAsk   float64 // DOWN(=no) 最优卖价
	BookLatMs int64   // 盘口传输延迟（毫秒，> Config.MaxBookLatMs 视为无效 tick）
	BinPrice  float64 // Binance BTCUSDT 最新 @trade 价（≤0 = 缺失/陈旧）
	SpotAgeMs int64   // spot 距本地接收毫秒数（-1 = 尚无推送; 诊断，不参与判定）
	TwapPrice float64 // Chainlink TWAP-60 现值（≤0 = 缺失；dist_t 观察腿输入）
	TwapAgeMs int64   // TWAP 距上次推送毫秒数（诊断）
}

// ── 实盘执行 ──

// ExecStatus* 是 live 执行状态（Record.ExecStatus 取值; paper 行无 exec 字段,
// 空串 = 纸面模拟成交）。
// 生命周期（2026-09-10 实盘接入; 2026-09-19 起下单为 GTC, 多一个 resting 中间态）:
//
//	submitting ──(POST 返回)──┬─→ filled（即时全额成交, 终态）
//	                          ├─→ resting（挂单在簿, 成交量未定）
//	                          └─→ rejected（未受理: 风控闸/CLOB 拒绝/网络错误）
//	resting ──(FillTracker: rem≤RemMin 撤单 + 查询终态)──→ filled / partial / unfilled
//
// ⚠️ resting ≠ 终态: 仓位大小要等挂单走完（到 rem ≤ 策略时间腿被我们撤掉, 或闭市
// 自动撤回）才能定稿, 故 resting 行**不入 pending、不注册结算**——否则中途结算会
// 按半个仓位记账。filled/partial 为真实持仓, 才注册结算轮询。
const (
	ExecStatusSubmitting = "submitting" // 已写行、POST 未回填（崩溃缝隙, 重启人工核对）
	ExecStatusResting    = "resting"    // GTC 挂单在簿, 成交量未定（终态由 FillTracker 回填）
	ExecStatusFilled     = "filled"     // 全额成交（实际 shares == 目标）
	ExecStatusPartial    = "partial"    // 部分成交（实际 shares < 目标）
	ExecStatusUnfilled   = "unfilled"   // 0 成交（引擎已 Done, 不重试）
	ExecStatusRejected   = "rejected"   // 未下单（风控闸/CLOB 拒绝/网络错误）
)

// ExecNoteUnknown 是 ExecNote 的「成交结果不明」标记前缀: 网络错误/超时不等同于
// 未受理, 订单可能已成交——该行与 submitting 同属重启人工核对类（Recorder 载入
// 扫描与 NeedsReconcile 的判据, 勿自动补单）。两种来源:
//   - rejected 行: POST 传输错误/超时（订单可能已受理）;
//   - GTC 挂单终态行: 闭市后查询不到该单、也从未观测到过（成交量无从确认）。
//
// HTTP ≥400 拒单有服务端明确失败响应, 不属此类。
const ExecNoteUnknown = "未知结果"

// ExecResult 是一次实盘下单的终态结果（internal/trading LiveExecutor.Execute 产出,
// Recorder.CompleteExecution 消费回填）。纯数据类型放 flip: recorder 侧引用无需
// 反向依赖 trading（trading→flip 单向, 无环）。
type ExecResult struct {
	Status    string  // ExecStatus*: filled/partial/unfilled/rejected
	OrderID   string  // CLOB 订单 id（POST 响应回填; 网络错误时不可得）
	FillPrice float64 // 实际成交均价 = Cost/Shares（0 = 未成交）
	Shares    float64 // 实际成交股数（0 = 未成交）
	Cost      float64 // 实际花费 USDC（0 = 未成交/拒绝）
	Note      string  // 拒绝原因; ExecNoteUnknown 开头 = POST 结果不明
}

// FillFinal 是一笔 GTC 挂单的**终态成交**（trading.FillTracker 在闭市前查询
// CLOB `size_matched` 后产出, ExecState.ApplyFillFinal 消费落盘）。
// 与 ExecResult 的分工: ExecResult = POST 那一刻的即时结果（可能只是挂单在簿）;
// FillFinal = 挂单走完全部生命后的累计成交——仓位大小与结算 P&L 以它为准。
type FillFinal struct {
	ConditionID string  // 定位落盘行（一窗至多一笔信号, 与 CompleteExecution 同键）
	Shares      float64 // 累计成交股数（CLOB size_matched）
	Cost        float64 // 累计花费 USDC（= Shares × 限价, 保守口径见 FillTracker doc）
	Status      string  // ExecStatus*: filled/partial/unfilled; resting = 仍未确认（只更新 note）
	Note        string  // 终态说明（写 ExecNote; ExecNoteUnknown 前缀 = 需人工核对）
}

// ── 输出 ──

// Observation 是一次触底观测（成功与失败都产出，诊断/校准信号频率用）。
// 与回测 extract 观测 1:1 对齐（python/v4/01_backtest_r1.py）。
// json 键与回测 trades CSV 同名（m_20/m_30/m_45/dist_s/dist_t 等，09-15 复验映射用）。
type Observation struct {
	Ts           int64   `json:"ts"`                        // 触底时刻（unix 毫秒）
	Side         string  `json:"side"`                      // 狗侧: "yes"/"no"
	Rem          int     `json:"rem"`                       // 触底时窗口剩余秒
	Fill         float64 `json:"fill"`                      // 狗侧 ask@触发（成交口径，≤0.20）
	M20          float64 `json:"m_20"`                      // 前 20 槽位窗口 ask max（0 = 无有效 ask）
	M30          float64 `json:"m_30"`                      // 前 30 槽位窗口 ask max
	M45          float64 `json:"m_45"`                      // 前 45 槽位窗口 ask max（急跌腿）
	DistS        float64 `json:"dist_s,omitempty"`          // 浅洞 dist（spot 口径；0 = 未计算）
	DistT        float64 `json:"dist_t,omitempty"`          // 观察: dist（TWAP 口径，不参与决策）
	OK           bool    `json:"ok"`                        // 是否通过全部腿成为信号
	RejectReason string  `json:"reject_reason,omitempty"`   // 未通过原因
	Shares       float64 `json:"shares,omitempty"`          // 目标股数 = stake/fill（仅 ok=true）
	BookLatMs    int64   `json:"book_latency_ms,omitempty"` // 触底时刻盘口延迟（诊断）
	TwapAgeMs    int64   `json:"twap_age_ms,omitempty"`     // TWAP 距上次推送毫秒数（诊断）

	// 浅洞腿决策原始输入（2026-09-03 起落盘，诊断 anchor/σ/spot 口径差与
	// yes/no 进带率漂移用；0 = 该输入当时缺失，与 reject_reason 呼应）
	Anchor  float64 `json:"anchor"`   // 本窗 anchor = 边界 TWAP-60 流值（USD）
	HistBps float64 `json:"hist_bps"` // σ（bps）: 前 ≤18 已完窗 |close−open| 均值（0 = 不足 3 窗）
	Spot    float64 `json:"spot"`     // 触底 tick Binance BTCUSDT 价（0 = 缺失/陈旧 >2s）
	// 触底 tick 的 spot 本地接收龄（毫秒，-1 = 尚无推送）。2026-09-16 补: 此前只落
	// spot 价格不落年龄，0.1s 与 1.9s（亚阈值陈旧）在数据里无从区分——而触发点正是
	// 急动点（崩盘秒 1s 位移可达 0.8~1.6σ，yes 侧浅洞带总宽仅 0.6σ），陈旧足以把
	// 一笔单从带内推到带外。纯诊断，不参与判定（口径见 docs/dog020_risk_latency_plan_2026-09-16.md §2.3）
	SpotAgeMs int64   `json:"spot_age_ms,omitempty"`
	TwapPrice float64 `json:"twap_price"` // 触底 tick TWAP-60 流值（dist_t 观察腿输入）

	// 美元位移（2026-09-25 补，2026-09-24 实盘断崖复查的后续诊断）：与 dist_s/dist_t
	// 同输入、同号口径（sgn: yes +1 / no −1），但单位是**美元**而不是 σ —— σ 尺子本身
	// 带 regime 漂移（同一 1.8bps 位移，回测算 0.19σ、实盘算 0.26σ），美元才是绝对尺子，
	// 用来回答「信号出现时波动是否已经把现货/TWAP 推离 anchor 很远」。
	// 与 tail 的 dev 同口径（tail: dev = sgn·(spot − anchor)，美元）。
	// 0/缺省 = 输入缺失（anchor、spot 或 twap ≤ 0）；0 也正好是「现货恰在锚上」，
	// 两者同样是「无从判读」，故不再细分。
	DevUSD     float64 `json:"dev_usd,omitempty"`      // sgn·(spot − anchor)
	TwapDevUSD float64 `json:"twap_dev_usd,omitempty"` // sgn·(twap_price − anchor)
}

// Record 是一条触底观测的完整落盘记录（成功与失败都记）。
// 字段 = Observation 全部 + 记录元信息（结算后回填 won/pnl/resolved_at;
// live 行回填 exec_status/order_id/avg_fill_price/cost/exec_note——paper 行
// 无这些字段, omitempty 对旧日文件与 python 脚本零影响）。
type Record struct {
	Observation
	EventType   string  `json:"event_type"` // 恒为 "touch"
	Date        string  `json:"date"`       // UTC 日（按日切分文件名，与回测 date 口径一致）
	ConditionID string  `json:"condition_id"`
	Slug        string  `json:"slug"`
	EventStart  int64   `json:"event_start"`           // 窗口起点（unix 秒）
	Stake       float64 `json:"stake,omitempty"`       // 每笔投入 USDC（结算 P&L 基准）
	Won         *bool   `json:"won,omitempty"`         // 结算后填充（狗侧是否赢）
	PnL         float64 `json:"pnl,omitempty"`         // 结算后填充（USDC）
	ResolvedAt  string  `json:"resolved_at,omitempty"` // 结算时间（RFC3339）

	// SettleSrc 结算来源（internal/settle 的三层回退, 2026-09-24 起）:
	// push = 边界推送自算（主路径）/ official = 官方接口兜底 / gamma = UMA 轮询。
	// 空 = 未记（本字段引入之前的行）。事后核对「自算是否与官方一致」靠它分桶。
	SettleSrc string `json:"settle_src,omitempty"`

	// GateReason 风控闸原因（GateDailyLoss/GateFirstWindow; 空 = 未被闸）。
	//
	// 两模式都写（2026-09-16 方案 A, docs §3.5）: paper 被闸行行 schema 与正常信号
	// 完全一致（IsFilled 仍 true、照常结算回填 won/pnl）——纸面是当前唯一在跑的
	// live-like 样本, 砍数据会削弱 09-30 复盘的统计力, 而被闸行正是「不熔断会怎样」
	// 的反事实。⚠️ 因此分析脚本必须显式过滤（02/06 的 --include-gated 对照）。
	GateReason string `json:"gate_reason,omitempty"`

	// ── live 执行回填（paper 行恒空）──
	ExecStatus string  `json:"exec_status,omitempty"`    // 空=paper; 取值见 ExecStatus*
	OrderID    string  `json:"order_id,omitempty"`       // CLOB 订单 id
	FillPrice  float64 `json:"avg_fill_price,omitempty"` // 实际成交均价（≤ 观测 fill; 吃单可价格改善）
	Cost       float64 `json:"cost,omitempty"`           // 实际花费 USDC（结算 P&L 基准; paper 无 → Resolve 用 Stake 兜底）
	ExecNote   string  `json:"exec_note,omitempty"`      // rejected/unknown 原因
}

// IsFilled 判断记录是否实际成交（须注册结算轮询）: paper 行（ExecStatus 空 =
// 恒模拟全额成交）与 live filled/partial 为真; unfilled/rejected/submitting/
// resting 为假（resting = 挂单在簿成交量未定, 注册结算须等 FillTracker 回填）。
func (r *Record) IsFilled() bool {
	switch r.ExecStatus {
	case "", ExecStatusFilled, ExecStatusPartial:
		return true
	}
	return false
}
