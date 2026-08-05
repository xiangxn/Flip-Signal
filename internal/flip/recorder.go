package flip

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

// FlipRecorder writes flip trading signals to a JSONL file for paper trading
// analysis. Signals are written immediately; resolution (won/pnl) is appended
// as a second line when the market resolves.
type FlipRecorder struct {
	mu      sync.Mutex
	file    *os.File
	pending map[string]*FlipSignal // conditionID → signal
}

// NewFlipRecorder creates a recorder that appends to the given JSONL file path.
func NewFlipRecorder(path string) (*FlipRecorder, error) {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("create output dir: %w", err)
	}

	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, fmt.Errorf("open output file: %w", err)
	}

	return &FlipRecorder{
		file:    f,
		pending: make(map[string]*FlipSignal),
	}, nil
}

// RecordSignal writes a signal record (without won/pnl) and stores it for
// later resolution. Only the first signal per conditionID is kept.
func (r *FlipRecorder) RecordSignal(sig *FlipSignal) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	// Store for resolution — overwrite if somehow duplicate
	r.pending[sig.ConditionID] = sig

	data, err := json.Marshal(sig)
	if err != nil {
		return fmt.Errorf("marshal signal: %w", err)
	}
	if _, err := r.file.Write(append(data, '\n')); err != nil {
		return fmt.Errorf("write signal: %w", err)
	}

	return nil
}

// Resolve computes won/pnl for a pending signal and writes a resolution record.
// outcome follows lab.Event.Outcome: 0=Up, 1=Down.
func (r *FlipRecorder) Resolve(conditionID string, outcome int) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	sig, ok := r.pending[conditionID]
	if !ok {
		return nil // no signal for this market, nothing to do
	}
	delete(r.pending, conditionID)

	// won computation:
	//   side=="yes" → YES>0.7, bought NO (bet DOWN) → win if outcome==1 (Down)
	//   side=="no"  → NO>0.7,  bought YES (bet UP) → win if outcome==0 (Up)
	var won bool
	if sig.Side == "yes" {
		won = outcome == 1 // DOWN wins
	} else {
		won = outcome == 0 // UP wins
	}

	var pnl float64
	if won {
		pnl = (1.0 - sig.EntryPrice) * float64(sig.Shares)
	} else {
		pnl = (0.0 - sig.EntryPrice) * float64(sig.Shares)
	}

	rec := resolutionRecord{
		Type:        "resolution",
		ConditionID: conditionID,
		Side:        sig.Side,
		EntryPrice:  sig.EntryPrice,
		Shares:      sig.Shares,
		Won:         won,
		PnL:         round4(pnl),
	}

	data, err := json.Marshal(rec)
	if err != nil {
		return fmt.Errorf("marshal resolution: %w", err)
	}
	if _, err := r.file.Write(append(data, '\n')); err != nil {
		return fmt.Errorf("write resolution: %w", err)
	}

	return nil
}

// Close flushes pending signals and closes the file.
func (r *FlipRecorder) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()

	// Write any unresolved signals (shouldn't happen in normal operation)
	for _, sig := range r.pending {
		data, _ := json.Marshal(sig)
		r.file.Write(append(data, '\n'))
	}
	r.pending = make(map[string]*FlipSignal)
	return r.file.Close()
}

// resolutionRecord is the JSON line appended when a market resolves.
type resolutionRecord struct {
	Type        string  `json:"type"`
	ConditionID string  `json:"condition_id"`
	Side        string  `json:"side"`
	EntryPrice  float64 `json:"entry_price"`
	Shares      int     `json:"shares"`
	Won         bool    `json:"won"`
	PnL         float64 `json:"pnl"`
}

func round4(v float64) float64 {
	return float64(int(v*10000+0.5)) / 10000
}
