package tail

import "github.com/necklace/flip-signal/internal/flip"

// ── 持仓止损（2026-10-03 决策 #34, 用户决定「马上执行添加止损」）──
//
// 与持仓监察（hold.go）的关系: 监察是**只记录**的旁路（问题「真要出场那一刻有没有
// 对手方」的取证工具）; 本文件是**真下单**的止损腿——持仓期间持仓侧 bid 跌破阈值
// 就按当时快照 bid 挂 FAK 卖单。两者共用同一批读数（持仓侧 bid）、门控刻意**不同**
// （见 StopArmed）。
//
// 落地依据 = 实盘不是回测（用户 2026-10-03 判定「实盘验证更准」）:
//   - 实盘（data/tail-live 09-25~10-02, 8 天, 10U/注）纯价格腿 bid<0.30 = 25 次触发,
//     23/23 输单全中 + 2/445 误杀, Δ +45.51U, 日级配对 95% [+22.62,+70.44]; 25/25
//     触发秒有对手方（bid5 最小 33 股 > 最大持仓 12.5 股）。
//   - 同一形态 14 天回测 ≈ 中性（+0.83U [−24.84,+19.21]）——触发人群胜率
//     实盘 8.0% vs 回测 23.2%: 实盘挂单成交被逆向选择（「0.99 近半不成交」的同一件事）。
//   完整证据（脚本 python/v4/44_tail_stoploss_live.py）与预先写死的前向判据:
//   docs/tail_stoploss_live_2026-10-03.md。

// MinSellShares CLOB 最小下单股数（买与卖同一约束）。止损卖出**只在 live**用
// 它判「剩余不足最小单量」——paper 模式不套用（理由见 ExecState.StopSell）。
const MinSellShares = 5.0

// StopTrigger 判断一条持仓侧买价是否触发止损（**纯函数**, 判定与执行两侧的唯一口径）。
//
// 三条口径（写死在这里, 配置只挪阈值）:
//   - **严格小于** `cfg.StopLossBid`（默认 0.30）;
//   - `bid == 0` **不是触发**而是「整侧撤空、卖不掉」——触发它只会产出一条注定被拒
//     的卖单（决策 #21 的常态）, 由调用方按普通 tick 跳过, 不落任何行;
//   - `StopLossEnabled=false` ⇒ 恒 false（总开关, 关掉 = 退回持有到期）。
//
// ⚠️ 只有价格腿、**不带 dev**——实盘证据（memory ⑦）: 文档原口径
// `bid<0.30 ∧ dev<−20` 在实盘只在 13 窗出现、可成交 9 笔、出场价中位 0.101,
// 只捞到 9/23 输单; 纯价格腿 25 次触发、全中 23 输单、出场价中位 0.260。
// dev 腿 = 等确认, 而确认到的时候报价已经塌了。
func StopTrigger(cfg Config, bid float64) bool {
	return cfg.StopLossEnabled && bid > 0 && bid < cfg.StopLossBid
}

// HoldBidOf 取**持仓侧**最优买价（side = 所押侧 yes/no）。
// 与 hold.go 的取值同源（UpBid/DownBid）, 但独立成函数供止损路径调用——
// 两条路径的门控不同（见 StopArmed）, 取值口径必须一致。
func HoldBidOf(side string, t flip.Tick) float64 {
	if side == flip.SideYes {
		return t.UpBid
	}
	return t.DownBid
}

// StopArmed 判断本 tick 是否**可以用来检查止损**（纯函数; 门控）。
//
// 与 HoldWatchRow 的门控**刻意不同**, 与本族判定路径的半有效 tick 口径对齐:
//   - `rem > 0`（闭市后挂单没有意义）;
//   - `BookLatMs ≤ maxLatMs`（盘口延迟闸——staleness 的 bid 卖不出好价）;
//   - **不要求 spot > 0、不要求 anchor > 0**——止损是纯价格腿: 卖出要的是
//     「现在有没有人按这个价买」, 与结算线口径的 dev 无关（实盘证据里 19/25 次
//     触发时 dev ≥ 0, 即报价先塌、结算线还没动）;
//   - **不要求持仓侧 bid > 0**——bid == 0 不是「不能检查」而是「检查了也不触发」
//     （StopTrigger 恒 false）, 由它自己兜住, 免得两处各写一遍 0 的语义。
//
// ⚠️ 「不要求 spot」是它与本族判定路径的唯一门控差异（判定要求 spot > 0）,
// 有意为之: 止损是**旁路**（不进 Engine 状态机、不碰 parity 红线）, 它只关心
// 卖出时刻。
func StopArmed(maxLatMs int64, t flip.Tick) bool {
	return t.Rem > 0 && t.BookLatMs <= maxLatMs
}
