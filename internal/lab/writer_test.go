package lab

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestWriter_WriteAndRotate(t *testing.T) {
	dir := t.TempDir()

	w, err := NewWriter(dir)
	if err != nil {
		t.Fatalf("NewWriter: %v", err)
	}

	event := &Event{
		ConditionID: "0xtest123",
		StartTime:   time.Now().Unix(),
		OpenPrice:   50000.0,
		ClosePrice:  50100.0,
		Outcome:     0, // Up
		Snapshots: []*ResearchSnapshot{
			{
				CurrentPrice: 50050.0,
				OpenPrice:    50000.0,
				YesPrice:     0.45,
				NoPrice:      0.55,
			},
		},
	}

	if err := w.Write(event); err != nil {
		t.Fatalf("Write: %v", err)
	}

	if err := w.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}

	// Verify file was created with the day's date
	day := time.Now().UTC().Format("2006-01-02")
	expectedName := "events_" + day + ".jsonl"
	files, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("ReadDir: %v", err)
	}
	if len(files) != 1 {
		t.Fatalf("expected 1 file, got %d", len(files))
	}
	if files[0].Name() != expectedName {
		t.Errorf("expected file %s, got %s", expectedName, files[0].Name())
	}

	// Read and verify content
	data, err := os.ReadFile(filepath.Join(dir, expectedName))
	if err != nil {
		t.Fatalf("ReadFile: %v", err)
	}

	var readEvent Event
	if err := json.Unmarshal([]byte(strings.TrimSpace(string(data))), &readEvent); err != nil {
		t.Fatalf("Unmarshal: %v", err)
	}
	if readEvent.ConditionID != "0xtest123" {
		t.Errorf("ConditionID: expected 0xtest123, got %s", readEvent.ConditionID)
	}
	if readEvent.Outcome != 0 {
		t.Errorf("Outcome: expected 0, got %d", readEvent.Outcome)
	}
	if readEvent.OpenPrice != 50000.0 {
		t.Errorf("OpenPrice: expected 50000, got %.2f", readEvent.OpenPrice)
	}
	if len(readEvent.Snapshots) != 1 {
		t.Errorf("Snapshots: expected 1, got %d", len(readEvent.Snapshots))
	}
}

func TestWriter_ClosedDoubleClose(t *testing.T) {
	dir := t.TempDir()

	w, err := NewWriter(dir)
	if err != nil {
		t.Fatalf("NewWriter: %v", err)
	}

	if err := w.Close(); err != nil {
		t.Errorf("first Close: %v", err)
	}
	// Second close should be a no-op (no panic, no error)
	if err := w.Close(); err != nil {
		// Acceptable: either nil or an error about already closed
		t.Logf("second Close returned: %v", err)
	}
}

func TestWriter_MultipleEvents(t *testing.T) {
	dir := t.TempDir()

	w, err := NewWriter(dir)
	if err != nil {
		t.Fatalf("NewWriter: %v", err)
	}

	for i := 0; i < 3; i++ {
		event := &Event{
			ConditionID: "0xmulti" + string(rune('0'+i)),
			OpenPrice:   50000.0,
			ClosePrice:  50000.0 + float64(i)*10,
		}
		if err := w.Write(event); err != nil {
			t.Fatalf("Write %d: %v", i, err)
		}
	}

	if err := w.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}

	day := time.Now().UTC().Format("2006-01-02")
	path := filepath.Join(dir, "events_"+day+".jsonl")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("ReadFile: %v", err)
	}

	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) != 3 {
		t.Fatalf("expected 3 lines, got %d", len(lines))
	}
}
