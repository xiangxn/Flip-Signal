package tail

import "math"

// ── 手续费（2026-10-03 决策 #35）──
//
// 本文件只放**官方公式的纯函数实现**；口径（谁付、何时付、如何入 P&L）分布在:
//   - 归属判定: internal/trading/live_executor.go 的 parseFill（买入的即时成交 = taker）
//     与 internal/tail/recorder.go 的 RecordExit（止损卖出 = taker）;
//   - 记账: Record.TakerShares/TakerPrice/Fee + recomputePnL 的扣减;
//   - 展示: internal/dashboard（fee 小卡 + 行内 P&L 提示）。

// Fee 按 Polymarket **官方公式**计算一笔成交的手续费（USDC）:
//
//	fee = C × feeRate × p × (1 − p)
//
// C = 成交股数, p = 成交价（USDC/股）, feeRate = 市场费率。官方口径
// （docs.polymarket.com/trading/fees.md，2026-10-03 核对）:
//   - **只有 taker 付费, maker 恒 0**（"Makers are never charged fees. Only takers
//     pay fees."）——故本函数只应作用于 taker 成交（买入的 GTC 即时撮合部分、
//     止损卖出的 FAK 成交）;
//   - Crypto 类（btc-updown-5m 属之）taker 费率 **0.07**、maker 0; 实盘市场详情
//     接口的 feeSchedule 实测 {rate: 0.07, takerOnly: true} 与文档一致;
//   - 金额**四舍五入到 5 位小数**、最小 0.00001 USDC（更小的按 0 记）;
//   - 费用对 50¢ 对称（p 与 1−p 同值）: p=0.30 与 p=0.70 的美元费用相同。
//
// 官方对照表（100 股, Crypto）: @0.50 → 1.75 / @0.30 → 1.47 / @0.05 → 0.3325 /
// @0.99 → 0.0693——TestFeeOfficialTable 逐格钉住。
//
// 入参不合法（费率 ≤0 / 股数 ≤0 / 价格越界）一律返回 0: 「不收费」是安全方向
// （宁可少记也不能给非成交记出费用）。费率 0 = 关闭计费（paper 行与关闭本功能的
// 场景走这里）。
func Fee(rate, shares, price float64) float64 {
	if rate <= 0 || shares <= 0 || price <= 0 || price >= 1 {
		return 0
	}
	f := shares * rate * price * (1 - price)
	return math.Round(f*1e5) / 1e5 // 官方口径: 5 位小数, 更小的舍到 0
}
