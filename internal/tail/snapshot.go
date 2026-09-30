package tail

import (
	"time"

	"github.com/necklace/flip-signal/internal/flip"
)

// LiveSnapshot 是扫尾盘引擎运行时状态的只读快照（由 cmd/tail main 的 runtimeState
// 采集, dashboard handler 无锁读——同 flip.LiveSnapshot 的模式）。
//
// 与 flip 的语义差异（本族是尾盘买热门侧, 没有「触底/浅洞」概念）:
//   - 窗口读数换成**热门侧**（有效价高的一侧）的 side/hotAsk/dev/sd;
//   - 进度用**三段链的闩锁**（见 Latches）而非触底观测;
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
	HotSide string  // 热门侧: yes | no（有效价高的一侧, 平局取 yes）
	HotAsk  float64 // 热门侧**有效价**（ask 优先、bid 兜底）= 成交价口径
	HotSrc  string  // 有效价来源: ask | bid（bid = 该侧卖单被撤空, 只能按买价挂单）
	Dev     float64 // 位移（**美元**, 正 = 朝热门侧方向）
	Sd      float64 // 该窗 1σ 折美元（0 = σ 不可用）

	// 本窗三段链的进度（dashboard 显示「T150 已判 / T60 已判 / 监听中 / 已出信号 / 锚已冻结」）
	T150Sent     bool // 第一段（rem≤t150_rem 判 ⑤）已做
	T60Sent      bool // 第二段（rem≤t60_rem 判 ⑤）已做
	Listening    bool // 已进入监听段（此后每秒判 ②）
	SignalSent   bool // 本窗已出信号（= 已下单, 整窗只一单）
	AnchorFrozen bool

	// Stats 为本窗 tick 健康度（引擎计数器, 窗口间/未开始为 nil）
	Stats *WindowStats

	// Risk 为日亏熔断摘要（两模式都填; paper 的 Enforced=false）——复用 flip 的类型
	// （普通结构体 + json tag 已定, 前端两族共用一套渲染）
	Risk *flip.RiskSummary

	// Live 为 live 执行摘要（Mode=="live" 时填充; paper 恒 nil）
	Live *flip.LiveExec
}

// CurvePoint 是当前窗口动态曲线上的一个采样点（读数一律**美元**；0 = 该 tick
// 无该读数，前端断线不画）。
//
// 采样由 cmd/tail 主循环每 tick 追加一条（dashboard 只读，不参与任何判定）:
//   - Anchor = 边界那一秒的 TWAP 推送（取锚通道命中前恒 0）——本族的锚在最早的判定行
//     之前早已定局, 所以曲线上它是一条从边界后 ~2s 起拉平的水平参考线;
//   - Twap = Chainlink TWAP-60 流值（0 = 尚无推送）;
//   - Spot = Binance 现货最新价（0 = 无推送/超龄——与引擎判现货缺失同口径,
//     不把陈旧价画进曲线）;
//   - Tie = **结算线**（只在 rem ≤ 60 有值, 其余恒 0）: 从此刻起现货保持在该水平、
//     到闭市一直不动, 闭市那一刻的 TWAP-60 恰好等于 anchor 的**临界价**——
//     现货在它之上 ⇒ 结算 Up, 之下 ⇒ Down。推导与口径见 cmd/tail/curve.go 的
//     RequiredPrice（它是**派生量**不是读数: 前端画成虚线阈值, 与三条实测线区分）。
//   - Extrap = **速度外推临界价**（只在 rem ∈ [60, 150] 有值, 其余恒 0）: 假设现货
//     从此刻起保持当前速度线性运行, 进入最后 60s 那一刻的价格须达到此值, 闭市 TWAP-60
//     才刚好等于 anchor（用户口径, 推导见 docs/l.md 与 cmd/tail/curve.go 的 ExtrapPrice）。
//     与 Tie 合起来拼满整窗、假设正好相反（运动继续 vs 运动停住）; 它恒是 Spot 与 anchor
//     的凸组合 ⇒ 必然落在实测线中间, 不像 Tie 那样需要量程照顾。
type CurvePoint struct {
	Ts     int64   `json:"ts"`  // 采样时刻（unix 毫秒）
	Rem    int     `json:"rem"` // 窗口剩余秒（ts 缺失时的兜底横坐标）
	Anchor float64 `json:"anchor"`
	Twap   float64 `json:"twap"`
	Spot   float64 `json:"spot"`
	Tie    float64 `json:"tie"`
	Extrap float64 `json:"extrap"`
}

// Curve 是 /api/curve 的响应体: 当前窗口的逐 tick 采样序列（前端画三线同轴曲线）。
//
// 换窗语义（前端据此决定擦不擦画布）: EventStart 变化即代表曲线换装到新窗口——
// **新窗还没有任何采样时返回的是上一窗的 EventStart 与 Points**（不是空数组），
// 前端保留上一窗的曲线不擦, 直到新窗第一个采样到来才整体重画。
type Curve struct {
	EventStart int64        `json:"event_start"` // 采样所属窗口起点（unix 秒; 0 = 尚无窗口）
	Slug       string       `json:"slug,omitempty"`
	WindowSec  int          `json:"window_sec"` // 窗口长度（秒）: 前端 x 轴量程
	Points     []CurvePoint `json:"points"`
}

// Snapshotter 由 cmd/tail main 的 runtimeState 实现，返回当前运行快照与动态曲线
// （dashboard handler 分别按 5s / 1s 轮询——口径说明见各自的类型注释）。
type Snapshotter interface {
	Snapshot() LiveSnapshot
	Curve() Curve
}
