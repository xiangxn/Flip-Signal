package tail

import (
	"time"

	"github.com/necklace/flip-signal/internal/flip"
)

// LiveSnapshot 是扫尾盘引擎运行时状态的只读快照（由 cmd/tail main 的 runtimeState
// 采集, dashboard handler 无锁读——同 flip.LiveSnapshot 的模式）。
//
// 与 flip 的语义差异（本族是尾盘买热门侧, 没有「触底/浅洞」概念）:
//   - 窗口读数换成**热门侧**（ask 高的一侧）的 side/hotAsk/dev/sd;
//   - 进度用**两个一次性闩锁**（帧 rem≤FrameRem / 决策快照 rem≤RemStart）而非触底观测;
//   - 锚可见性字段与 tailstats 同口径（AnchorExact/AnchorSrc/AnchorArrivedMs）。
//
// 窗口剩余秒不在这里: 与 flip 同款, 由 dashboard 用 EventStart + 窗口长现算
// （轮询间隔 5s, 快照里的 rem 会明显滞后）。
type LiveSnapshot struct {
	Mode        string    // 成交模式: paper/live（构造后不变, live 缺凭证降级为 paper）
	StartedAt   time.Time // 进程启动时刻（构造后不变）
	ConditionID string    // 当前窗口 conditionId
	Slug        string    // 当前窗口 slug
	EventStart  int64     // 当前窗口起点（unix 秒）
	EngineState string    // 状态机标签: Watching/Done

	// 锚与 σ（决策 #15: 锚 = 边界那一秒的 TWAP 推送）。本族时序上「锚在产出任何行
	// 之前已定局」——取锚通道边界 +20s 结束, 最早的帧在 +150s。
	Anchor          float64 // ≤0 = 尚未取到（本窗一行不产出）
	HistBps         float64 // 本窗生效 σ（bps; ≤0 = 不可用）
	AnchorExact     bool    // 是否已精确命中边界那一秒的推送
	AnchorSrc       string  // 锚来源（官方通道休眠时恒 "stream"）
	AnchorArrivedMs int64   // 该推送的本地到达时刻距边界毫秒（发布延迟的无偏观测）

	// 当前窗口最新盘口（0 = 尚无有效盘口）
	YesBid    float64
	YesAsk    float64
	NoBid     float64
	NoAsk     float64
	BookLatMs int64
	SpotAgeMs int64 // Binance spot 距本地接收毫秒（−1 = 尚无推送）
	TwapAgeMs int64 // TWAP-60 距上次推送毫秒

	// 数据源现值（0 = 缺失/陈旧）
	SpotPrice float64 // Binance BTCUSDT 最新价
	TwapPrice float64 // Chainlink TWAP-60 流值

	// 尾盘读数（现算, 与落盘行同源——见 decide.go 的派生量纯函数）
	HotSide string  // 热门侧: yes | no（平局取 yes）
	HotAsk  float64 // 热门侧 ask = 成交价口径
	Dev     float64 // 位移（**美元**, 正 = 朝热门侧方向）
	Sd      float64 // 该窗 1σ 折美元（0 = σ 不可用）

	// 本窗三个闩锁（dashboard 显示「帧已落 / 快照已落 / 监听行已落 / 锚已冻结」）
	FrameSent    bool
	SnapSent     bool
	ScanSent     bool // 监听口径对账行已产出（或无需产出——snap 已达标）
	AnchorFrozen bool

	// Stats 为本窗 tick 健康度（引擎计数器, 窗口间/未开始为 nil）
	Stats *WindowStats

	// Risk 为日亏熔断摘要（两模式都填; paper 的 Enforced=false）——复用 flip 的类型
	// （普通结构体 + json tag 已定, 前端两族共用一套渲染）
	Risk *flip.RiskSummary

	// Live 为 live 执行摘要（Mode=="live" 时填充; paper 恒 nil）
	Live *flip.LiveExec
}

// Snapshotter 由 cmd/tail main 的 runtimeState 实现，返回当前运行快照
// （dashboard handler 每 5s 轮询）。
type Snapshotter interface {
	Snapshot() LiveSnapshot
}
