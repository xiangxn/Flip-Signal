package flip

import (
	"fmt"
	"log"
)

// Executor 负责信号成交（纸面/实盘同接口）。本包只实现纸面路径；
// live 真实下单由 cmd/flip 直调 trading.LiveTrader（两阶段落盘/风控闸编排在
// main 侧, 见 cmd/flip/main.go handleObservation）——trading 依赖 SDK, flip
// 保持零外部依赖, 故 live 实现不入本包。
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

// NewExecutor 按模式返回执行器。live 语义已在 cmd/flip 分流（trading.LiveTrader,
// 不经本接口）——此处仅作防线: 收到 live 时告警并回退纸面（防止跳过 main 分流
// 的调用方在 live 意图下静默纸面）。
func NewExecutor(mode string) Executor {
	switch mode {
	case "live":
		log.Printf("⚠️ [Executor] live 须由 cmd/flip 构建 trading.LiveTrader——此处回退纸面执行")
		return PaperExecutor{}
	default:
		return PaperExecutor{}
	}
}
