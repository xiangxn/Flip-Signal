// Package flip 实现「狗@0.2」策略引擎：买入被砸到 0.2 的下狗。
//
// 检测 Polymarket UP/DOWN 盘口某侧 ask 首次 ≤0.20 的 tick（触底观测），
// 一次性判定三腿（急跌 × 浅洞 × 时间，docs/engine_plan_dog020_2026-09-02.md）:
//
//	急跌   m_45: 触发前 45 tick 内同侧 ask 曾 ≥ 0.40（窗口 max）
//	浅洞   dist_s ∈ (-0.5, 0): spot 在狗败侧、向狗败方向偏锚 ≤0.5σ（浅坑未走深）
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
	// RejectDistOut 浅洞腿不过: dist_s 不在 (DistLo, DistHi) 开区间。
	RejectDistOut = "dist_out"
)

// Config 包含策略的全部可调参数。
// 默认值与回测标定一致（python/v4/01_backtest_r1.py 常量，2026-09-02 定稿）。
type Config struct {
	// 触发阈值: 某侧 ask 满足 0 < ask ≤ 此值 即触底观测（0.20）
	TriggerAskMax float64
	// 急跌腿阈值: m_45 窗口内同侧 ask 曾 ≥ 此值（0.40, CRASH_MIN）
	CrashMinAsk float64
	// 急跌窗口: 触发前 N 个 tick 槽位内求 max（45, CRASH_WINDOW）
	CrashWindow int
	// 浅洞带: dist_s 必须 ∈ (DistLo, DistHi) 开区间（-0.5, 0, BAND）
	DistLo float64
	DistHi float64
	// 时间腿: 仅 rem > 此值 的触发有效（180, REM_MIN）
	RemMin int
	// 每信号投入 USDC（2）
	Stake float64
}

// DefaultConfig 返回回测标定的默认参数（python/v4/01_backtest_r1.py）:
// m_45 纯现货 n=245, WR 29.0%, EV +1.078U/注, +264U/14 天。
func DefaultConfig() Config {
	return Config{
		TriggerAskMax: 0.20, // 0.2 触底（dog@0.2 规则族核心）
		CrashMinAsk:   0.40, // 急跌: 45s 窗口内曾 ≥ 0.40
		CrashWindow:   45,   // 急跌窗（tick 槽位）
		DistLo:        -0.5, // 浅洞带下界: 坑深 ≤0.5σ（坑过深 = 砸盘被现货确认）
		DistHi:        0.0,  // 浅洞带上界: dist_s<0 = 现货须在狗败侧（方向约束）
		RemMin:        180,  // 窗口前 2 分钟内才观测
		Stake:         2,    // 每笔 2 USDC
	}
}

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
	BookLatMs int64   // 盘口传输延迟（毫秒，>300 视为无效 tick）
	BinPrice  float64 // Binance BTCUSDT 最新 @trade 价（≤0 = 缺失/陈旧）
	TwapPrice float64 // Chainlink TWAP-60 现值（≤0 = 缺失；dist_t 观察腿输入）
	TwapAgeMs int64   // TWAP 距上次推送毫秒数（诊断）
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
	Anchor    float64 `json:"anchor"`    // 本窗 anchor = 边界 TWAP-60 流值（USD）
	HistBps   float64 `json:"hist_bps"`  // σ（bps）: 前 ≤18 已完窗 |close−open| 均值（0 = 不足 3 窗）
	Spot      float64 `json:"spot"`      // 触底 tick Binance BTCUSDT 价（0 = 缺失/陈旧 >2s）
	TwapPrice float64 `json:"twap_price"` // 触底 tick TWAP-60 流值（dist_t 观察腿输入）
}

// Record 是一条触底观测的完整落盘记录（成功与失败都记）。
// 字段 = Observation 全部 + 记录元信息（结算后回填 won/pnl/resolved_at）。
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
}
