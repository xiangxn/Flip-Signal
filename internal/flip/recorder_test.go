package flip

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
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

// mkSignalCross 构造一条已通过判定的信号观测（ok=true, stake=2）。
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

// recFile 读取当日 jsonl 文件内容（单文件假设）。
func recFile(t *testing.T, dir string) string {
	t.Helper()
	files, _ := filepath.Glob(filepath.Join(dir, "crosses_*.jsonl"))
	if len(files) != 1 {
		t.Fatalf("jsonl 文件数 = %d, want 1", len(files))
	}
	data, err := os.ReadFile(files[0])
	if err != nil {
		t.Fatalf("ReadFile: %v", err)
	}
	return string(data)
}

// TestRecorder_ResolveWon 验证 UP 侧信号 + outcome=1(DOWN 赢) → flip 赢。
func TestRecorder_ResolveWon(t *testing.T) {
	r := newTestRecorder(t)
	ts := int64(1788186000000)

	// UP 侧触发: outcome=1 表示 DOWN 胜 → flip(UP 崩溃)赢
	if err := r.RecordCross("condA", "btc-updown-5m-1", ts, mkSignalCross("up", ts), "both", 2.0); err != nil {
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
	// pnl = shares - stake = 4 - 2 = 2
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

	if err := r.RecordCross("condB", "btc-updown-5m-2", ts, mkSignalCross("down", ts), "only", 2.0); err != nil {
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

	if err := r.RecordCross("condF", "btc-updown-5m-6", ts, mkSignalCross("down", ts), "only", 2.0); err != nil {
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

	if err := r.RecordCross("condC", "btc-updown-5m-3", ts, mkSignalCross("up", ts), "both", 2.0); err != nil {
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

// TestRecorder_ResolveUnknownNoOp 验证未知 conditionID 结算为 no-op。
func TestRecorder_ResolveUnknownNoOp(t *testing.T) {
	r := newTestRecorder(t)
	if err := r.Resolve("nope", 0); err != nil {
		t.Fatalf("未知 conditionID 应 no-op: %v", err)
	}
}

// TestRecorder_FileWritten 验证 ok=false 记录立即落盘（行级 flush，无需 Close）。
func TestRecorder_FileWritten(t *testing.T) {
	dir := t.TempDir()
	r, err := NewRecorder(dir)
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	defer r.Close()

	c := &Cross{Ts: 1788186900000, Side: "up", Rem: 100, TriggerBid: 0.71, OK: false, RejectReason: "trigger_bid_too_low"}
	if err := r.RecordCross("condD", "btc-updown-5m-4", 1788186900000/1000, c, "only", 2.0); err != nil {
		t.Fatalf("RecordCross: %v", err)
	}

	// 不调用 Close 也应有文件内容（行级 flush）
	if data := recFile(t, dir); len(data) == 0 {
		t.Fatalf("jsonl 文件为空（行级 flush 未生效）")
	}
}

// TestRecorder_ImmediateWrite 验证 ok=true 信号记录立即落盘:
// 结算前盘上已有该行，且 won/pnl/resolved_at 未填充。
func TestRecorder_ImmediateWrite(t *testing.T) {
	dir := t.TempDir()
	r, err := NewRecorder(dir)
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	defer r.Close()

	ts := int64(1788187200000)
	if err := r.RecordCross("condE", "btc-updown-5m-5", ts, mkSignalCross("down", ts), "both", 2.0); err != nil {
		t.Fatalf("RecordCross: %v", err)
	}

	data := recFile(t, dir)
	lines := strings.Split(strings.TrimSpace(data), "\n")
	if len(lines) != 1 {
		t.Fatalf("行数 = %d, want 1", len(lines))
	}
	if !strings.Contains(lines[0], `"ok":true`) {
		t.Fatalf("未结算信号行应含 ok:true: %s", lines[0])
	}
	if strings.Contains(lines[0], `"won":`) || strings.Contains(lines[0], `"resolved_at"`) {
		t.Fatalf("结算前不应有 won/resolved_at 字段: %s", lines[0])
	}
}

// TestRecorder_ResolveRewritesDisk 验证结算回填后当日文件被原子重写:
// ok 行获得 won/pnl/resolved_at，失败行原样保留。
func TestRecorder_ResolveRewritesDisk(t *testing.T) {
	dir := t.TempDir()
	r, err := NewRecorder(dir)
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	defer r.Close()

	// 先写一条失败观测，再写一条信号，结算后者
	failed := &Cross{Ts: 1788187250000, Side: "down", Rem: 100, TriggerBid: 0.80, PostEnd: 0.70, OK: false, RejectReason: "post_end_too_high"}
	if err := r.RecordCross("condF", "btc-updown-5m-6", 1788187250000/1000, failed, "only", 2.0); err != nil {
		t.Fatalf("RecordCross failed: %v", err)
	}
	ts := int64(1788187300000)
	if err := r.RecordCross("condG", "btc-updown-5m-7", ts, mkSignalCross("up", ts), "both", 2.0); err != nil {
		t.Fatalf("RecordCross signal: %v", err)
	}
	if err := r.Resolve("condG", 1); err != nil {
		t.Fatalf("Resolve: %v", err)
	}

	data := recFile(t, dir)
	lines := strings.Split(strings.TrimSpace(data), "\n")
	if len(lines) != 2 {
		t.Fatalf("行数 = %d, want 2（每事件仍一行）", len(lines))
	}
	var sigLine, failLine string
	for _, ln := range lines {
		if strings.Contains(ln, `"condition_id":"condG"`) {
			sigLine = ln
		} else {
			failLine = ln
		}
	}
	if sigLine == "" {
		t.Fatalf("未找到结算后的信号行: %s", data)
	}
	for _, want := range []string{`"won":true`, `"pnl":2`, `"resolved_at"`} {
		if !strings.Contains(sigLine, want) {
			t.Fatalf("信号行缺 %s: %s", want, sigLine)
		}
	}
	if !strings.Contains(failLine, `"ok":false`) || strings.Contains(failLine, `"won":`) {
		t.Fatalf("失败行应保持原样: %s", failLine)
	}
}

// TestRecorder_ResolveRollback 验证写盘失败时结算字段回滚、保持 pending 可重试。
func TestRecorder_ResolveRollback(t *testing.T) {
	dir := t.TempDir()
	r, err := NewRecorder(dir)
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	defer r.Close()

	ts := int64(1788187400000)
	if err := r.RecordCross("condR", "btc-updown-5m-8", ts, mkSignalCross("up", ts), "both", 2.0); err != nil {
		t.Fatalf("RecordCross: %v", err)
	}
	// 删除记录文件使 rewriteDay 的 os.ReadFile 失败
	day := time.UnixMilli(ts).UTC().Format("2006-01-02")
	if err := os.Remove(filepath.Join(dir, "crosses_"+day+".jsonl")); err != nil {
		t.Fatalf("Remove: %v", err)
	}
	if err := r.Resolve("condR", 1); err == nil {
		t.Fatalf("文件缺失时 Resolve 应返回错误")
	}
	pending := r.PendingSignals()
	if len(pending) != 1 {
		t.Fatalf("pending = %d, want 1（回滚后保持待结算）", len(pending))
	}
	s := pending[0]
	if s.Won != nil || s.PnL != 0 || s.ResolvedAt != "" {
		t.Fatalf("结算字段未回滚: won=%v pnl=%v resolved=%q", s.Won, s.PnL, s.ResolvedAt)
	}
}

// TestRecorder_LoadPendingRestores 验证重启后从磁盘恢复内存态
// （pending/resolved/failed 三态 + 统计访问器）。
func TestRecorder_LoadPendingRestores(t *testing.T) {
	dir := t.TempDir()
	r, err := NewRecorder(dir)
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	ts := int64(1788187500000)
	if err := r.RecordCross("condP", "btc-updown-5m-9", ts, mkSignalCross("up", ts), "both", 2.0); err != nil {
		t.Fatalf("RecordCross signal: %v", err)
	}
	failed := &Cross{Ts: 1788187600000, Side: "down", Rem: 100, TriggerBid: 0.71, OK: false, RejectReason: "trigger_bid_too_low"}
	if err := r.RecordCross("condF2", "btc-updown-5m-10", 1788187600000/1000, failed, "only", 2.0); err != nil {
		t.Fatalf("RecordCross failed: %v", err)
	}
	if err := r.Resolve("condP", 1); err != nil {
		t.Fatalf("Resolve: %v", err)
	}
	if err := r.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}

	// 模拟重启: 新 recorder 从磁盘恢复
	r2, err := NewRecorder(dir)
	if err != nil {
		t.Fatalf("NewRecorder #2: %v", err)
	}
	defer r2.Close()

	if got := r2.PendingSignals(); len(got) != 0 {
		t.Fatalf("重启后 pending = %d, want 0（已结算）", len(got))
	}
	sigs := r2.Signals()
	if len(sigs) != 1 || sigs[0].Won == nil || !*sigs[0].Won || sigs[0].PnL != 2.0 {
		t.Fatalf("重启后信号结算状态丢失: %+v", sigs)
	}
	if got := r2.Count(); got != 2 {
		t.Fatalf("重启后 Count = %d, want 2", got)
	}
	daily, pos := r2.DailyPnl()
	if len(daily) != 1 || pos != 1 || daily[sigs[0].Date] != 2.0 {
		t.Fatalf("逐日统计异常: daily=%v pos=%d", daily, pos)
	}
	if dd := r2.MaxDrawdown(); dd != 0 {
		t.Fatalf("MaxDrawdown = %v, want 0", dd)
	}
}

// TestRecorder_UTCDateAndFilename 验证 UTC 日口径:
// 23:30Z 的观测落在 UTC 当日文件，而非本地时区日期。
func TestRecorder_UTCDateAndFilename(t *testing.T) {
	dir := t.TempDir()
	r, err := NewRecorder(dir)
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	defer r.Close()

	// 2026-08-31T23:30:00Z（本地 UTC+8 为 09-01 07:30，若用本地日会跨文件）
	ts := time.Date(2026, 8, 31, 23, 30, 0, 0, time.UTC).UnixMilli()
	if err := r.RecordCross("condT", "btc-updown-5m-11", ts, mkSignalCross("up", ts), "both", 2.0); err != nil {
		t.Fatalf("RecordCross: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "crosses_2026-08-31.jsonl")); err != nil {
		t.Fatalf("UTC 日文件缺失（非 UTC 口径）: %v", err)
	}
	if got := r.Signals()[0].Date; got != "2026-08-31" {
		t.Fatalf("Date = %q, want 2026-08-31（UTC 日）", got)
	}
}
