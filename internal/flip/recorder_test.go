package flip

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestFlipRecorder_RecordAndResolve_Win(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "signals.jsonl")

	r, err := NewFlipRecorder(path)
	if err != nil {
		t.Fatalf("NewFlipRecorder: %v", err)
	}

	sig := &FlipSignal{
		Time:         time.Now().UTC(),
		ConditionID:  "0xabc123",
		Side:         "yes",
		Score:        6,
		EntryPrice:   0.15,
		Shares:       1.0,
		ExecStatus:   "filled",
		FilledShares: 1.0,
		AvgFillPrice: 0.15,
		RemainingSec: 225,
		PathEff:      0.75,
		NoiseRatio:   1.2,
		Flips:        2,
	}

	if err := r.RecordSignal(sig); err != nil {
		t.Fatalf("RecordSignal: %v", err)
	}

	// side="yes" → bought NO → wins if outcome=1 (Down)
	if err := r.Resolve("0xabc123", 1); err != nil {
		t.Fatalf("Resolve: %v", err)
	}

	if err := r.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}

	// Read file and verify contents
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("ReadFile: %v", err)
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) != 1 {
		t.Fatalf("expected 1 JSONL line (combined signal), got %d: %q", len(lines), data)
	}

	// 单行包含完整信号（含结算字段）
	var readSig FlipSignal
	if err := json.Unmarshal([]byte(lines[0]), &readSig); err != nil {
		t.Fatalf("unmarshal signal: %v", err)
	}
	if readSig.Side != "yes" || readSig.EntryPrice != 0.15 {
		t.Errorf("signal mismatch: %+v", readSig)
	}
	if !readSig.Won {
		t.Errorf("expected won=true, got %v", readSig.Won)
	}
	// PnL = (1.0 - 0.15) * 1 = 0.85
	if readSig.PnL < 0.84 || readSig.PnL > 0.86 {
		t.Errorf("expected PnL ~0.85, got %.4f", readSig.PnL)
	}
}

func TestFlipRecorder_RecordAndResolve_Loss(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "signals.jsonl")

	r, err := NewFlipRecorder(path)
	if err != nil {
		t.Fatalf("NewFlipRecorder: %v", err)
	}

	sig := &FlipSignal{
		Time:         time.Now().UTC(),
		ConditionID:  "0xdef456",
		Side:         "no",
		Score:        7,
		EntryPrice:   0.22,
		Shares:       2.0,
		ExecStatus:   "filled",
		FilledShares: 2.0,
		AvgFillPrice: 0.22,
		RemainingSec: 200,
	}

	if err := r.RecordSignal(sig); err != nil {
		t.Fatalf("RecordSignal: %v", err)
	}

	// side="no" → bought YES → wins if outcome=0 (Up)
	// Resolve as Down (1) → loss
	if err := r.Resolve("0xdef456", 1); err != nil {
		t.Fatalf("Resolve: %v", err)
	}

	if err := r.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}

	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("ReadFile: %v", err)
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) != 1 {
		t.Fatalf("expected 1 JSONL line, got %d", len(lines))
	}

	var readSig FlipSignal
	if err := json.Unmarshal([]byte(lines[0]), &readSig); err != nil {
		t.Fatalf("unmarshal signal: %v", err)
	}
	if readSig.Won {
		t.Error("expected loss")
	}
	// PnL = (0.0 - 0.22) * 2 = -0.44
	if readSig.PnL > -0.43 || readSig.PnL < -0.45 {
		t.Errorf("expected PnL ~-0.44, got %.4f", readSig.PnL)
	}
}

func TestFlipRecorder_Resolve_NoPendingSignal(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "signals.jsonl")

	r, err := NewFlipRecorder(path)
	if err != nil {
		t.Fatalf("NewFlipRecorder: %v", err)
	}
	defer r.Close()

	// Resolve a market that had no signal — should be a no-op
	if err := r.Resolve("0xnonexistent", 0); err != nil {
		t.Errorf("Resolve with no pending signal should not error: %v", err)
	}
}

func TestFlipRecorder_SignalStats(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "signals.jsonl")

	r, err := NewFlipRecorder(path)
	if err != nil {
		t.Fatalf("NewFlipRecorder: %v", err)
	}
	defer r.Close()

	// Record and resolve 2 won, 1 lost
	sigs := []struct {
		cid     string
		side    string
		price   float64
		outcome int
		won     bool
	}{
		{"0x01", "yes", 0.15, 1, true},  // YES side, Down wins
		{"0x02", "no", 0.20, 0, true},   // NO side, Up wins
		{"0x03", "yes", 0.25, 0, false}, // YES side, Up (loss)
	}

	for _, s := range sigs {
		sig := &FlipSignal{
			ConditionID:  s.cid,
			Side:         s.side,
			EntryPrice:   s.price,
			Shares:       1.0,
			ExecStatus:   "filled",
			FilledShares: 1.0,
			AvgFillPrice: s.price,
		}
		r.RecordSignal(sig)
		r.Resolve(s.cid, s.outcome)
	}

	total, won, lost, pending, winRate, cumPnl := r.SignalStats()
	if total != 3 {
		t.Errorf("total: expected 3, got %d", total)
	}
	if won != 2 {
		t.Errorf("won: expected 2, got %d", won)
	}
	if lost != 1 {
		t.Errorf("lost: expected 1, got %d", lost)
	}
	if pending != 0 {
		t.Errorf("pending: expected 0, got %d", pending)
	}
	if winRate < 0.66 || winRate > 0.67 {
		t.Errorf("winRate: expected ~0.667, got %.4f", winRate)
	}
	// PnL: (1-0.15) + (1-0.20) + (0-0.25) = 0.85 + 0.80 - 0.25 = 1.40
	if cumPnl < 1.39 || cumPnl > 1.41 {
		t.Errorf("cumPnl: expected ~1.40, got %.4f", cumPnl)
	}
}

// Test failed signals: count in won/lost but PnL=0
func TestFlipRecorder_FailedSignal(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "signals.jsonl")

	r, err := NewFlipRecorder(path)
	if err != nil {
		t.Fatalf("NewFlipRecorder: %v", err)
	}
	defer r.Close()

	sig := &FlipSignal{
		ConditionID:  "0xfail",
		Side:         "yes",
		EntryPrice:   0.15,
		Shares:       1.0,
		ExecStatus:   "failed",
		FilledShares: 0,
		AvgFillPrice: 0,
	}
	r.RecordSignal(sig)
	// Outcome=1 (Down) → would have won if executed
	r.Resolve("0xfail", 1)

	_, won, lost, _, _, cumPnl := r.SignalStats()
	if won != 1 {
		t.Errorf("failed signal should count as won (direction correct), got won=%d", won)
	}
	if lost != 0 {
		t.Errorf("failed signal should not count as lost, got lost=%d", lost)
	}
	if cumPnl != 0 {
		t.Errorf("failed signal PnL should be 0, got %.4f", cumPnl)
	}

	// 第二个 failed 信号：方向错误
	sig2 := &FlipSignal{
		ConditionID:  "0xfail2",
		Side:         "no",
		EntryPrice:   0.20,
		Shares:       1.0,
		ExecStatus:   "failed",
		FilledShares: 0,
		AvgFillPrice: 0,
	}
	r.RecordSignal(sig2)
	// Outcome=1 (Down) → NO side would lose
	r.Resolve("0xfail2", 1)

	_, won2, lost2, _, _, cumPnl2 := r.SignalStats()
	if won2 != 1 {
		t.Errorf("total won should be 1, got %d", won2)
	}
	if lost2 != 1 {
		t.Errorf("total lost should be 1 (one won failed + one lost failed), got %d", lost2)
	}
	if cumPnl2 != 0 {
		t.Errorf("all failed signals PnL should be 0, got %.4f", cumPnl2)
	}
}
