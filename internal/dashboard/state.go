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

// State 聚合 dashboard 的数据源引用。
// Recorder 与 Snapshotter（flip.Snapshotter, 由 main 的 runtimeState 实现）均为
// 主循环已持有的组件，无独立生命周期。
type State struct {
	recorder  *flip.Recorder
	snapshot  flip.Snapshotter
	cfg       flip.Config
	mode      string
	startedAt time.Time
	nowFn     func() time.Time // 可注入时钟（测试用），默认 time.Now
}

// NewState 构造 dashboard 状态载体。
func NewState(recorder *flip.Recorder, snapshot flip.Snapshotter, cfg flip.Config, mode string) *State {
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
