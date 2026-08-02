package decision

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/necklace/lasttrading/internal/mqs"
)

// DecisionRecord is the JSON log format (§10).
type DecisionRecord struct {
	Time      string  `json:"time"`
	MarketID  string  `json:"market_id"`
	Remaining int     `json:"remaining"`
	MQS       float64 `json:"MQS"`
	Trend     float64 `json:"trend"`
	Noise     float64 `json:"noise"`
	Health    float64 `json:"health"`
	Flow      float64 `json:"flow"`
	Liquidity float64 `json:"liquidity"`
	BTCReturn string  `json:"btc_return"`
	Decision  string  `json:"decision"`
	Result    string  `json:"result"` // WIN, LOSE, or "" (pending)
}

// ResultRecord is written when a market resolves.
type ResultRecord struct {
	Type           string `json:"type"` // "resolution"
	Time           string `json:"time"`
	MarketID       string `json:"market_id"`
	WinningOutcome string `json:"winning_outcome"`
	OurDecision    string `json:"our_decision"`
	Result         string `json:"result"` // WIN or LOSE
}

// Recorder writes decision records to a JSONL file.
// In-progress decisions (pending market resolution) are held in memory
// and flushed with results when the market resolves.
type Recorder struct {
	mu       sync.Mutex
	file     *os.File
	filePath string

	// Pending decisions for current cycle, keyed by market ID
	pending map[string][]DecisionRecord
}

// NewRecorder creates a new recorder.
func NewRecorder(path string) (*Recorder, error) {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("create log dir: %w", err)
	}

	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, fmt.Errorf("open log file: %w", err)
	}

	return &Recorder{
		file:     f,
		filePath: path,
		pending:  make(map[string][]DecisionRecord),
	}, nil
}

// AddDecision queues a decision record. It is NOT written yet — it will be
// flushed with the result when Resolve() is called for this market.
func (r *Recorder) AddDecision(marketID string, mq mqs.MarketQuality, remainingSec int, btcReturn float64, decision Decision) {
	r.mu.Lock()
	defer r.mu.Unlock()

	rec := DecisionRecord{
		Time:      time.Now().UTC().Format(time.RFC3339),
		MarketID:  marketID,
		Remaining: remainingSec,
		MQS:       round2(mq.Total),
		Trend:     round2(mq.TrendScore),
		Noise:     round2(mq.NoiseScore),
		Health:    round2(mq.HealthScore),
		Flow:      round2(mq.FlowScore),
		Liquidity: round2(mq.LiquidityScore),
		BTCReturn: fmt.Sprintf("%.2f%%", btcReturn*100),
		Decision:  decision.String(),
		Result:    "", // filled in by Resolve()
	}

	r.pending[marketID] = append(r.pending[marketID], rec)
}

// Resolve is called when a market resolves. It writes all pending decisions
// for the market with WIN/LOSE results, plus a resolution summary record.
//   - marketID: the Polymarket market ID
//   - winningOutcome: "Yes" or "No"
//   - ourDirection: "BUY_YES" or "BUY_NO" (the direction we bet on)
func (r *Recorder) Resolve(marketID, winningOutcome, ourDirection string) {
	r.mu.Lock()
	defer r.mu.Unlock()

	// Determine result
	var result string
	switch {
	case ourDirection == "BUY_YES" && winningOutcome == "Yes":
		result = "WIN"
	case ourDirection == "BUY_NO" && winningOutcome == "No":
		result = "WIN"
	case ourDirection == "":
		result = "UNKNOWN"
	default:
		result = "LOSE"
	}

	// Write all pending decisions with result filled in
	for i := range r.pending[marketID] {
		r.pending[marketID][i].Result = result
		data, _ := json.Marshal(r.pending[marketID][i])
		r.file.Write(append(data, '\n'))
	}

	// Write resolution summary
	summary := ResultRecord{
		Type:           "resolution",
		Time:           time.Now().UTC().Format(time.RFC3339),
		MarketID:       marketID,
		WinningOutcome: winningOutcome,
		OurDecision:    ourDirection,
		Result:         result,
	}
	data, _ := json.Marshal(summary)
	r.file.Write(append(data, '\n'))

	delete(r.pending, marketID)
}

// FlushPending writes all pending decisions with empty results (for crash safety).
func (r *Recorder) FlushPending() {
	r.mu.Lock()
	defer r.mu.Unlock()

	for _, recs := range r.pending {
		for _, rec := range recs {
			data, _ := json.Marshal(rec)
			r.file.Write(append(data, '\n'))
		}
	}
	r.pending = make(map[string][]DecisionRecord)
}

// Close closes the underlying file.
func (r *Recorder) Close() error {
	r.FlushPending()
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.file.Close()
}

func round2(v float64) float64 {
	return float64(int(v*100+0.5)) / 100
}
