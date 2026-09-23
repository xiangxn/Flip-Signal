package dashboard

import (
	"time"

	"github.com/necklace/flip-signal/internal/flip"
)

// FlipState 聚合 flip dashboard 的数据源引用。
// Recorder 与 Snapshotter（flip.Snapshotter, 由 cmd/flip 的 runtimeState 实现）均为
// 主循环已持有的组件，无独立生命周期。
//
// ⚠️ 与 TailState 是两个独立类型（各有各的 handler 方法, 方法名同名）: 两族的
// Recorder/Snapshot/Config 类型互不相同, 硬套一个共用基类只会让两边都退化成
// interface{}。共用的是**纯函数**部分（common.go），不是状态载体。
type FlipState struct {
	recorder  *flip.Recorder
	snapshot  flip.Snapshotter
	cfg       flip.Config
	mode      string
	limits    SourceLimits
	startedAt time.Time
	nowFn     func() time.Time // 可注入时钟（测试用），默认 time.Now
}

// NewFlipState 构造 flip dashboard 状态载体（limits 见 SourceLimits 注释）。
func NewFlipState(recorder *flip.Recorder, snapshot flip.Snapshotter, cfg flip.Config, mode string, limits SourceLimits) *FlipState {
	return &FlipState{
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
func (s *FlipState) Recorder() *flip.Recorder { return s.recorder }
