package flip

// CanTrade 日亏熔断闸（纯函数）: 当日已结算 P&L ≤ maxDailyLoss 即停单。
//
// 口径（2026-09-16 明确, plan §3.4）:
//   - 「日」= UTC 日（记录行 Date 即 UTC, 见 utcDate）——与 DailyPnl 聚合同源,
//     跨 UTC 零点自动归零, 无本地时区参与;
//   - 「P&L」= 已结算口径（仅 rec.OK && rec.Won != nil 的行）——被闸行照常结算
//     也计入（方案 A）;
//   - 边界: 严格不等式, todayPnl == maxDailyLoss 时仍可交易（线值是"亏损超过"）;
//     maxDailyLoss ≥ 0 = 永不开单（0 > 0 不成立）——cmd/flip 对 ≥0 直接 Fatal。
//
// 无状态化设计: todayPnl 由调用方每次从 Recorder.DailyPnl()（磁盘已结算真相）
// 现算传入——天然重启安全、跨日自动归零, 不需要内存日切状态机
// （eth 分支的 CheckRisk 是有状态版, 本实现不复用其结构）。
// ⚠️ 本函数只看「当下是否过线」, **不含当日锁存**——过线后若被闸行结算回血,
// 现算值会回升过线而"自动复牌"。锁存在编排层 breakerTripped（exec_state.go）:
// 它叠加当日 GatedOn 磁盘真相, 保证当日一旦触发即停到跨日。
// 注意: 已结算口径下「刚触发、尚未结算」的亏损单不计入闸值——熔断是止损
// 闸而非预测器, 小额 stake + 熔断叠加已足够, 不做未结算敞口估算。
func CanTrade(todayPnl, maxDailyLoss float64) bool {
	return todayPnl > maxDailyLoss
}
