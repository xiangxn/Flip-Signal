package flip

import "time"

// LiveSnapshot 是运行时状态的只读快照（cmd/flip main 采集，dashboard handler 无锁读）。
// 数据来源: 引擎状态机 + 当前窗口盘口/现货 + 运行元信息。
type LiveSnapshot struct {
	Mode        string    // 成交模式: paper/live
	StartedAt   time.Time // 进程启动时刻
	ConditionID string    // 当前窗口 conditionId
	Slug        string    // 当前窗口 slug
	EventStart  int64     // 当前窗口起点（unix 秒）
	EngineState string    // 状态机标签: Watching/Done（触底观测语义）

	// 当前窗口最新盘口（0 = 尚无有效盘口）
	UpBid     float64
	UpAsk     float64
	DownBid   float64
	DownAsk   float64
	BookLatMs int64 // 盘口传输延迟（毫秒）
	TwapAgeMs int64 // TWAP-60 距上次推送毫秒数（诊断）

	// Binance spot（浅洞腿参考价）：SpotPrice=0 且 SpotAgeMs=−1 = 尚无推送；
	// 有推送时 SpotAgeMs 为本地接收龄（毫秒，>2000 = 引擎已判现货缺失）
	SpotPrice float64
	SpotAgeMs int64

	// Live 为 live 执行摘要（Mode=="live" 时填充; paper 恒 nil）
	Live *LiveExec
}

// LiveExec 是 live 执行的只读摘要（Mode=="live" 时由 ExecState.LiveSummary 填充;
// paper 恒 nil）。今日口径 = UTC 日（与记录文件切分、HandleObservation 熔断现算
// 同口径）。与 trading.LiveExecutor（真实下单实现）语义区分: 本类型只是 Dashboard
// 的 live 状态摘要数据。
type LiveExec struct {
	TodayFilled  int     `json:"today_filled"`   // 今日实际成交笔数（filled/partial）
	TodayPnl     float64 `json:"today_pnl"`      // 今日已结算 P&L（USDC; 熔断判定输入）
	MaxDailyLoss float64 `json:"max_daily_loss"` // 熔断线（--max-daily-loss）
	BreakerOpen  bool    `json:"breaker_open"`   // 熔断未触发: 今日 P&L > MaxDailyLoss（可下单）
	Reconciling  int     `json:"reconciling"`    // 待人工核对执行行（submitting/未知结果, 重启扫描语义）
}

// Snapshotter 由 cmd/flip main 的 runtimeState 实现，返回当前运行快照
// （dashboard handler 每 5s 轮询）。
type Snapshotter interface {
	Snapshot() LiveSnapshot
}
