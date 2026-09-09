package flip

// CanTrade 日亏熔断闸（纯函数）: 当日已结算 P&L ≤ maxDailyLoss 即停单。
//
// 无状态化设计: todayPnl 由调用方每次从 Recorder.DailyPnl()（磁盘已结算真相）
// 现算传入——天然重启安全、跨日自动归零, 不需要内存日切状态机
// （eth 分支的 CheckRisk 是有状态版, 本实现不复用其结构）。
// 注意: 已结算口径下「刚触发、尚未结算」的亏损单不计入闸值——熔断是止损
// 闸而非预测器, 小额 stake + 熔断叠加已足够, 不做未结算敞口估算。
func CanTrade(todayPnl, maxDailyLoss float64) bool {
	return todayPnl > maxDailyLoss
}
