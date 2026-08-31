package flip

import (
	"os"
	"path/filepath"
	"testing"
)

// newTestRecorder 创建临时目录 recorder，测试结束后清理。
func newTestRecorder(t *testing.T) *Recorder {
	t.Helper()
	dir := t.TempDir()
	r, err := NewRecorder(dir)
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	t.Cleanup(func() { r.Close() })
	return r
}

// mkSignalCross 构造一条已通过判定的信号观测（ok=true）。
func mkSignalCross(side string, ts int64) *Cross {
	return &Cross{
		Ts:         ts,
		Side:       side,
		Rem:        200,
		TriggerBid: 0.80,
		PostEnd:    0.50,
		Fill:       0.50,
		FillComp:   0.50,
		Shares:     4.0, // stake 2 / fill 0.5
		OK:         true,
	}
}

// TestRecorder_ResolveWon 验证 UP 侧信号 + outcome=1(DOWN 赢) → flip 赢。
func TestRecorder_ResolveWon(t *testing.T) {
	r := newTestRecorder(t)
	ts := int64(1788186000000)

	// UP 侧触发: outcome=1 表示 DOWN 胜 → flip(UP 崩溃)赢
	if err := r.RecordCross("condA", "btc-updown-5m-1", ts, mkSignalCross("up", ts), "both"); err != nil {
		t.Fatalf("RecordCross: %v", err)
	}
	if err := r.Resolve("condA", 1); err != nil {
		t.Fatalf("Resolve: %v", err)
	}

	sigs := r.Signals()
	if len(sigs) != 1 {
		t.Fatalf("signals = %d, want 1", len(sigs))
	}
	s := sigs[0]
	if s.Won == nil || !*s.Won {
		t.Fatalf("up 触发 + outcome=1 → won=false, 应为 true")
	}
	// pnl = shares - stake = 4 - (4×0.5) = 2
	if s.PnL != 2.0 {
		t.Fatalf("pnl = %v, want 2.0", s.PnL)
	}
	if s.ResolvedAt == "" {
		t.Fatalf("resolved_at 未填充")
	}
}

// TestRecorder_ResolveDownWon 验证 DOWN 侧信号 + outcome=0(UP 赢) → flip 赢
// （down 触发 = 市场信 DOWN 会跌 → 买 UP；UP 赢 = flip 赢）。
func TestRecorder_ResolveDownWon(t *testing.T) {
	r := newTestRecorder(t)
	ts := int64(1788186300000)

	if err := r.RecordCross("condB", "btc-updown-5m-2", ts, mkSignalCross("down", ts), "only"); err != nil {
		t.Fatalf("RecordCross: %v", err)
	}
	if err := r.Resolve("condB", 0); err != nil {
		t.Fatalf("Resolve: %v", err)
	}

	sigs := r.Signals()
	if len(sigs) != 1 {
		t.Fatalf("signals = %d, want 1", len(sigs))
	}
	s := sigs[0]
	if s.Won == nil || !*s.Won {
		t.Fatalf("down 触发 + outcome=0(UP 赢) → won=false, 应为 true")
	}
	if s.PnL != 2.0 {
		t.Fatalf("pnl = %v, want 2.0", s.PnL)
	}
}

// TestRecorder_ResolveDownLost 验证 DOWN 侧信号 + outcome=1(DOWN 赢) → flip 输。
func TestRecorder_ResolveDownLost(t *testing.T) {
	r := newTestRecorder(t)
	ts := int64(1788186500000)

	if err := r.RecordCross("condF", "btc-updown-5m-6", ts, mkSignalCross("down", ts), "only"); err != nil {
		t.Fatalf("RecordCross: %v", err)
	}
	if err := r.Resolve("condF", 1); err != nil {
		t.Fatalf("Resolve: %v", err)
	}

	sigs := r.Signals()
	if len(sigs) != 1 {
		t.Fatalf("signals = %d, want 1", len(sigs))
	}
	s := sigs[0]
	if s.Won == nil || *s.Won {
		t.Fatalf("down 触发 + outcome=1(DOWN 赢) → won=true, 应为 false")
	}
	if s.PnL != -2.0 {
		t.Fatalf("pnl = %v, want -2.0", s.PnL)
	}
}

// TestRecorder_ResolveIdempotent 验证同一 conditionID 重复结算安全。
func TestRecorder_ResolveIdempotent(t *testing.T) {
	r := newTestRecorder(t)
	ts := int64(1788186600000)

	if err := r.RecordCross("condC", "btc-updown-5m-3", ts, mkSignalCross("up", ts), "both"); err != nil {
		t.Fatalf("RecordCross: %v", err)
	}
	if err := r.Resolve("condC", 1); err != nil {
		t.Fatalf("Resolve #1: %v", err)
	}
	if err := r.Resolve("condC", 1); err != nil {
		t.Fatalf("Resolve #2: %v", err)
	}
	sigs := r.Signals()
	if len(sigs) != 1 {
		t.Fatalf("重复结算后 signals = %d, want 1", len(sigs))
	}
}

// TestRecorder_FileWritten 验证 ok=false 记录立即落盘（行级 flush）。
func TestRecorder_FileWritten(t *testing.T) {
	dir := t.TempDir()
	r, err := NewRecorder(dir)
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	defer r.Close()

	c := &Cross{Ts: 1788186900000, Side: "up", Rem: 100, TriggerBid: 0.71, OK: false, RejectReason: "trigger_bid_too_low"}
	if err := r.RecordCross("condD", "btc-updown-5m-4", 1788186900000/1000, c, "only"); err != nil {
		t.Fatalf("RecordCross: %v", err)
	}

	// 不调用 Close 也应有文件内容（行级 flush）
	files, _ := filepath.Glob(filepath.Join(dir, "crosses_*.jsonl"))
	if len(files) != 1 {
		t.Fatalf("jsonl 文件数 = %d, want 1", len(files))
	}
	data, err := os.ReadFile(files[0])
	if err != nil {
		t.Fatalf("ReadFile: %v", err)
	}
	if len(data) == 0 {
		t.Fatalf("jsonl 文件为空（行级 flush 未生效）")
	}
}

// TestRecorder_CloseFlushesPending 验证 Close 刷出未结算信号。
func TestRecorder_CloseFlushesPending(t *testing.T) {
	dir := t.TempDir()
	r, err := NewRecorder(dir)
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	ts := int64(1788187200000)
	if err := r.RecordCross("condE", "btc-updown-5m-5", ts, mkSignalCross("down", ts), "both"); err != nil {
		t.Fatalf("RecordCross: %v", err)
	}
	if err := r.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	files, _ := filepath.Glob(filepath.Join(dir, "crosses_*.jsonl"))
	if len(files) != 1 {
		t.Fatalf("jsonl 文件数 = %d, want 1", len(files))
	}
	data, _ := os.ReadFile(files[0])
	if len(data) == 0 {
		t.Fatalf("Close 后文件为空，未刷出 pending")
	}
}
