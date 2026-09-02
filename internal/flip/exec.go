package flip

import (
	"fmt"
	"log"
)

// Executor 负责信号成交（纸面/实盘同接口）。当前仅纸面实现；
// live（FAK）接口预留——纸面验证通过后从 eth 分支历史恢复实盘路径。
type Executor interface {
	Execute(obs *Observation) error
}

// PaperExecutor 纸面成交：观测已含决策时刻狗侧 ask（≤0.20），无真实订单。
// 只校验 fill > 0 ——v4 回测无任何 fill 边界，v3 的 [0.05,0.95] 检查整体删除
// （执行层不得吞掉合法信号）。
type PaperExecutor struct{}

// Execute 校验并记账一笔纸面信号。
func (PaperExecutor) Execute(obs *Observation) error {
	if obs == nil || !(obs.Fill > 0) {
		return fmt.Errorf("PaperExecutor: fill 无效（obs=%+v）", obs)
	}
	return nil
}

// NewExecutor 按模式返回执行器；live 未实现时回退纸面并告警。
func NewExecutor(mode string) Executor {
	switch mode {
	case "live":
		log.Printf("⚠️ live 模式尚未实现（FAK 接口预留），回退纸面执行")
		return PaperExecutor{}
	default:
		return PaperExecutor{}
	}
}
