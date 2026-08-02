package lab

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// Writer persists Events as JSONL (one JSON object per line) with daily file rotation.
//
// File naming: events_YYYY-MM-DD.jsonl under the configured directory.
// Files are opened in append mode — safe to restart without data loss.
//
// Thread-safe: Write may be called from multiple goroutines.
type Writer struct {
	mu         sync.Mutex
	dir        string
	file       *os.File
	buf        *bufio.Writer
	currentDay string
}

// NewWriter creates a Writer that stores events under dir.
// Creates the directory if it does not exist.
func NewWriter(dir string) (*Writer, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("create output dir: %w", err)
	}
	return &Writer{dir: dir}, nil
}

// Write persists an Event as a single JSON line.
// Automatically rotates to a new file when the UTC date changes.
func (w *Writer) Write(event *Event) error {
	w.mu.Lock()
	defer w.mu.Unlock()

	day := time.Now().UTC().Format("2006-01-02")
	if day != w.currentDay {
		if err := w.rotate(day); err != nil {
			return err
		}
	}

	data, err := json.Marshal(event)
	if err != nil {
		return fmt.Errorf("marshal event: %w", err)
	}
	data = append(data, '\n')
	_, err = w.buf.Write(data)
	return err
}

// rotate closes the current file (if any) and opens the file for the new day.
func (w *Writer) rotate(day string) error {
	if w.buf != nil {
		if err := w.buf.Flush(); err != nil {
			return fmt.Errorf("flush: %w", err)
		}
		if err := w.file.Close(); err != nil {
			return fmt.Errorf("close: %w", err)
		}
	}

	path := filepath.Join(w.dir, fmt.Sprintf("events_%s.jsonl", day))
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return fmt.Errorf("open %s: %w", path, err)
	}

	w.file = f
	w.buf = bufio.NewWriter(f)
	w.currentDay = day
	return nil
}

// Close flushes and closes the underlying file.
func (w *Writer) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.buf != nil {
		if err := w.buf.Flush(); err != nil {
			return err
		}
		return w.file.Close()
	}
	return nil
}
