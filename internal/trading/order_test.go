package trading

import (
	"math"
	"testing"

	"github.com/necklace/flip-signal/internal/flip"
)

func TestSignalToOrder_YesSide(t *testing.T) {
	sig := &flip.FlipSignal{Side: "yes"}
	tok, side, err := SignalToOrder(sig, "yesTok", "noTok")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if tok != "noTok" {
		t.Errorf("expected noTok, got %s", tok)
	}
	if side != "no" {
		t.Errorf("expected side=no, got %s", side)
	}
}

func TestSignalToOrder_NoSide(t *testing.T) {
	sig := &flip.FlipSignal{Side: "no"}
	tok, side, err := SignalToOrder(sig, "yesTok", "noTok")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if tok != "yesTok" {
		t.Errorf("expected yesTok, got %s", tok)
	}
	if side != "yes" {
		t.Errorf("expected side=yes, got %s", side)
	}
}

func TestSignalToOrder_MissingToken(t *testing.T) {
	// side=yes 但 noTokenID 为空
	sig := &flip.FlipSignal{Side: "yes"}
	_, _, err := SignalToOrder(sig, "yesTok", "")
	if err == nil {
		t.Fatal("expected error for missing NO token")
	}

	// side=no 但 yesTokenID 为空
	sig.Side = "no"
	_, _, err = SignalToOrder(sig, "", "noTok")
	if err == nil {
		t.Fatal("expected error for missing YES token")
	}
}

func TestSignalToOrder_UnknownSide(t *testing.T) {
	sig := &flip.FlipSignal{Side: "up"}
	_, _, err := SignalToOrder(sig, "yesTok", "noTok")
	if err == nil {
		t.Fatal("expected error for unknown side")
	}
}

func TestCalcMaxPrice(t *testing.T) {
	tests := []struct {
		name     string
		entry    float64
		slippage float64
		expect   float64
	}{
		{"normal", 0.20, 0.07, 0.214},
		{"zero slippage", 0.20, 0, 0.20},
		{"large slippage", 0.25, 0.07, 0.2675},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := CalcMaxPrice(tt.entry, tt.slippage)
			if math.Abs(got-tt.expect) > 0.0001 {
				t.Errorf("CalcMaxPrice(%v, %v) = %v, want %v", tt.entry, tt.slippage, got, tt.expect)
			}
		})
	}
}

func TestCalcMaxPrice_Invalid(t *testing.T) {
	if !math.IsNaN(CalcMaxPrice(0, 0.07)) {
		t.Error("expected NaN for zero entry price")
	}
	if !math.IsNaN(CalcMaxPrice(-0.1, 0.07)) {
		t.Error("expected NaN for negative entry price")
	}
}
