// Package flip 实现面向纸面交易的 Flip Signal Detection 引擎。
//
// 基于 docs/flip_strategy_plan_2026-08-13.md（Formula B 精简公式）
// 与 python/backtest_flip_scoring.py。
// 检测 Polymarket YES/NO 价格穿越 0.7 的时刻，通过三个信号条件判断
// 该穿越是否可能发生翻转（flip）：
//
//	B1 背离硬要求: BTC 与 PM 反向（min_divergence）
//	B2 过度自信:   BTC 振幅 < 历史平均的一半（range_expansion < 0.5）
//	B3 确认回归:   确认期对侧 bid 回升三档（other_delta）
//
// 纯计算层零外部依赖，外加带状态机的引擎支持实时运行。
package flip

import "time"

// FlipConfig 包含翻转信号检测的全部可调参数。
// 默认值与 python/backtest_flip_config.py 一致，基于 5s 采样数据标定。
// 当前参数为 2026-08-13 Formula B 减法版：n=126, WR 46.0%, P&L +26.91, PF 2.60。
type FlipConfig struct {
	// ── Layer 0: 触发前置条件 ──

	// PM 一侧 bid 超过此阈值即触发穿越检测
	TriggerThreshold float64 `mapstructure:"trigger_threshold"`
	// 穿越时刻之前至少需要的 snapshot 数量（确保有足够的 BTC 价格路径）
	MinPreSnaps int `mapstructure:"min_pre_snaps"`
	// 窗口有效期上限：仅 remaining_sec < 此值的穿越才有效（太早触发时 BTC 路径太短，不可靠）
	MaxRemainingSec int `mapstructure:"max_remaining_sec"`
	// 窗口有效期下限：仅 remaining_sec > 此值的穿越才有效
	// 确保有足够时间完成确认（confirm_delay_ticks × 5s）+ 下单执行
	MinRemainingSec int `mapstructure:"min_remaining_sec"`

	// ── B1 背离硬要求（Formula B 核心）──

	// 穿越时刻 BTC 必须与 PM 反向：背离度 = ±btc_pos（YES侧取负）。
	// 背离度 < 此值 → 否决（同向/中性穿越 EV≈0，χ²=125 分桶证据）。
	// 0 = 禁用
	MinDivergence float64 `mapstructure:"min_divergence"`

	// ── B2 过度自信（振幅扩张，tick 无关，基于前 N 根 K 线平均振幅）──

	// 历史 K 线窗口大小，用于计算平均振幅
	HistWindowN int `mapstructure:"hist_window_n"`
	// 穿越时 BTC 振幅 < 此值 → BTC 没动但 PM 已 0.7+，过度自信 → +2 分
	RangeExpThreshold float64 `mapstructure:"range_exp_threshold"`
	// 穿越时 BTC 振幅 ≥ 此值 → 真突破（BTC 确实大幅移动了），一票否决
	RangeExpMax float64 `mapstructure:"range_exp_max"`

	// ── B3 确认回归（T+N ticks 后观察对侧变化）──

	// 确认等待 tick 数：穿越后等 N 个 tick 再取对面价格（1 tick ≈ 5s）
	ConfirmDelayTicks int `mapstructure:"confirm_delay_ticks"`
	// other_delta 硬过滤下限，-999 = 禁用（由评分权重处理）
	ODHardFilter float64 `mapstructure:"od_hard_filter"`
	// 对面涨幅 > 此值 → 对面强烈回归 → +3 分
	OtherDeltaVStrong float64 `mapstructure:"other_delta_vstrong"`
	// 对面涨幅 > 此值 → 对面明显回归 → +2 分
	OtherDeltaStrong float64 `mapstructure:"other_delta_strong"`
	// 对面涨幅 > 此值 → 对面小幅回归 → +1 分
	OtherDeltaWeak float64 `mapstructure:"other_delta_weak"`

	// ── 入场价格上限 gate（ASK 口径，实盘对齐）──

	// 确认时刻对侧 ASK > 此值 → 信号无效（盈亏比已恶化，实盘 FAK 也无法成交）
	// ASK 口径：对侧 ask = 1 - 触发侧 bid（yes/no_price 存的是 best bid），
	// 与 trading.max_price 的 FAK 最优卖价校验口径一致；0 = 禁用
	MaxEntryPrice float64 `mapstructure:"max_entry_price"`

	// ── 评分权重（Formula B）──

	// 对面大涨权重 (od > vstrong)
	WOtherD5VStrong int `mapstructure:"w_other_delta_vstrong"`
	// 对面中涨权重 (od > strong)
	WOtherD5Strong int `mapstructure:"w_other_delta_strong"`
	// 对面小涨权重 (od > weak)
	WOtherD5Weak int `mapstructure:"w_other_delta_weak"`
	// 振幅偏小权重（BTC 没动但 PM 0.7+）
	WRangeExpansion int `mapstructure:"w_range_expansion"`

	// ── 入场阈值 ──

	// 复合评分 ≥ 此值 → 开仓 1 share（B2 或 od>strong 任一成立即触发）
	ScoreEntry int `mapstructure:"score_entry"`
	// 复合评分 ≥ 此值 → 加仓（99 = 禁用）
	ScoreAdd int `mapstructure:"score_add"`

	// ── 订单簿延迟风控 ──

	// 订单簿数据延迟超过此值（毫秒）则信号不可信，0 = 禁用
	MaxLatencyMs int64 `mapstructure:"max_latency_ms"`
}

// DefaultConfig 返回翻转信号检测的默认参数配置。
// 2026-08-13 Formula B 减法版标定：n=126, WR 46.0%, P&L +26.91, PF 2.60。
func DefaultConfig() FlipConfig {
	return FlipConfig{
		TriggerThreshold:    0.7,  // PM 一侧 bid >0.7 触发
		MinPreSnaps:         5,    // 穿越前至少 5 个 snapshot
		MaxRemainingSec:     260,  // 窗口有效期上限：remaining_sec < 260s 的穿越才有效
		MinRemainingSec:     35,   // 窗口有效期下限：remaining_sec > 35s（2 ticks×5s + 25s 执行缓冲）
		MinDivergence:       0.05, // B1: BTC 必须与 PM 背离（YES侧 btc_pos<-0.05，NO侧 >0.05）
		HistWindowN:         18,   // 前 18 根 K 线（~1.5h）算平均振幅
		RangeExpThreshold:   0.5,  // 振幅 <0.5 → BTC 没动但 PM 0.7+ → 过度自信 → +2
		RangeExpMax:         1.5,  // 振幅 ≥1.5 → 真突破，否决
		ConfirmDelayTicks:   2,    // 确认等待 2 ticks（10s）
		ODHardFilter:        -999.0, // 禁用硬过滤，由评分权重处理
		OtherDeltaVStrong:   0.05, // 对面涨 >0.05 → +3 分
		OtherDeltaStrong:    0.02, // 对面涨 >0.02 → +2 分
		OtherDeltaWeak:      0.01, // 对面涨 >0.01 → +1 分
		MaxEntryPrice:       0.45, // 确认时刻对侧 ASK >0.45 → 信号无效（fill≥0.50 转负 EV）
		WOtherD5VStrong:     3,    // 对面大涨权重
		WOtherD5Strong:      2,    // 对面中涨权重
		WOtherD5Weak:        1,    // 对面小涨权重
		WRangeExpansion:     2,    // 振幅偏小权重
		ScoreEntry:          2,    // ≥2 分开仓（B2 或 od>strong 任一成立即触发）
		ScoreAdd:            99,   // 加仓禁用
		MaxLatencyMs:        0,    // 0=禁用，实盘建议 300ms
	}
}

// ── 状态机 ──

type flipState int

const (
	stateIdle       flipState = iota // waiting for Reset()
	stateWatching                    // accumulating snapshots, watching for >0.7
	stateConfirming                  // crossed, waiting for T+5s confirmation
	stateDone                        // cycle complete or vetoed
)

// ── 信号输出 ──

// FlipSignal 表示一个检测到的翻转交易信号。
// 字段与 Python FlipSignal dataclass 对齐，确保 JSONL 兼容。
type FlipSignal struct {
	Time         time.Time `json:"time"`
	ConditionID  string    `json:"condition_id"`
	Side         string    `json:"side"` // "yes" or "no"
	Score        int       `json:"score"`
	EntryPrice   float64   `json:"entry_price"` // 穿越时刻对侧 bid（特征/纸面结算用）
	Shares       float64   `json:"shares"` // 目标股数 = stake / entry_price（Engine 不设置，由调用方计算）
	RemainingSec int       `json:"remaining_sec"`

	// 执行结果（Trader 回填）
	ExecStatus   string    `json:"exec_status"`            // "pending" | "filled" | "failed"
	FilledAt     time.Time `json:"filled_at,omitempty"`    // 成交确认时间（UTC）
	FilledShares float64   `json:"filled_shares"`          // 实际成交股数（failed 时为 0）
	AvgFillPrice float64   `json:"avg_fill_price"`         // 实际成交均价（纸面 = entryPrice，实盘 = CLOB 真实价）
	SlippageBps  float64   `json:"slippage_bps,omitempty"` // entry → fill 滑点（bp），正=不利

	// 特征详情（分析/调试用）
	RangeExpansion float64 `json:"range_expansion"`
	BTCPosition    float64 `json:"btc_position"`
	BtcDivergence  float64 `json:"btc_divergence"` // 背离度：正=BTC 与 PM 反向（YES侧取 -btc_pos）
	OtherDelta     float64 `json:"other_delta"`

	// 结算时填充
	Won        bool      `json:"won"`
	PnL        float64   `json:"pnl"`
	ResolvedAt time.Time `json:"resolved_at,omitempty"` // 结算时间（UTC）

	// 数据质量
	OrderBookLatency int64 `json:"order_book_latency,omitempty"` // 订单簿最大延迟（毫秒），诊断用
}

// ── 评分输入 ──

// ScoreParams 封装 ComputeFlipScore 的全部输入参数（Formula B）。
type ScoreParams struct {
	OtherDelta       float64     // B3: 确认期对侧 bid 变化
	RangeExpansion   float64     // B2: 穿越时 BTC 振幅（历史振幅单位）
	HistReady        bool        // 历史振幅是否就绪（未就绪时 B2/F0 不参与）
	OrderBookLatency int64       // 订单簿最大延迟（毫秒），用于延迟风控
	Cfg              FlipConfig  // 评分参数
}
