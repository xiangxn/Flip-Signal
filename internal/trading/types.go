// Package trading 实现 Flip Signal 的实盘交易执行层。
//
// 负责将 FlipSignal 映射为 Polymarket CLOB GTC 限价单、跟踪成交、
// 依据真实市场结算计算 realized P&L，并施加风险控制。
// 纯风险计算（risk.go / order.go）零外部依赖，SDK 依赖仅限 client.go。
package trading

import "time"

// ── 交易配置 ──

// TradingConfig 实盘交易执行参数。
type TradingConfig struct {
	Enabled              bool    `mapstructure:"enabled"`                // 启动时是否启用实盘（true=实盘，false=纸面）
	OutputPath           string  `mapstructure:"output_path"`            // 交易记录 JSONL 路径
	StakePerSignal       float64 `mapstructure:"stake_per_signal"`       // 每信号投入 USDC
	MaxSlippage          float64 `mapstructure:"max_slippage"`           // 滑点容忍（价格上限 = entry×(1+slippage)）
	MaxDailyLoss         float64 `mapstructure:"max_daily_loss"`         // 日亏上限 USDC，触发后当日停止
	CooldownAfterLossSec int     `mapstructure:"cooldown_after_loss_sec"` // 亏损后冷却秒数
	ResolutionTimeoutSec int     `mapstructure:"resolution_timeout_sec"` // WS 结算等待超时秒数
}

// DefaultConfig 返回安全的默认 TradingConfig。
func DefaultConfig() TradingConfig {
	return TradingConfig{
		Enabled:              false,
		OutputPath:           "data/trades.jsonl",
		StakePerSignal:       5.0,
		MaxSlippage:          0.07,
		MaxDailyLoss:         10.0,
		CooldownAfterLossSec: 300,
		ResolutionTimeoutSec: 300,
	}
}

// ── 订单状态 ──

// OrderState 订单生命周期状态。
type OrderState int

const (
	OrderPending   OrderState = iota // 已构造，待提交
	OrderSubmitted                   // 已提交 CLOB
	OrderFilled                      // 全部成交
	OrderFailed                      // 提交或成交失败
)

// StateLabel 返回订单状态的中文标签。
func (s OrderState) String() string {
	switch s {
	case OrderPending:
		return "待提交"
	case OrderSubmitted:
		return "已提交"
	case OrderFilled:
		return "已成交"
	case OrderFailed:
		return "失败"
	default:
		return "未知"
	}
}

// ── 订单记录 ──

// OrderRecord 一条 GTC 订单的完整记录。
type OrderRecord struct {
	ID           string     `json:"id"`             // CLOB orderID（PostOrder 返回）
	ConditionID  string     `json:"condition_id"`   // 所属市场
	TokenID      string     `json:"token_id"`       // 买入的 token ID
	Side         string     `json:"side"`           // BUY
	TokenSide    string     `json:"token_side"`     // "yes"/"no"，持仓方向
	Price        float64    `json:"price"`          // 价格上限（maxPrice）
	RequestedAmt float64    `json:"requested_amt"`  // 请求 USDC 金额
	FilledShares float64    `json:"filled_shares"`  // 实际成交股数
	AvgFillPrice float64    `json:"avg_fill_price"` // 成交均价
	State        OrderState `json:"state"`          // 订单状态
	ErrorMsg     string     `json:"error_msg,omitempty"`
	CreatedAt    time.Time  `json:"created_at"`
	UpdatedAt    time.Time  `json:"updated_at"`
}

// ── 持仓 ──

// Position 一个市场的持仓记录。
type Position struct {
	ConditionID      string    `json:"condition_id"`
	TokenID          string    `json:"token_id"`
	TokenSide        string    `json:"token_side"`        // "yes"/"no"
	Shares           float64   `json:"shares"`            // 实际成交股数
	AvgPrice         float64   `json:"avg_price"`         // 加权成交均价
	CostUSDC         float64   `json:"cost_usdc"`         // 投入成本 = shares × avgPrice
	OrderID          string    `json:"order_id"`          // 来源订单 ID
	OpenedAt         time.Time `json:"opened_at"`
	SettledAt        time.Time `json:"settled_at,omitempty"`
	Won              bool      `json:"won"`
	PnL              float64   `json:"pnl"`
	Outcome          int       `json:"outcome"`           // 0=Up 1=Down
	ResolutionSource string    `json:"resolution_source"` // "poller"（ResolutionPoller 通过 gamma API 确认后结算）
}

// ── 运行期状态快照 ──

// ExecutionState Trader 的运行期状态快照，供日志与 Dashboard 读取。

// ExecInfo 信号执行结果，由 OnSignal 返回供调用方回填 FlipRecorder。
type ExecInfo struct {
	Status       string  // "pending" | "filled" | "failed"
	FilledShares float64 // 实际成交股数
	AvgFillPrice float64 // 实际成交均价
}
// pendingGtcOrder 记录已提交但尚未完全成交的 GTC 限价单上下文，
// 供 TradeMonitor 事件循环和周期末对账使用（仅内存，不持久化）。
type pendingGtcOrder struct {
	OrderID     string       // CLOB order ID
	TokenID     string       // 买入的 token ID
	TokenSide   string       // "yes"/"no"
	ConditionID string       // 所属市场
	MaxPrice    float64      // 限价
	StakeUSDC   float64      // 投入预算
	Rec         *OrderRecord // 关联订单记录

	// TradeMonitor 事件累积
	FilledShares float64 // 已成交股数
	TotalCost    float64 // 已花费 USDC（∑ trade.size × trade.price）
	TradeCount   int     // 成交笔数
	Status       string  // 最后收到的 order status (LIVE/MATCHED/CANCELED)
}

type ExecutionState struct {
	Enabled          bool      `json:"enabled"`
	DailyPnl         float64   `json:"daily_pnl"`
	DailySignals     int       `json:"daily_signals"`
	DailyLimitHit    bool      `json:"daily_limit_hit"`
	CooldownUntil    time.Time `json:"cooldown_until"`
	CurrentCondition string    `json:"current_condition"`
	Position       *Position        `json:"position,omitempty"` // 当前未结算持仓
	PendingOrder   *pendingGtcOrder `json:"-"`                  // 当前未成交 GTC 挂单（仅内存）
	LastSkipReason   string    `json:"last_skip_reason"`
}
