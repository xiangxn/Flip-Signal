package flip

import "fmt"

// ExecMode 区分成交执行模式（纸面/实盘同源，见 docs/paper_plan_2026-08-31.md §2.3）。
type ExecMode string

const (
	// ModePaper 纸面模式: 按信号成交口径（对侧 ask@+10s）模拟成交，不真下单。
	ModePaper ExecMode = "paper"
	// ModeLive 实盘模式: FAK 真实下单（后续阶段接入，当前未实现）。
	ModeLive ExecMode = "live"
)

// ExecResult 是一次信号执行的成交结果。
type ExecResult struct {
	Status       string  // "filled" | "failed"
	FilledShares float64 // 成交股数（failed 时为 0）
	AvgFillPrice float64 // 成交均价
}

// Executor 抽象成交执行：纸面模拟与实盘 FAK 同源，由 mode 配置切换。
// live 实现（FAK + CLOB 凭证）在纸面验证通过后接入（届时恢复实盘路径）。
type Executor interface {
	// Execute 执行一次信号（Cross.OK=true 时调用）。返回成交结果。
	Execute(c *Cross) (ExecResult, error)
}

// PaperExecutor 纸面成交执行器：直接按信号成交口径（fill = 对侧 ask@+10s）
// 全额成交，无滑点（与回测口径一致）。
type PaperExecutor struct {
	cfg Config
}

// NewPaperExecutor 构造纸面执行器。
func NewPaperExecutor(cfg Config) *PaperExecutor {
	return &PaperExecutor{cfg: cfg}
}

// Execute 按信号口径模拟成交: shares = stake/fill，成交价 = fill。
func (p *PaperExecutor) Execute(c *Cross) (ExecResult, error) {
	if c == nil || !c.OK {
		return ExecResult{Status: "failed"}, fmt.Errorf("信号未通过判定，无法执行")
	}
	if c.Fill <= 0 || c.Shares <= 0 {
		return ExecResult{Status: "failed"}, fmt.Errorf("成交价无效: fill=%v", c.Fill)
	}
	return ExecResult{
		Status:       "filled",
		FilledShares: c.Shares,
		AvgFillPrice: c.Fill,
	}, nil
}

// NewExecutor 按 mode 构造执行器。live 模式尚未实现（后续阶段接入）。
func NewExecutor(cfg Config, mode ExecMode) (Executor, error) {
	switch mode {
	case ModePaper:
		return NewPaperExecutor(cfg), nil
	case ModeLive:
		return nil, fmt.Errorf("live 模式未实现（纸面验证通过后接入 FAK 实盘路径）")
	default:
		return nil, fmt.Errorf("未知执行模式: %q", mode)
	}
}
