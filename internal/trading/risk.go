package trading

import (
	"fmt"
	"math"
	"time"
)

// RiskVerdict 风控检查结果。
type RiskVerdict struct {
	OK     bool   // 是否通过
	Reason string // 未通过原因（中文）
}

// Pass 返回通过的风控结果。
func Pass() RiskVerdict { return RiskVerdict{OK: true} }

// Reject 返回拒绝的风控结果。
func Reject(reason string) RiskVerdict { return RiskVerdict{OK: false, Reason: reason} }

// CheckRisk 纯函数：按顺序检查风控闸门。
//
//	1. 未启用 → "实盘未启用"
//	2. 日亏达上限 → "日亏上限已触发，本日停止"
//	3. 亏损冷却中 → "亏损冷却中"
//	4. 已有持仓 → "已有未结算持仓"
func CheckRisk(es ExecutionState, cfg TradingConfig, now time.Time) RiskVerdict {
	if !es.Enabled {
		return Reject("实盘未启用")
	}
	if es.DailyLimitHit {
		return Reject("日亏上限已触发，本日停止")
	}
	if es.DailyPnl <= -cfg.MaxDailyLoss {
		return Reject(fmt.Sprintf("日亏已达上限 %.2f USDC，本日停止", cfg.MaxDailyLoss))
	}
	if now.Before(es.CooldownUntil) {
		remaining := es.CooldownUntil.Sub(now).Round(time.Second)
		return Reject(fmt.Sprintf("亏损冷却中，剩余 %v", remaining))
	}
	if es.Position != nil {
		return Reject("已有未结算持仓")
	}
	return Pass()
}

// ComputeShares 由 USDC 预算和价格计算目标股数（向下取整，最小 1）。
func ComputeShares(stakeUSDC, price float64) float64 {
	if price <= 0 {
		return 0
	}
	shares := math.Floor(stakeUSDC / price)
	if shares < 1 {
		shares = 1
	}
	return shares
}

// CalcPnL 计算结算盈亏。
//
//	won=true  → shares × (1 - avgPrice)
//	won=false → -shares × avgPrice
func CalcPnL(shares, avgPrice float64, won bool) float64 {
	if won {
		return shares * (1.0 - avgPrice)
	}
	return -shares * avgPrice
}

// IsNewDay 判断是否跨过 UTC 零点，用于日盈亏重置。
func IsNewDay(prev, now time.Time) bool {
	py, pm, pd := prev.UTC().Date()
	ny, nm, nd := now.UTC().Date()
	return py != ny || pm != nm || pd != nd
}
