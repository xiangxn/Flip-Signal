package trading

import (
	"testing"
	"time"
)

func TestCheckRisk_NotEnabled(t *testing.T) {
	es := ExecutionState{Enabled: false}
	v := CheckRisk(es, DefaultConfig(), time.Now())
	if v.OK {
		t.Fatal("expected rejection when not enabled")
	}
}

func TestCheckRisk_DailyLimitHit(t *testing.T) {
	es := ExecutionState{Enabled: true, DailyLimitHit: true}
	v := CheckRisk(es, DefaultConfig(), time.Now())
	if v.OK {
		t.Fatal("expected rejection when daily limit hit")
	}
}

func TestCheckRisk_DailyPnlExceedsMaxLoss(t *testing.T) {
	cfg := DefaultConfig()
	cfg.MaxDailyLoss = 10.0
	es := ExecutionState{Enabled: true, DailyPnl: -10.0}
	v := CheckRisk(es, cfg, time.Now())
	if v.OK {
		t.Fatal("expected rejection when daily pnl <= -maxDailyLoss")
	}
}

func TestCheckRisk_Cooldown(t *testing.T) {
	now := time.Now()
	es := ExecutionState{
		Enabled:       true,
		CooldownUntil: now.Add(5 * time.Minute),
	}
	v := CheckRisk(es, DefaultConfig(), now)
	if v.OK {
		t.Fatal("expected rejection during cooldown")
	}
}

func TestCheckRisk_CooldownExpired(t *testing.T) {
	now := time.Now()
	es := ExecutionState{
		Enabled:       true,
		CooldownUntil: now.Add(-1 * time.Second),
	}
	v := CheckRisk(es, DefaultConfig(), now)
	if !v.OK {
		t.Fatal("expected pass when cooldown expired, got: " + v.Reason)
	}
}

func TestCheckRisk_OpenPosition(t *testing.T) {
	es := ExecutionState{
		Enabled:  true,
		Position: &Position{ConditionID: "0x123"},
	}
	v := CheckRisk(es, DefaultConfig(), time.Now())
	if v.OK {
		t.Fatal("expected rejection when position is open")
	}
}

func TestCheckRisk_Pass(t *testing.T) {
	es := ExecutionState{Enabled: true}
	v := CheckRisk(es, DefaultConfig(), time.Now())
	if !v.OK {
		t.Fatalf("expected pass, got: %s", v.Reason)
	}
}

func TestComputeShares(t *testing.T) {
	tests := []struct {
		name   string
		stake  float64
		price  float64
		expect float64
	}{
		{"normal", 5.0, 0.2, 25},
		{"round down", 5.0, 0.22, 22},
		{"min 1 share", 5.0, 100.0, 1},
		{"zero price", 5.0, 0, 0},
		{"negative price", 5.0, -1, 0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := ComputeShares(tt.stake, tt.price)
			if got != tt.expect {
				t.Errorf("ComputeShares(%v, %v) = %v, want %v", tt.stake, tt.price, got, tt.expect)
			}
		})
	}
}

func TestCalcPnL(t *testing.T) {
	tests := []struct {
		name   string
		shares float64
		price  float64
		won    bool
		expect float64
	}{
		{"win", 25, 0.20, true, 20.0},    // 25 * (1-0.2) = 20
		{"loss", 25, 0.20, false, -5.0},  // -25 * 0.2 = -5
		{"win zero", 0, 0.20, true, 0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := CalcPnL(tt.shares, tt.price, tt.won)
			if got != tt.expect {
				t.Errorf("CalcPnL(%v, %v, %v) = %v, want %v", tt.shares, tt.price, tt.won, got, tt.expect)
			}
		})
	}
}

func TestIsNewDay(t *testing.T) {
	// same UTC day
	now := time.Now().UTC()
	sameDay := now.Add(1 * time.Hour)
	if IsNewDay(now, sameDay) {
		t.Error("same day should not be new day")
	}

	// cross UTC midnight: 23:59 → 00:01 next day
	prev := time.Date(2026, 8, 8, 23, 59, 0, 0, time.UTC)
	next := time.Date(2026, 8, 9, 0, 1, 0, 0, time.UTC)
	if !IsNewDay(prev, next) {
		t.Error("crossing UTC midnight should be new day")
	}
}
