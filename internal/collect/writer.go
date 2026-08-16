package collect

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// JSONLWriter 是通用的按日切分 JSONL 写入器（从 lab.Writer 泛化抽出）。
//
// 任意可序列化结构均可写入，每行一个 JSON object；文件名 = prefix_YYYY-MM-DD.jsonl，
// 按 UTC 日期自动切分；O_APPEND 追加模式，重启不丢数据。
//
// 线程安全：Write 可被多个 goroutine 并发调用。
type JSONLWriter struct {
	mu         sync.Mutex
	dir        string
	prefix     string
	file       *os.File
	buf        *bufio.Writer
	currentDay string
}

// NewJSONLWriter 创建写入器。dir 不存在时自动创建。
func NewJSONLWriter(dir, prefix string) (*JSONLWriter, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("create output dir: %w", err)
	}
	return &JSONLWriter{dir: dir, prefix: prefix}, nil
}

// Write 将 v 序列化为一行 JSON 追加到当日文件。
func (w *JSONLWriter) Write(v any) error {
	w.mu.Lock()
	defer w.mu.Unlock()

	day := time.Now().UTC().Format("2006-01-02")
	if day != w.currentDay {
		if err := w.rotate(day); err != nil {
			return err
		}
	}

	data, err := json.Marshal(v)
	if err != nil {
		return fmt.Errorf("marshal record: %w", err)
	}
	data = append(data, '\n')
	_, err = w.buf.Write(data)
	return err
}

// rotate 关闭旧文件并打开新一天的文件（追加模式）。
func (w *JSONLWriter) rotate(day string) error {
	if w.buf != nil {
		if err := w.buf.Flush(); err != nil {
			return fmt.Errorf("flush: %w", err)
		}
		if err := w.file.Close(); err != nil {
			return fmt.Errorf("close: %w", err)
		}
	}

	path := filepath.Join(w.dir, fmt.Sprintf("%s_%s.jsonl", w.prefix, day))
	file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return fmt.Errorf("open %s: %w", path, err)
	}
	w.file = file
	w.buf = bufio.NewWriter(file)
	w.currentDay = day
	return nil
}

// Close 落盘并关闭当前文件。
func (w *JSONLWriter) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.buf != nil {
		if err := w.buf.Flush(); err != nil {
			return err
		}
		if err := w.file.Close(); err != nil {
			return err
		}
		w.buf = nil
		w.file = nil
	}
	return nil
}
