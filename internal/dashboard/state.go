// Package dashboard 提供「狗@0.2」触底策略的 Web 监控界面。
//
// 单页前端（go:embed static/，手机浏览器兼容），4 个 JSON API:
//
//	/api/state         运行状态（引擎/窗口/盘口/现货/统计汇总）
//	/api/observations  触底观测列表（成功+失败，诊断信号频率）
//	/api/signals       信号列表（ok=true，含 P&L）
//	/api/config        当前策略配置
package dashboard

import (
	"time"

	"github.com/necklace/flip-signal/internal/flip"
)

// LiveSnapshot 是运行时状态的只读快照（main 包采集，handler 无锁读）。
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
}

// Snapshotter 由 main 包实现，返回当前运行快照（handler 每 5s 轮询）。
type Snapshotter interface {
	Snapshot() LiveSnapshot
}

// State 聚合 dashboard 的数据源引用。
// Recorder 与 Snapshotter 均为主循环已持有的组件，无独立生命周期。
type State struct {
	recorder  *flip.Recorder
	snapshot  Snapshotter
	cfg       flip.Config
	mode      string
	startedAt time.Time
	nowFn     func() time.Time // 可注入时钟（测试用），默认 time.Now
}

// NewState 构造 dashboard 状态载体。
func NewState(recorder *flip.Recorder, snapshot Snapshotter, cfg flip.Config, mode string) *State {
	return &State{
		recorder:  recorder,
		snapshot:  snapshot,
		cfg:       cfg,
		mode:      mode,
		startedAt: time.Now(),
		nowFn:     time.Now,
	}
}

// Recorder 返回信号记录器（handlers 查询用）。
func (s *State) Recorder() *flip.Recorder { return s.recorder }
