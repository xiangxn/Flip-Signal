package dashboard

import (
	"time"

	"github.com/necklace/flip-signal/internal/tail"
)

// TailState 聚合「扫尾盘 ⑤」dashboard 的数据源引用。
// Recorder 与 Snapshotter（tail.Snapshotter, 由 cmd/tail 的 runtimeState 实现）均为
// 主循环已持有的组件，无独立生命周期。
//
// 与 FlipState 的形制一致: 页面上的每个数字都是**现算的只读视图**（tally 逐行分类、
// 逐日聚合、今日健康度读当日 tailstats 文件），判定/执行路径一律不读它。
// ⚠️ 2026-09-24 起本族不再有 Go 侧判决机器（tail.Judge/mt19937 已删, /api/judge
// 下线）——纸面判决走离线脚本 python/v4/23_tail_integrated.py。
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
