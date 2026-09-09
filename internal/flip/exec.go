package flip

import (
	"fmt"
	"log"
)

// Executor 负责信号成交（纸面/实盘同一契约, 单点调用——cmd/flip 只经本接口
// 调 Execute, paper/live 具体实现可互换）。契约要点:
//
//   - 永远不返回 error——失败与终态一律折叠进 ExecResult
//     （live: filled/partial/unfilled/rejected + note; paper: 校验失败 rejected,
//     其余恒 filled 模拟全额成交）;
//   - 调用点同形: Execute(obs, tokenID, stake)——tokenID 为狗侧 token（live
//     下单用; paper 无真实订单, 忽略）, stake 为每笔投入;
//   - 编排差异（live 独有的风控闸/两阶段落盘/submitting 行）在调用方
//     （cmd/flip handleObservation）两侧处理——本接口不吞也不造编排,
//     纸面落盘行（RecordObservation）不经 ExecResult, schema 与回测 1:1 不变。
//
// 实现: PaperExecutor（本包, 零外部依赖）+ trading.LiveExecutor（SDK 依赖层,
// 单向依赖本包——接口须留在此处, 移出会造成 flip↔实现方循环依赖）。
type Executor interface {
	Execute(obs *Observation, tokenID string, stake float64) *ExecResult
}

// PaperExecutor 纸面成交: 观测已含决策时刻狗侧 ask（≤0.20），无真实订单。
// 只校验 fill > 0（nil/非法 → rejected + note）——v4 回测无任何 fill 边界，
// v3 的 [0.05,0.95] 检查整体删除（执行层不得吞掉合法信号）。
type PaperExecutor struct{}

// Execute 校验并模拟一笔纸面信号（tokenID 无意义, 忽略）。
// 恒返回 filled 全额模拟: FillPrice=fill、Shares=obs.Shares（引擎按
// stake/fill 算好的目标股数）、Cost=stake（仅作 ExecResult 基准——纸面
// 落盘行由 RecordObservation 直接写入, 不含 Cost, 结算走 Stake 兜底）。
func (PaperExecutor) Execute(obs *Observation, tokenID string, stake float64) *ExecResult {
	if obs == nil || !(obs.Fill > 0) {
		return &ExecResult{
			Status: ExecStatusRejected,
			Note:   fmt.Sprintf("PaperExecutor: fill 无效（obs=%+v）", obs),
		}
	}
	return &ExecResult{
		Status:    ExecStatusFilled,
		FillPrice: obs.Fill,
		Shares:    obs.Shares,
		Cost:      stake,
	}
}

// NewExecutor 按模式返回纸面执行器。live 语义在 cmd/flip 分流段把同一
// Executor 字段换成 trading.LiveExecutor（两者同满足本接口）——此处收到 live
// 仅作防线告警: 防止跳过 main 分流的调用方在 live 意图下静默纸面。
func NewExecutor(mode string) Executor {
	switch mode {
	case "live":
		log.Printf("⚠️ [Executor] live 须由 cmd/flip 构建 trading.LiveExecutor——此处回退纸面执行")
		return PaperExecutor{}
	default:
		return PaperExecutor{}
	}
}

// 编译期断言: 纸面实现满足执行契约。
var _ Executor = PaperExecutor{}
