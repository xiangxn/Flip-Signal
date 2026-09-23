package dashboard

import (
	"time"

	"github.com/necklace/flip-signal/internal/tail"
)

// TailState 聚合「扫尾盘 ⑤」dashboard 的数据源引用。
// Recorder 与 Snapshotter（tail.Snapshotter, 由 cmd/tail 的 runtimeState 实现）均为
// 主循环已持有的组件，无独立生命周期。
//
// 与 FlipState 的形制差异: 本族的判决读数是**算出来的**（tail.Judge 纯函数）,
// 不在记录器里——它要吃全量行 + 配置（五格 + T=150 对照格 + bootstrap 区间）。
// 故 handler 每次请求现算（全量行数量级 ~万, 两族都是毫秒级）。
type TailState struct {
	recorder  *tail.Recorder
	snapshot  tail.Snapshotter
	cfg       tail.Config
	mode      string
	limits    SourceLimits
	startedAt time.Time
	nowFn     func() time.Time // 可注入时钟（测试用），默认 time.Now
}

// NewTailState 构造 tail dashboard 状态载体（limits 见 SourceLimits 注释）。
func NewTailState(recorder *tail.Recorder, snapshot tail.Snapshotter, cfg tail.Config, mode string, limits SourceLimits) *TailState {
	return &TailState{
		recorder:  recorder,
		snapshot:  snapshot,
		cfg:       cfg,
		mode:      mode,
		limits:    limits,
		startedAt: time.Now(),
		nowFn:     time.Now,
	}
}

// Recorder 返回记录器（handlers 查询用）。
func (s *TailState) Recorder() *tail.Recorder { return s.recorder }
