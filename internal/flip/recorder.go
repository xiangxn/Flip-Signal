package flip

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

// FlipRecorder 将翻转交易信号写入 JSONL 文件，用于纸面交易分析。
// 信号即时写入；结算结果（won/pnl）在市场结算时追加为第二行。
type FlipRecorder struct {
	mu       sync.Mutex
	file     *os.File
	pending  map[string]*FlipSignal // conditionID → signal
	resolved []*FlipSignal          // all resolved signals (for dashboard)
}

// NewFlipRecorder 创建追加写入指定 JSONL 文件路径的 recorder。
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

// RecordSignal 写入信号记录（不含 won/pnl）并暂存以待结算。
// 每个 conditionID 仅保留第一个信号。
func (r *FlipRecorder) RecordSignal(sig *FlipSignal) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	// 暂存以待结算 —— 重复时覆盖
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

// Resolve 计算待结算信号的 won/pnl 并写入结算记录。
// outcome 遵循 lab.Event.Outcome: 0=Up, 1=Down。
func (r *FlipRecorder) Resolve(conditionID string, outcome int) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	sig, ok := r.pending[conditionID]
	if !ok {
		return nil // no signal for this market, nothing to do
	}
	delete(r.pending, conditionID)

	// 胜负判定：
	//   side=="yes" → YES>0.7, 买 NO（赌 DOWN）→ outcome==1（Down）时赢
	//   side=="no"  → NO>0.7, 买 YES（赌 UP）→ outcome==0（Up）时赢
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
	pnlRounded := round4(pnl)

	// 回写到信号对象，供 Dashboard 统计
	sig.Won = won
	sig.PnL = pnlRounded

	// 保存已结算信号，供 Dashboard 历史查询
	r.resolved = append(r.resolved, sig)

	rec := resolutionRecord{
		Type:        "resolution",
		ConditionID: conditionID,
		Side:        sig.Side,
		EntryPrice:  sig.EntryPrice,
		Shares:      sig.Shares,
		Won:         won,
		PnL:         pnlRounded,
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

// Close 刷新待结算信号并关闭文件。
func (r *FlipRecorder) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()

	// 写入任何未结算信号（正常运行中不应出现）
	for _, sig := range r.pending {
		data, _ := json.Marshal(sig)
		r.file.Write(append(data, '\n'))
	}
	r.pending = make(map[string]*FlipSignal)
	return r.file.Close()
}

// resolutionRecord 是市场结算时追加的 JSON 行。
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
	if v < 0 {
		return float64(int(v*10000-0.5)) / 10000
	}
	return float64(int(v*10000+0.5)) / 10000
}

// ── Dashboard 访问器 ──

// PendingSignals 返回所有待结算（未结算）信号的副本。
func (r *FlipRecorder) PendingSignals() []*FlipSignal {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]*FlipSignal, 0, len(r.pending))
	for _, sig := range r.pending {
		out = append(out, sig)
	}
	return out
}

// ResolvedSignals 返回所有已结算信号的副本。
func (r *FlipRecorder) ResolvedSignals() []*FlipSignal {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]*FlipSignal, len(r.resolved))
	copy(out, r.resolved)
	return out
}

// SignalStats 返回 Dashboard 展示所需的汇总统计。
func (r *FlipRecorder) SignalStats() (total, won, lost, pending int, winRate, cumPnl float64) {
	r.mu.Lock()
	defer r.mu.Unlock()

	pending = len(r.pending)
	resolved := len(r.resolved)
	total = resolved + pending

	for _, sig := range r.resolved {
		if sig.Won {
			won++
			cumPnl += sig.PnL
		} else {
			lost++
			cumPnl += sig.PnL
		}
	}

	if won+lost > 0 {
		winRate = float64(won) / float64(won+lost)
	}
	return
}
