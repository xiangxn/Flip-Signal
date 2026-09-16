// Package dashboard 提供「狗@0.2」触底策略的 Web 监控界面。
//
// 单页前端（go:embed static/，手机浏览器兼容），5 个 JSON API:
//
//	/api/state         运行状态（引擎/窗口/盘口/现货/统计汇总）
//	/api/observations  触底观测列表（成功+失败，诊断信号频率；?page=&limit= 分页）
//	/api/signals       信号列表（ok=true，含 P&L；?page=&limit= 分页）
//	/api/daily         逐日盈利明细（UTC 日，弹窗表）
//	/api/config        当前策略配置
//
// 运行时快照类型（flip.LiveSnapshot/LiveExec/Snapshotter）定义在 internal/flip
// （引擎域数据）——本包只做 HTTP 展示，不自行定义状态类型。
package dashboard

import (
	"time"

	"github.com/necklace/flip-signal/internal/flip"
)

// SourceLimits 是三个数据源新鲜度阈值（/api/state 的 limits 下发，前端按此标红）。
// 口径与引擎/采样层实际生效值同源（cmd/flip 的 3 个 flag）——前端不得硬编码阈值，
// 否则调 flag 后页面颜色语义会与实际判定脱节。
type SourceLimits struct {
	BookLatMs int64 `json:"book_lat_ms"` // 盘口延迟闸（引擎判无效）
	SpotAgeMs int64 `json:"spot_age_ms"` // Binance spot 新鲜度闸（采样层判缺失）
	TwapAgeMs int64 `json:"twap_age_ms"` // TWAP 新鲜度闸（anchor/close 守卫）
}

// State 聚合 dashboard 的数据源引用。
// Recorder 与 Snapshotter（flip.Snapshotter, 由 main 的 runtimeState 实现）均为
// 主循环已持有的组件，无独立生命周期。
type State struct {
	recorder  *flip.Recorder
	snapshot  flip.Snapshotter
	cfg       flip.Config
	mode      string
	limits    SourceLimits
	startedAt time.Time
	nowFn     func() time.Time // 可注入时钟（测试用），默认 time.Now
}

// NewState 构造 dashboard 状态载体（limits 见 SourceLimits 注释）。
func NewState(recorder *flip.Recorder, snapshot flip.Snapshotter, cfg flip.Config, mode string, limits SourceLimits) *State {
	return &State{
		recorder:  recorder,
		snapshot:  snapshot,
		cfg:       cfg,
		mode:      mode,
		limits:    limits,
		startedAt: time.Now(),
		nowFn:     time.Now,
	}
}

// Recorder 返回信号记录器（handlers 查询用）。
func (s *State) Recorder() *flip.Recorder { return s.recorder }
