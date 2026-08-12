// Package flip 实现面向纸面交易的 Flip Signal Detection 引擎。
//
// 基于 docs/flip_backtest_plan.md 与 python/backtest_flip_scoring.py。
// 检测 Polymarket YES/NO 价格穿越 0.7 的时刻，通过 7 特征复合评分
// 判断该穿越是否可能发生翻转（flip）。
//
// 纯计算层零外部依赖，外加带状态机的引擎支持实时运行。
package flip

import "time"

// FlipConfig 包含翻转信号检测的全部可调参数。
// 默认值与 python/backtest_flip_config.py 一致，基于 5s 采样数据标定。
// 当前参数为 2026-08-12 网格搜索优化结果：胜率 50.9%, P&L +62.40, 盈亏比 2.3。
type FlipConfig struct {
	// ── Layer 0: 触发前置条件 (§2.1) ──

	// PM 一侧价格超过此阈值即触发穿越检测
	TriggerThreshold float64 `mapstructure:"trigger_threshold"`
	// 多穿越重试：为 true 时，每个向上穿越 0.7 的上升沿都尝试评分，首个通过者获胜
	AllowRetryCrossings bool `mapstructure:"allow_retry_crossings"`
	// 穿越时刻之前至少需要的 snapshot 数量（确保有足够的 BTC 价格路径）
	MinPreSnaps int `mapstructure:"min_pre_snaps"`
	// 窗口有效期上限：仅 remaining_sec < 此值的穿越才有效（太早触发时 BTC 路径太短，不可靠）
	MaxRemainingSec int `mapstructure:"max_remaining_sec"`
	// 窗口有效期下限：仅 remaining_sec > 此值的穿越才有效
	// 确保有足够时间完成确认（confirm_delay_ticks × 5s）+ 下单执行
	MinRemainingSec int `mapstructure:"min_remaining_sec"`

	// ── 来回振荡判定（三条件必须同时满足）──

	// 路径效率 ≤ 此值 → 振荡候选。~1.0 为单边趋势, ~0.0 为来回振荡
	PathEffOscillating float64 `mapstructure:"path_eff_oscillating"`
	// 噪声比 > 此值 → 振荡候选。总路径长度 / 净位移，越大越"碎"
	NoiseRatioOscillating float64 `mapstructure:"noise_ratio_oscillating"`
	// 方向翻转次数 > 此值 → 振荡候选。BTC 价格方向改变的次数
	FlipsOscillating int `mapstructure:"flips_oscillating"`

	// ── 振幅扩张（tick 无关，基于前 N 根 K 线平均振幅）──

	// 历史 K 线窗口大小，用于计算平均振幅
	HistWindowN int `mapstructure:"hist_window_n"`
	// 穿越时 BTC 振幅 < 此值 → BTC 没怎么动但 PM 已经 0.7+，过度自信 → +2 分
	RangeExpThreshold float64 `mapstructure:"range_exp_threshold"`
	// 穿越时 BTC 振幅 ≥ 此值 → 真突破（BTC 确实大幅移动了），一票否决
	RangeExpMax float64 `mapstructure:"range_exp_max"`

	// ── 对面价格确认（T+N ticks 后观察对侧变化）──

	// 确认等待 tick 数：穿越后等 N 个 tick 再取对面价格（1 tick ≈ 5s）
	ConfirmDelayTicks int `mapstructure:"confirm_delay_ticks"`	// other_delta 硬过滤下限，-999 = 禁用（由评分权重处理）
	ODHardFilter float64 `mapstructure:"od_hard_filter"`
	// 对面涨幅 > 此值 → 对面强烈回归 → +3 分
	OtherDeltaVStrong float64 `mapstructure:"other_delta_vstrong"`
	// 对面涨幅 > 此值 → 对面明显回归 → +2 分
	OtherDeltaStrong float64 `mapstructure:"other_delta_strong"`
	// 对面涨幅 > 此值 → 对面小幅回归 → +1 分
	OtherDeltaWeak float64 `mapstructure:"other_delta_weak"`

	// ── BTC 位置 vs Open（以历史平均振幅为单位，tick 无关）──

	// NO 侧穿越时：btc_pos > 此值 → BTC 涨但 PM 看跌（背离），flip edge → +1 分
	BTCPosMax float64 `mapstructure:"btc_pos_max"`
	// YES 侧穿越时：btc_pos < 此值 → BTC 跌但 PM 看涨（背离），flip edge → +1 分
	BTCPosMin float64 `mapstructure:"btc_pos_min"`

	// ── T=0 质量硬过滤 ──

	// 路径效率 < 此值 → 趋势极不明确，穿越时直接否决
	PathEffVetoMin float64 `mapstructure:"path_eff_veto_min"`
	// 噪声比 > 此值 → 价格极度不稳定，穿越时直接否决
	NoiseRatioVetoMax float64 `mapstructure:"noise_ratio_veto_max"`

	// ── 入场价格折扣 ──

	// 对侧价格 < 此值 → 极低价入场 → +1 分
	EntryCheapStrong float64 `mapstructure:"entry_cheap_strong"`
	// 对侧价格 < 此值 → 低价入场 → +1 分（elif 不叠加）
	EntryCheapWeak float64 `mapstructure:"entry_cheap_weak"`
	// 确认时刻对侧价 > 此值 → 信号无效（盈亏比已恶化，实盘 FAK 也无法成交）
	// 与 trading.max_price 保持一致；0 = 禁用
	MaxEntryPrice float64 `mapstructure:"max_entry_price"`

	// ── 评分权重 ──

	// 对面大涨权重 (od > vstrong)
	WOtherD5VStrong int `mapstructure:"w_other_delta_vstrong"`
	// 对面中涨权重 (od > strong)
	WOtherD5Strong int `mapstructure:"w_other_delta_strong"`
	// 对面小涨权重 (od > weak)
	WOtherD5Weak int `mapstructure:"w_other_delta_weak"`
	// 来回振荡权重（降低：振荡信号胜率 31% 远低于趋势 45%）
	WOscillating int `mapstructure:"w_oscillating"`
	// 极低价入场权重
	WCheapEntryStr int `mapstructure:"w_cheap_entry_strong"`
	// 低价入场权重
	WCheapEntryWeak int `mapstructure:"w_cheap_entry_weak"`
	// 振幅偏小权重（BTC 没动但 PM 0.7+）
	WRangeExpansion int `mapstructure:"w_range_expansion"`
	// BTC 背离权重（BTC 与 PM 方向相反）
	WBtcExtreme int `mapstructure:"w_btc_extreme"`

	// ── 入场阈值 ──

	// 复合评分 ≥ 此值 → 开仓 1 share
	ScoreEntry int `mapstructure:"score_entry"`
	// 复合评分 ≥ 此值 → 加仓（99 = 禁用，5s 数据下评分精度不足以支持加仓）
	ScoreAdd int `mapstructure:"score_add"`

	// ── 订单簿延迟风控 ──

	// 订单簿数据延迟超过此值（毫秒）则信号不可信，0 = 禁用
	MaxLatencyMs int64 `mapstructure:"max_latency_ms"`
}

// DefaultConfig 返回翻转信号检测的默认参数配置。
// 2026-08-12 网格搜索优化：胜率 50.9%, P&L +62.40, 盈亏比 2.3, 日均 37 信号。
func DefaultConfig() FlipConfig {
	return FlipConfig{
		TriggerThreshold:     0.7,    // PM 一侧 >0.7 触发
		AllowRetryCrossings:  true,   // 多穿越重试：每个上升沿都尝试评分
		MinPreSnaps:          5,      // 穿越前至少 5 个 snapshot
		MaxRemainingSec:      260,    // 窗口有效期上限：remaining_sec < 260s 的穿越才有效
		MinRemainingSec:      35,     // 窗口有效期下限：remaining_sec > 35s（5 ticks × 5s + 10s 执行缓冲）
		PathEffOscillating:    0.7,   // 收紧到 0.7（原 0.8 太宽松，噪声大）
		NoiseRatioOscillating: 1.5,   // 噪声比 >1.5 视为振荡
		FlipsOscillating:      1,     // 2026-08-12 sweep: 1 比 2 多 6 笔优质信号，胜率不变 P&L 更高
		HistWindowN:           18,    // 前 18 根 K 线（~1.5h）算平均振幅
		RangeExpThreshold:     0.5,   // 振幅 <0.5 → BTC 没动但 PM 0.7+ → 过度自信
		RangeExpMax:           1.5,   // 收紧到 1.5（原 2.0 太宽，真突破仍然通过了）
		ConfirmDelayTicks:     2,     // 从 5 改为 2（10s）：25s 时对面已反弹但入场价跑掉，FAK 无法成交
		ODHardFilter:          -999.0, // 禁用硬过滤，由评分权重处理
		OtherDeltaVStrong:     0.05,  // 对面涨 >0.05 → +3 分
		OtherDeltaStrong:      0.02,  // 对面涨 >0.02 → +2 分
		OtherDeltaWeak:        0.01,  // 对面涨 >0.01 → +1 分
		PathEffVetoMin:        0.4,   // 路径效率 <0.4 → 趋势极不明确，否决
		NoiseRatioVetoMax:     3.0,   // 噪声比 >3.0 → 价格极不稳定，否决
		BTCPosMax:             0.1,   // NO>0.7: BTC 涨 >0.1 倍振幅 → 背离 → +1
		BTCPosMin:             -0.1,  // YES>0.7: BTC 跌 >0.1 倍振幅 → 背离 → +1
		EntryCheapStrong:      0.20,  // 对侧 <0.20 → 极低价入场 → +1
		EntryCheapWeak:        0.25,  // 对侧 <0.25 → 低价入场 → +1（elif 不叠加）
		MaxEntryPrice:         0.3,   // 确认时刻对侧价 >0.30 → 信号无效（与 trading.max_price 一致，2026-08-12 6天数据扫描）
		WOtherD5VStrong:       3,     // 对面大涨权重
		WOtherD5Strong:        2,     // 对面中涨权重
		WOtherD5Weak:          1,     // 对面小涨权重
		WOscillating:          1,     // 降低到 1（原 2）：振荡信号胜率仅 31%，远低于趋势 45%
		WCheapEntryStr:        1,     // 极低价入场权重
		WCheapEntryWeak:       1,     // 低价入场权重
		WRangeExpansion:       2,     // 振幅偏小权重
		WBtcExtreme:           1,     // BTC 背离权重
		ScoreEntry:            5,     // ≥5 分开仓
		ScoreAdd:              99,    // 加仓禁用（5s 评分精度不够）
		MaxLatencyMs:          0,     // 0=禁用，实盘建议 300ms
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
	EntryPrice   float64   `json:"entry_price"`
	Shares       float64   `json:"shares"`       // 目标股数 = stake / entry_price（Engine 不设置，由调用方计算）
	RemainingSec int       `json:"remaining_sec"`

	// 执行结果（Trader 回填）
	ExecStatus   string    `json:"exec_status"`             // "pending" | "filled" | "failed"
	FilledAt     time.Time `json:"filled_at,omitempty"`     // 成交确认时间（UTC）
	FilledShares float64   `json:"filled_shares"`           // 实际成交股数（failed 时为 0）
	AvgFillPrice float64   `json:"avg_fill_price"`          // 实际成交均价（纸面 = entryPrice，实盘 = CLOB 真实价）
	SlippageBps  float64   `json:"slippage_bps,omitempty"`  // entry → fill 滑点（bp），正=不利

	// 特征详情（分析/调试用）
	PathEff        float64 `json:"path_eff"`
	NoiseRatio     float64 `json:"noise_ratio"`
	Flips          int     `json:"flips"`
	IsOscillating  bool    `json:"is_oscillating"`
	RangeExpansion float64 `json:"range_expansion"`
	BTCPosition    float64 `json:"btc_position"`
	BTCExtreme     bool    `json:"btc_extreme"`
	OtherDelta     float64 `json:"other_delta"`

	// 结算时填充
	Won        bool      `json:"won"`
	PnL        float64   `json:"pnl"`
	ResolvedAt time.Time `json:"resolved_at,omitempty"` // 结算时间（UTC）

	// 数据质量
	OrderBookLatency int64 `json:"order_book_latency,omitempty"` // 订单簿最大延迟（毫秒），诊断用
}

// ── 评分输入 ──

// ScoreParams 封装 ComputeFlipScore 的全部输入参数。
type ScoreParams struct {
	Side           string
	OtherDelta     float64
	IsOscillating  bool
	EntryPrice     float64
	RangeExpansion float64
	BTCPosition    float64
	BTCExtreme     bool  // pre-computed by engine (BTC diverges from PM direction)
	HistReady      bool
	OrderBookLatency int64 // 订单簿最大延迟（毫秒），用于延迟风控
	Cfg            FlipConfig
}
