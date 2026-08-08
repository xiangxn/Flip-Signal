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
// 默认值与 backtest_flip_config.py 完全一致（5s 数据标定）。
type FlipConfig struct {
	// ── Layer 0: 前置条件 (§2.1) ──
	TriggerThreshold     float64 `mapstructure:"trigger_threshold"`      // PM price > this triggers detection (0.7)
	AllowRetryCrossings  bool    `mapstructure:"allow_retry_crossings"`  // Multi-crossing: retry on every rising edge until a bet is placed (true)
	MinPreSnaps          int     `mapstructure:"min_pre_snaps"`          // Minimum snapshots before crossing (5)
	MaxRemainingSec      int     `mapstructure:"max_remaining_sec"`      // Only crossings with remaining_sec < this are valid (260, window too early = insufficient BTC path)

	// ── 振荡检测（三条件必须同时满足）—— Formula A ──
	PathEffOscillating    float64 `mapstructure:"path_eff_oscillating"`     // path_eff ≤ this → candidate (0.8, was 0.5)
	NoiseRatioOscillating float64 `mapstructure:"noise_ratio_oscillating"` // noise_ratio > this → candidate (1.5, was 5.0)
	FlipsOscillating      int     `mapstructure:"flips_oscillating"`       // flips > this → candidate (1, was 2)

	// ── 振幅扩张（tick 无关，基于 hist_avg_range）──
	HistWindowN       int     `mapstructure:"hist_window_n"`        // Historical kline window size (18)
	RangeExpThreshold float64 `mapstructure:"range_exp_threshold"`  // < this → BTC barely moved, PM overconfident (0.5)
	RangeExpMax       float64 `mapstructure:"range_exp_max"`        // ≥ this → F0 veto, real breakout (2.0)

	// ── 对面确认 (§2.7) —— Formula A 三级评分 ──
	ConfirmDelayTicks  int     `mapstructure:"confirm_delay_ticks"`  // Ticks to wait for confirmation (1 tick = 5s)
	ODHardFilter       float64 `mapstructure:"od_hard_filter"`       // other_delta hard lower bound, -999 = disabled (Formula A: scoring handles it)
	OtherDeltaVStrong  float64 `mapstructure:"other_delta_vstrong"`  // > this → +3 points (0.05, new top tier)
	OtherDeltaStrong   float64 `mapstructure:"other_delta_strong"`   // > this → +2 points (0.02, was 0.03)
	OtherDeltaWeak     float64 `mapstructure:"other_delta_weak"`     // > this → +1 points (0.01, was +2)

	// ── BTC 位置 (§2.8) —— Formula A 放宽 ──
	BTCPosMax float64 `mapstructure:"btc_pos_max"` // NO>0.7: btc_pos > this → BTC diverges from PM → +1 (0.1)
	BTCPosMin float64 `mapstructure:"btc_pos_min"` // YES>0.7: btc_pos < this → BTC diverges from PM → +1 (-0.1)

	// ── T=0 quality vetoes（硬编码→可配置）──
	PathEffVetoMin    float64 `mapstructure:"path_eff_veto_min"`    // path_eff < this → veto at crossing (0.4, trend too unclear)
	NoiseRatioVetoMax float64 `mapstructure:"noise_ratio_veto_max"` // noise_ratio > this → veto at crossing (3.0, price too unstable)

	// ── 入场价格 (§2.9) —— Formula A 重新激活 ──
	EntryCheapStrong float64 `mapstructure:"entry_cheap_strong"` // < this → +1 point  (0.20)
	EntryCheapWeak   float64 `mapstructure:"entry_cheap_weak"`   // < this → +1 point  (0.25, elif — no stacking)

	// ── 评分权重 (§2.10) —— Formula A ──
	WOtherD5VStrong int `mapstructure:"w_other_delta_vstrong"` // Opposite huge move  (3, new)
	WOtherD5Strong  int `mapstructure:"w_other_delta_strong"`  // Opposite big move    (2, was 4)
	WOtherD5Weak    int `mapstructure:"w_other_delta_weak"`    // Opposite small move  (1, was 2)
	WOscillating    int `mapstructure:"w_oscillating"`         // Oscillation bonus    (2, was 1)
	WCheapEntryStr  int `mapstructure:"w_cheap_entry_strong"`  // Very cheap entry     (1, was 2, was disabled)
	WCheapEntryWeak int `mapstructure:"w_cheap_entry_weak"`    // Cheap entry          (1)
	WRangeExpansion int `mapstructure:"w_range_expansion"`     // Range too small      (2)
	WBtcExtreme     int `mapstructure:"w_btc_extreme"`         // BTC extreme pos      (1)

	// ── 入场阈值 (§2.10) ──
	ScoreEntry int `mapstructure:"score_entry"` // ≥ this → open 1 share (5)
	ScoreAdd   int `mapstructure:"score_add"`   // ≥ this → add 2 shares (99 = disabled)
}

// DefaultConfig 返回与 backtest_flip_config.py Formula A 一致的配置。
// 回测结果: 34 signals, 52.9% WR, +10.09 P&L, PF=2.7（lab 数据）。
func DefaultConfig() FlipConfig {
	return FlipConfig{
		TriggerThreshold:     0.7,
		AllowRetryCrossings:  true, // Multi-crossing: retry on every rising edge
		MinPreSnaps:          5,
		MaxRemainingSec:      260, // §2.1: crossings before this are invalid (too early, BTC path too short)
		PathEffOscillating:    0.8,  // Formula A: relaxed from 0.5
		NoiseRatioOscillating: 1.5,  // Formula A: relaxed from 5.0 (never fired)
		FlipsOscillating:      1,    // Formula A: relaxed from 2
		HistWindowN:           18,
		RangeExpThreshold:     0.5,
		RangeExpMax:           2.0,
		ConfirmDelayTicks:     1,     // 1 tick = 5s
		ODHardFilter:          -999.0, // Formula A: disabled, scoring handles it
		OtherDeltaVStrong:     0.05,   // Formula A: new top tier (>0.05 → +3)
		OtherDeltaStrong:      0.02,   // Formula A: was 0.03 +4
		OtherDeltaWeak:        0.01,   // Formula A: was +2
		PathEffVetoMin:        0.4,  // path_eff below this → trend too unclear, veto
		NoiseRatioVetoMax:     3.0,  // noise_ratio above this → price too unstable, veto
		BTCPosMax:             0.1,  // Formula A: widened from 0.5
		BTCPosMin:             -0.1, // Formula A: widened from -0.5
		EntryCheapStrong:      0.20, // Formula A: re-activated (was 0=disabled)
		EntryCheapWeak:        0.25,
		WOtherD5VStrong:       3,      // Formula A: new weight
		WOtherD5Strong:        2,      // Formula A: was 4
		WOtherD5Weak:          1,      // Formula A: was 2
		WOscillating:          2,      // Formula A: was 1
		WCheapEntryStr:        1,      // Formula A: was 2 (was disabled)
		WCheapEntryWeak:       1,
		WRangeExpansion:       2,
		WBtcExtreme:           1,
		ScoreEntry:            5,
		ScoreAdd:              99, // disabled (5s data scoring not fine-grained enough)
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
	ExecStatus   string  `json:"exec_status"`    // "pending" | "filled" | "failed"
	FilledShares float64 `json:"filled_shares"`  // 实际成交股数（failed 时为 0）
	AvgFillPrice float64 `json:"avg_fill_price"` // 实际成交均价（纸面 = entryPrice，实盘 = CLOB 真实价）

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
	Won bool    `json:"won"`
	PnL float64 `json:"pnl"`
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
	BTCExtreme     bool // pre-computed by engine (BTC diverges from PM direction)
	HistReady      bool
	Cfg            FlipConfig
}
