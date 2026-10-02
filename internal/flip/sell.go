package flip

import "fmt"

// SellExecutor 负责**持仓卖出**（止损出场）——与 Executor（买入开仓）并列的第二个
// 执行契约。当前唯一使用方是扫尾盘（internal/tail/exec_state.go 的 StopSell）;
// 拆成独立接口而不是往 Executor 上加方法，理由是两者的编排形态不同:
//
//   - 买入：一笔信号 = 一个新仓位，有 submitting/resting 两阶段 + FillTracker
//     定稿（决策 #16）;
//   - 卖出：一个**已在手上**的仓位，用 FAK 限价单按快照 bid 拍一次——
//     成交多少算多少（partial 累计），吃不到（unfilled）下一 tick 重试，
//     不在簿上留单 ⇒ **无 resting 态、无 tracker、单次 POST 即终态**。
//
// 契约与 Executor 一致: 永远不返回 error，失败与终态折叠进 SellResult;
// paper/live 实现可互换（PaperSellExecutor / trading.LiveSellExecutor）。
//
// 实现: PaperSellExecutor（本包, 零外部依赖）+ trading.LiveSellExecutor（SDK 依赖层,
// 单向依赖本包——接口留在此处的理由同 Executor）。
type SellExecutor interface {
	Sell(tokenID string, shares float64, limitPrice float64) *SellResult
}

// SellResult 一笔卖出委托的终态。Shares/Price 是**实际成交**部分
// （Status=filled/partial 时有效; Price 为成交均价——CLOB 单笔撮合价，本引擎按
// 快照 bid 一个限价，实际可能更优）。
//
// Status 复用买入那套 ExecStatus 常量（filled/partial/unfilled/rejected），
// 语义逐条对应: unfilled = 本次没吃到、调用方下一 tick 重试（**不落盘不冻结**）;
// rejected/unknown = 冻结点位、写一次 exit_note 等人工核对。
type SellResult struct {
	Status string  // ExecStatus* 常量（filled/partial/unfilled/rejected）
	Shares float64 // 已卖出股数
	Price  float64 // 成交均价（USDC/股）
	Note   string
}

// PaperSellExecutor 纸面卖出（零外部依赖, 与 PaperExecutor 并列）:
// 按「快照 bid 即可成交」记账（与实盘证据分析
// docs/tail_stoploss_live_2026-10-03.md 的假设逐位同口径——那边 25/25 触发秒都有
// 对手方、bid5 最小 33 股 vs 最大持仓 12.5 股，纸面按全额成交模拟）。
//
// 只校验 shares > 0 且 limitPrice > 0（非法 → rejected + note）——
// 与 PaperExecutor 同一原则: 执行层不发明边界，只拒绝算不出来的输入。
type PaperSellExecutor struct{}

func (PaperSellExecutor) Sell(tokenID string, shares float64, limitPrice float64) *SellResult {
	if !(shares > 0) || !(limitPrice > 0) {
		return &SellResult{
			Status: ExecStatusRejected,
			Note:   fmt.Sprintf("PaperSellExecutor: 参数无效（shares=%.4f limit=%.4f）", shares, limitPrice),
		}
	}
	return &SellResult{
		Status: ExecStatusFilled,
		Shares: shares,
		Price:  limitPrice,
	}
}

// 编译期断言: 纸面实现满足卖出契约。
var _ SellExecutor = PaperSellExecutor{}
