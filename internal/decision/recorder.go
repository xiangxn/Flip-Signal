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
	Remaining int     `json:"remaining"`
	MQS       float64 `json:"MQS"`
	Trend     float64 `json:"trend"`
	Noise     float64 `json:"noise"`
	Health    float64 `json:"health"`
	Flow      float64 `json:"flow"`
	Liquidity float64 `json:"liquidity"`
	BTCReturn string  `json:"btc_return"`
	Decision  string  `json:"decision"`
	Result    string  `json:"result"` // "WIN", "LOSE", "" (pending)
}

// Recorder writes decision records to a JSONL file.
type Recorder struct {
	mu       sync.Mutex
	file     *os.File
	filePath string
}

// NewRecorder creates a new recorder that writes to the given path.
// If the directory doesn't exist, it will be created.
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
	}, nil
}

// Record writes a decision record to the log.
func (r *Recorder) Record(mq mqs.MarketQuality, remainingSec int, btcReturn float64, decision Decision, result string) error {
	rec := DecisionRecord{
		Time:      time.Now().UTC().Format(time.RFC3339),
		Remaining: remainingSec,
		MQS:       round2(mq.Total),
		Trend:     round2(mq.TrendScore),
		Noise:     round2(mq.NoiseScore),
		Health:    round2(mq.HealthScore),
		Flow:      round2(mq.FlowScore),
		Liquidity: round2(mq.LiquidityScore),
		BTCReturn: fmt.Sprintf("%.2f%%", btcReturn*100),
		Decision:  decision.String(),
		Result:    result,
	}

	data, err := json.Marshal(rec)
	if err != nil {
		return fmt.Errorf("marshal record: %w", err)
	}

	r.mu.Lock()
	defer r.mu.Unlock()

	_, err = r.file.Write(append(data, '\n'))
	return err
}

// Close closes the underlying file.
func (r *Recorder) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.file.Close()
}

func round2(v float64) float64 {
	return float64(int(v*100+0.5)) / 100
}
