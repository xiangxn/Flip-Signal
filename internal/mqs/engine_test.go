package mqs

import (
	"math"
	"testing"
	"time"

	"github.com/necklace/lasttrading/internal/snapshot"
)

// Helper: generate a window of snapshots from a price series.
func makeWindow(prices []float64) []*snapshot.Snapshot {
	now := time.Now().UnixMilli()
	window := make([]*snapshot.Snapshot, len(prices))
	for i, p := range prices {
		window[i] = &snapshot.Snapshot{
			Timestamp:    now + int64(i*1000),
			MarketID:     "test",
			RemainingSec: 300 - i,
			OpenPrice:    prices[0],
			Price:        p,
		}
	}
	return window
}

func makeWindowWithVolume(prices []float64, buyVols, sellVols []float64) []*snapshot.Snapshot {
	now := time.Now().UnixMilli()
	window := make([]*snapshot.Snapshot, len(prices))
	for i, p := range prices {
		bv := 0.0
		sv := 0.0
		if i < len(buyVols) {
			bv = buyVols[i]
		}
		if i < len(sellVols) {
			sv = sellVols[i]
		}
		window[i] = &snapshot.Snapshot{
			Timestamp:    now + int64(i*1000),
			MarketID:     "test",
			RemainingSec: 300 - i,
			OpenPrice:    prices[0],
			Price:        p,
			BuyVolume1s:  bv,
			SellVolume1s: sv,
			SignedFlow10s: bv - sv,
		}
	}
	return window
}

func TestTrendScore_StrongTrend(t *testing.T) {
	// Strong uptrend: linear price increase
	prices := make([]float64, 30)
	for i := 0; i < 30; i++ {
		prices[i] = 100.0 + float64(i)*0.1 // 100 → 102.9
	}
	window := makeWindow(prices)
	score := ComputeTrendScore(window)

	if score < 70 {
		t.Errorf("Expected high trend score for strong uptrend, got %.1f", score)
	}
	t.Logf("Strong uptrend TrendScore: %.1f", score)
}

func TestTrendScore_Noisy(t *testing.T) {
	// Oscillating prices should get low trend score
	prices := make([]float64, 30)
	for i := 0; i < 30; i++ {
		if i%2 == 0 {
			prices[i] = 100.0
		} else {
			prices[i] = 101.0
		}
	}
	window := makeWindow(prices)
	score := ComputeTrendScore(window)

	if score > 50 {
		t.Errorf("Expected low trend score for oscillating prices, got %.1f", score)
	}
	t.Logf("Oscillating TrendScore: %.1f", score)
}

func TestNoiseScore_CalmMarket(t *testing.T) {
	// Steady uptrend should have low noise (NoiseScore close to 0)
	prices := make([]float64, 30)
	for i := 0; i < 30; i++ {
		prices[i] = 100.0 + float64(i)*0.05
	}
	window := makeWindow(prices)
	score := ComputeNoiseScore(window)

	// Calm = low NoiseScore (< 30)
	t.Logf("Calm market NoiseScore: %.1f", score)
	if score > 30 {
		t.Errorf("Expected low noise (<30) for calm market, got %.1f", score)
	}
}

func TestNoiseScore_Volatile(t *testing.T) {
	// High volatility should have high noise score
	prices := make([]float64, 30)
	for i := 0; i < 30; i++ {
		prices[i] = 100.0 + math.Sin(float64(i)*0.8)*2.0
	}
	window := makeWindow(prices)
	score := ComputeNoiseScore(window)

	// Volatile = high NoiseScore
	t.Logf("Volatile market NoiseScore: %.1f", score)
	// Note: a sine wave returns to start, so net=0 but path>0 → high noise ratio → low quality → high NoiseScore
}

func TestHealthScore_Momentum(t *testing.T) {
	// Strong momentum should have good health
	prices := make([]float64, 30)
	buyVols := make([]float64, 30)
	sellVols := make([]float64, 30)

	for i := 0; i < 30; i++ {
		// Accelerating price
		prices[i] = 100.0 + float64(i)*0.02 + float64(i*i)*0.001
		// Increasing volume
		buyVols[i] = 1.0 + float64(i)*0.1
		sellVols[i] = 0.5
	}

	window := makeWindowWithVolume(prices, buyVols, sellVols)
	score := ComputeHealthScore(window)
	t.Logf("Strong momentum HealthScore: %.1f", score)
	if score < 50 {
		t.Errorf("Expected good health for accelerating market, got %.1f", score)
	}
}

func TestFlowScore_BuyHeavy(t *testing.T) {
	prices := make([]float64, 30)
	buyVols := make([]float64, 30)
	sellVols := make([]float64, 30)

	for i := 0; i < 30; i++ {
		prices[i] = 100.0 + float64(i)*0.02
		buyVols[i] = 2.0 // consistently more buys
		sellVols[i] = 0.5
	}

	window := makeWindowWithVolume(prices, buyVols, sellVols)
	score := ComputeFlowScore(window)
	t.Logf("Buy-heavy FlowScore: %.1f", score)
	if score < 60 {
		t.Errorf("Expected high flow score for buy-heavy market, got %.1f", score)
	}
}

func TestFullMQS(t *testing.T) {
	// Simulate a "perfect" market: steady uptrend with balanced data
	prices := make([]float64, 60)
	buyVols := make([]float64, 60)
	sellVols := make([]float64, 60)

	for i := 0; i < 60; i++ {
		prices[i] = 100.0 + float64(i)*0.05
		buyVols[i] = 2.0
		sellVols[i] = 1.0
	}

	window := makeWindowWithVolume(prices, buyVols, sellVols)

	// Set some order book data on the last few snapshots
	for i := 55; i < 60; i++ {
		window[i].BidDepth5 = 10.0
		window[i].AskDepth5 = 10.0
		window[i].BidDepth10 = 20.0
		window[i].AskDepth10 = 20.0
		window[i].YesPrice = 0.51
		window[i].NoPrice = 0.50
	}

	mq := ComputeMQS(window)

	t.Logf("MQS Results:")
	t.Logf("  Trend:     %.1f", mq.TrendScore)
	t.Logf("  Noise:     %.1f", mq.NoiseScore)
	t.Logf("  Health:    %.1f", mq.HealthScore)
	t.Logf("  Flow:      %.1f", mq.FlowScore)
	t.Logf("  Liquidity: %.1f", mq.LiquidityScore)
	t.Logf("  TOTAL:     %.1f", mq.Total)

	// Verify ranges
	if mq.TrendScore < 0 || mq.TrendScore > 100 {
		t.Errorf("TrendScore out of range: %.1f", mq.TrendScore)
	}
	if mq.NoiseScore < 0 || mq.NoiseScore > 100 {
		t.Errorf("NoiseScore out of range: %.1f", mq.NoiseScore)
	}
	if mq.HealthScore < 0 || mq.HealthScore > 100 {
		t.Errorf("HealthScore out of range: %.1f", mq.HealthScore)
	}
	if mq.FlowScore < 0 || mq.FlowScore > 100 {
		t.Errorf("FlowScore out of range: %.1f", mq.FlowScore)
	}
	if mq.LiquidityScore < 0 || mq.LiquidityScore > 100 {
		t.Errorf("LiquidityScore out of range: %.1f", mq.LiquidityScore)
	}
	if mq.Total < 0 || mq.Total > 100 {
		t.Errorf("Total out of range: %.1f", mq.Total)
	}

	// Verify weighted formula
	expectedTotal := mq.TrendScore*0.30 +
		(100-mq.NoiseScore)*0.25 +
		mq.HealthScore*0.20 +
		mq.FlowScore*0.15 +
		mq.LiquidityScore*0.10

	expectedTotal = clamp(expectedTotal, 0, 100)
	if math.Abs(mq.Total-expectedTotal) > 0.01 {
		t.Errorf("Total does not match weighted formula: got %.1f, expected %.1f", mq.Total, expectedTotal)
	}
}

func TestEmptyWindow(t *testing.T) {
	mq := ComputeMQS(nil)
	if mq.Total != 0 {
		t.Errorf("Expected 0 for empty window, got %.1f", mq.Total)
	}
}

func TestSingleSnapshot(t *testing.T) {
	window := makeWindow([]float64{100.0})
	mq := ComputeMQS(window)

	// Should not panic, scores should be valid
	if mq.Total < 0 || mq.Total > 100 {
		t.Errorf("Total out of range for single snapshot: %.1f", mq.Total)
	}
}
