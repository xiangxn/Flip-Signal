package decision

import (
	"fmt"

	"github.com/necklace/lasttrading/internal/mqs"
)

// Decision represents the trading decision.
type Decision int

const (
	DecisionForbidden Decision = iota // Hard reject
	DecisionNoTrade                   // MQS too low, no trade
	DecisionStandard                  // MQS >= 75, standard trade allowed
	DecisionStrong                    // MQS >= 85, strong signal, increase position
)

// String returns the decision name.
func (d Decision) String() string {
	switch d {
	case DecisionForbidden:
		return "FORBIDDEN"
	case DecisionNoTrade:
		return "NO_TRADE"
	case DecisionStandard:
		return "BUY"
	case DecisionStrong:
		return "BUY_STRONG"
	default:
		return "UNKNOWN"
	}
}

// RejectionReason describes why a trade was forbidden.
type RejectionReason string

const (
	ReasonOK              RejectionReason = ""
	ReasonTimeExpired     RejectionReason = "remaining_sec_below_10"
	ReasonLowLiquidity    RejectionReason = "liquidity_below_40"
	ReasonHighNoise       RejectionReason = "noise_above_70"
	ReasonMQSTooLow       RejectionReason = "mqs_below_75"
)

// Result contains the full decision output.
type Result struct {
	Decision Decision
	Reason   RejectionReason // Set when Forbidden
	Message  string
}

// Decide applies the trading admission rules (§9).
func Decide(mq mqs.MarketQuality, remainingSec int) Result {
	// --- Forbidden conditions ---

	if remainingSec < 10 {
		return Result{
			Decision: DecisionForbidden,
			Reason:   ReasonTimeExpired,
			Message:  fmt.Sprintf("remaining %ds < 10s — too late to trade", remainingSec),
		}
	}

	if mq.LiquidityScore < 40 {
		return Result{
			Decision: DecisionForbidden,
			Reason:   ReasonLowLiquidity,
			Message:  fmt.Sprintf("liquidity %.0f < 40 — insufficient depth", mq.LiquidityScore),
		}
	}

	if mq.NoiseScore > 70 {
		return Result{
			Decision: DecisionForbidden,
			Reason:   ReasonHighNoise,
			Message:  fmt.Sprintf("noise %.0f > 70 — market too noisy", mq.NoiseScore),
		}
	}

	// --- Standard trade check ---
	if remainingSec < 10 || remainingSec > 60 {
		return Result{
			Decision: DecisionNoTrade,
			Message:  fmt.Sprintf("remaining %ds outside [10, 60] trading window", remainingSec),
		}
	}

	if mq.Total < 75 {
		return Result{
			Decision: DecisionNoTrade,
			Reason:   ReasonMQSTooLow,
			Message:  fmt.Sprintf("MQS %.0f < 75 — quality insufficient", mq.Total),
		}
	}

	// --- Strong signal check ---
	if mq.Total >= 85 && mq.TrendScore >= 80 && mq.NoiseScore <= 30 && mq.HealthScore >= 70 {
		return Result{
			Decision: DecisionStrong,
			Message:  fmt.Sprintf("strong signal: MQS=%.0f Trend=%.0f Noise=%.0f Health=%.0f", mq.Total, mq.TrendScore, mq.NoiseScore, mq.HealthScore),
		}
	}

	// --- Standard trade ---
	return Result{
		Decision: DecisionStandard,
		Message:  fmt.Sprintf("standard trade: MQS=%.0f remaining=%ds", mq.Total, remainingSec),
	}
}
