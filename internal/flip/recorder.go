package flip

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// FlipRecorder 将翻转交易信号写入 JSONL 文件。
//
// 写入策略：信号检测和成交确认阶段仅在内存中缓存（pending map），
// 市场结算时一次性写入一条完整 JSON 行，包含检测时间、成交时间、结算时间。
// 崩溃恢复：Close() 会将未结算信号刷入文件（部分字段为空）。
type FlipRecorder struct {
	mu       sync.Mutex
	file     *os.File
	pending  map[string]*FlipSignal // conditionID → signal（内存缓存）
	resolved []*FlipSignal          // 已结算信号（Dashboard 历史查询）
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

// RecordSignal 暂存信号到内存（不写文件，等 Resolve 时一并写出）。
// 每个 conditionID 仅保留第一个信号。
func (r *FlipRecorder) RecordSignal(sig *FlipSignal) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	r.pending[sig.ConditionID] = sig
	return nil
}

// UpdateExecution 回填信号的执行结果到内存（Trader 调用）。
//
// 首次变为 "filled" 时记录 FilledAt 时间戳和滑点。
// 不写文件 —— 完整记录在 Resolve 时一次性写出。
func (r *FlipRecorder) UpdateExecution(conditionID, execStatus string, filledShares, avgFillPrice float64) {
	r.mu.Lock()
	defer r.mu.Unlock()

	sig, ok := r.pending[conditionID]
	if !ok {
		return
	}

	wasFilled := sig.ExecStatus == "filled"
	sig.ExecStatus = execStatus
	sig.FilledShares = filledShares
	sig.AvgFillPrice = avgFillPrice

	// 成交后同步 Shares 为实际成交股数，确保 Dashboard 显示与 Polymarket 持仓一致
	if execStatus == "filled" && filledShares > 0 {
		sig.Shares = filledShares
	}

	// 首次成交：记录时间戳和滑点
	if execStatus == "filled" && !wasFilled && filledShares > 0 {
		sig.FilledAt = time.Now().UTC()
		sig.SlippageBps = calcSlippageBps(sig.EntryPrice, avgFillPrice)
	}
}

// Resolve 结算信号：计算 won/pnl，写入一条完整 JSONL 行，移入 resolved。
// outcome 遵循 lab.Event.Outcome: 0=Up, 1=Down。
func (r *FlipRecorder) Resolve(conditionID string, outcome int) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	sig, ok := r.pending[conditionID]
	if !ok {
		return nil
	}
	delete(r.pending, conditionID)

	// 胜负判定
	var won bool
	if sig.Side == "yes" {
		won = outcome == 1 // DOWN wins
	} else {
		won = outcome == 0 // UP wins
	}

	// PnL 计算：仅 filled 时按真实成交价/量计算
	var pnl float64
	if sig.ExecStatus == "filled" {
		if won {
			pnl = sig.FilledShares * (1.0 - sig.AvgFillPrice)
		} else {
			pnl = -sig.FilledShares * sig.AvgFillPrice
		}
	}

	sig.Won = won
	sig.PnL = round4(pnl)
	sig.ResolvedAt = time.Now().UTC()

	// 保存已结算信号，供 Dashboard 历史查询
	r.resolved = append(r.resolved, sig)

	// 写入一条完整 JSONL 行
	data, err := json.Marshal(sig)
	if err != nil {
		return fmt.Errorf("marshal signal: %w", err)
	}
	if _, err := r.file.Write(append(data, '\n')); err != nil {
		return fmt.Errorf("write signal: %w", err)
	}

	return nil
}

// Close 将未结算信号刷入文件后关闭。
func (r *FlipRecorder) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()

	// 写入未结算信号（部分字段为空，用于崩溃恢复诊断）
	for _, sig := range r.pending {
		data, _ := json.Marshal(sig)
		r.file.Write(append(data, '\n'))
	}
	r.pending = make(map[string]*FlipSignal)
	return r.file.Close()
}

// calcSlippageBps 计算信号价到成交价的滑点（bp）。
// 正数 = 成交价高于信号价（不利），负数 = 成交价低于信号价（有利）。
func calcSlippageBps(entryPrice, avgFillPrice float64) float64 {
	if entryPrice <= 0 {
		return 0
	}
	return (avgFillPrice - entryPrice) / entryPrice * 10000
}

func round4(v float64) float64 {
	if v < 0 {
		return float64(int(v*10000-0.5)) / 10000
	}
	return float64(int(v*10000+0.5)) / 10000
}

// ── Dashboard 访问器 ──

// PendingSignals 返回所有待结算信号的副本。
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
