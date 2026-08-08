package lab

import (
	"testing"
	"time"

	"github.com/necklace/flip-signal/internal/feed"
)

func TestCollector_StartEventAndFinalize(t *testing.T) {
	binance := feed.NewBinanceAdapter() // won't connect — just need the struct
	c := NewCollector(binance)

	c.StartEvent("0xtest", time.Now().Unix(), 50000.0)

	if c.ConditionID() != "0xtest" {
		t.Errorf("ConditionID: expected 0xtest, got %s", c.ConditionID())
	}
	if c.OpenPrice() != 50000.0 {
		t.Errorf("OpenPrice: expected 50000, got %.2f", c.OpenPrice())
	}

	// Tick returns nil when no Binance data (price == 0)
	snap := c.Tick(time.Now())
	if snap != nil {
		t.Error("expected nil snap when Binance has no data")
	}

	// FinalizeEvent with empty snapshots → ClosePrice=0, outcome=Down
	event := c.FinalizeEvent()
	if event.ClosePrice != 0 {
		t.Errorf("ClosePrice: expected 0, got %.2f", event.ClosePrice)
	}
	if event.Outcome != 1 { // ClosePrice (0) <= OpenPrice (50000) → Down
		t.Errorf("Outcome: expected 1 (Down), got %d", event.Outcome)
	}
	if event.ConditionID != "0xtest" {
		t.Errorf("ConditionID: expected 0xtest, got %s", event.ConditionID)
	}
	if len(event.Snapshots) != 0 {
		t.Errorf("Snapshots: expected 0, got %d", len(event.Snapshots))
	}
}

func TestCollector_FinalizeEvent_UpOutcome(t *testing.T) {
	binance := feed.NewBinanceAdapter()
	c := NewCollector(binance)

	c.StartEvent("0xup", time.Now().Unix(), 50000.0)

	// Manually inject a snapshot with higher close price (simulate what Tick does)
	c.snapshots = []*ResearchSnapshot{
		{CurrentPrice: 50100.0, OpenPrice: 50000.0},
	}

	event := c.FinalizeEvent()
	if event.ClosePrice != 50100.0 {
		t.Errorf("ClosePrice: expected 50100, got %.2f", event.ClosePrice)
	}
	if event.Outcome != 0 { // ClosePrice (50100) > OpenPrice (50000) → Up
		t.Errorf("Outcome: expected 0 (Up), got %d", event.Outcome)
	}
}

func TestCollector_UpdatePolymarket(t *testing.T) {
	binance := feed.NewBinanceAdapter()
	c := NewCollector(binance)

	c.UpdatePolymarket(0.45, 0.55)

	if c.yesPrice != 0.45 {
		t.Errorf("yesPrice: expected 0.45, got %.2f", c.yesPrice)
	}
	if c.noPrice != 0.55 {
		t.Errorf("noPrice: expected 0.55, got %.2f", c.noPrice)
	}
}

func TestCollector_SnapshotsCopy(t *testing.T) {
	binance := feed.NewBinanceAdapter()
	c := NewCollector(binance)

	c.StartEvent("0xcopy", time.Now().Unix(), 50000.0)

	// Inject snapshots
	c.snapshots = []*ResearchSnapshot{
		{CurrentPrice: 50100.0},
		{CurrentPrice: 50200.0},
	}

	copy := c.Snapshots()
	if len(copy) != 2 {
		t.Fatalf("expected 2 snapshots, got %d", len(copy))
	}

	// Mutate copy — should not affect original
	copy[0] = nil
	if c.snapshots[0] == nil {
		t.Error("mutating copy should not affect original snapshots")
	}
}
