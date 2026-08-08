package trading

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

// TradeRecorder 将订单生命周期与结算记录追加写入 JSONL 文件。
// 模式对齐 flip.FlipRecorder。
type TradeRecorder struct {
	mu   sync.Mutex
	file *os.File
}

// NewTradeRecorder 创建追加写入指定路径的 recorder。
func NewTradeRecorder(path string) (*TradeRecorder, error) {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("create trades output dir: %w", err)
	}

	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, fmt.Errorf("open trades output file: %w", err)
	}

	return &TradeRecorder{file: f}, nil
}

// AppendOrder 写入一条订单记录。
func (r *TradeRecorder) AppendOrder(rec *OrderRecord) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	data, err := json.Marshal(rec)
	if err != nil {
		return fmt.Errorf("marshal order record: %w", err)
	}
	if _, err := r.file.Write(append(data, '\n')); err != nil {
		return fmt.Errorf("write order record: %w", err)
	}
	return nil
}

// AppendPosition 写入一条持仓结算记录。
func (r *TradeRecorder) AppendPosition(p *Position) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	data, err := json.Marshal(p)
	if err != nil {
		return fmt.Errorf("marshal position record: %w", err)
	}
	if _, err := r.file.Write(append(data, '\n')); err != nil {
		return fmt.Errorf("write position record: %w", err)
	}
	return nil
}

// Flush 确保数据落盘。
func (r *TradeRecorder) Flush() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.file.Sync()
}

// Close 关闭文件。
func (r *TradeRecorder) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.file.Close()
}
