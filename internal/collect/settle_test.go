package collect

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// TestSettlementWorker_Flow 验证队列化写盘 + 后台官方价修正的完整流程：
// 事件先以流值口径立即落盘，官方价到达后追加修正行；重复投递/重复修正被去重。
func TestSettlementWorker_Flow(t *testing.T) {
	dir := t.TempDir()
	calls := 0
	fetch := func(ctx context.Context, start time.Time) (float64, float64, bool) {
		calls++
		if calls >= 2 {
			return 64000, 64100, true // 第 2 次轮询官方价产出
		}
		return 0, 0, false
	}
	cfg := DefaultSettlementConfig()
	cfg.PollInterval = 10 * time.Millisecond
	cfg.MaxWait = 5 * time.Second

	w := NewSettlementWorker(dir, fetch, cfg)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	w.Start(ctx)

	// start_time 用今天真实窗口起点：事件/修正行按 start_time 日归文件
	now := time.Now().UTC()
	st := time.Date(now.Year(), now.Month(), now.Day(), 0, 30, 0, 0, time.UTC).Unix()

	ev := &Event{
		ConditionID:    "cond1",
		Slug:           "btc-updown-5m-1",
		StartTime:      st,
		TwapOpenPrice:  63900,
		TwapClosePrice: 63950,
		CloseSource:    "stream",
		Outcome:        0,
	}
	w.Submit(ev, true)

	path := filepath.Join(dir, "events_"+time.Now().UTC().Format("2006-01-02")+".jsonl")

	// 1. 事件行应立即落盘（流值口径）
	deadline := time.Now().Add(2 * time.Second)
	for {
		data, err := os.ReadFile(path)
		if err == nil && len(data) > 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("事件未立即落盘")
		}
		time.Sleep(5 * time.Millisecond)
	}

	// 2. 官方价到达后追加修正行
	for {
		data, _ := os.ReadFile(path)
		if strings.Count(string(data), "\n") >= 2 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("修正行未写入: %s", string(data))
		}
		time.Sleep(5 * time.Millisecond)
	}
	data, _ := os.ReadFile(path)
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) != 2 {
		t.Fatalf("期望 2 行（事件+修正），实际 %d", len(lines))
	}
	var evLine Event
	if err := json.Unmarshal([]byte(lines[0]), &evLine); err != nil {
		t.Fatalf("事件行解析失败: %v", err)
	}
	if evLine.CloseSource != "stream" || evLine.TwapClosePrice != 63950 {
		t.Fatalf("事件行应为流值口径: %+v", evLine)
	}
	var corr SettlementCorrection
	if err := json.Unmarshal([]byte(lines[1]), &corr); err != nil {
		t.Fatalf("修正行解析失败: %v", err)
	}
	if corr.EventType != "settlement_correction" || corr.StartTime != st ||
		corr.TwapOpenPrice != 64000 || corr.TwapClosePrice != 64100 ||
		corr.Outcome != 0 || corr.CloseSource != "official" {
		t.Fatalf("修正行内容不符: %+v", corr)
	}

	// 3. 同一窗口重复投递事件 → 被去重跳过（文件仍 2 行）
	w.Submit(&Event{ConditionID: "cond1", Slug: "btc-updown-5m-1", StartTime: st}, true)
	time.Sleep(50 * time.Millisecond)
	data, _ = os.ReadFile(path)
	if got := strings.Count(string(data), "\n"); got != 2 {
		t.Fatalf("重复事件应被去重，实际 %d 行", got)
	}
}

// TestNeedsOfficialCorrection 阈值判定（表驱动）：
// 新鲜度不足或幅度过小 → 官方修正；幅度充足且推送新鲜 → 流值定稿。
func TestNeedsOfficialCorrection(t *testing.T) {
	cases := []struct {
		name     string
		ageMs    int64
		open     float64
		close    float64
		minRange float64
		maxAgeMs int64
		want     bool
	}{
		{"幅度大且新鲜 → 流值定稿", 800, 64000, 64030, 15, 5000, false},
		{"幅度略小于阈值 → 官方修正", 800, 64000, 64014.9, 15, 5000, true},
		{"幅度等于阈值 → 流值定稿（误差 1.82 不足以翻转）", 800, 64000, 64015, 15, 5000, false},
		{"推送过旧（流冻结）→ 官方修正", 60000, 64000, 64030, 15, 5000, true},
		{"幅度大但 age 恰在阈值上 → 官方修正", 5001, 64000, 64030, 15, 5000, true},
	}
	for _, c := range cases {
		if got := NeedsOfficialCorrection(c.ageMs, c.open, c.close, c.minRange, c.maxAgeMs); got != c.want {
			t.Errorf("%s: got=%v want=%v", c.name, got, c.want)
		}
	}
}

// TestSettlementWorker_SkipCorrection 不需要修正的事件不触发官方拉取、不写修正行。
func TestSettlementWorker_SkipCorrection(t *testing.T) {
	dir := t.TempDir()
	fetchCalled := 0
	fetch := func(ctx context.Context, start time.Time) (float64, float64, bool) {
		fetchCalled++
		return 64000, 64100, true
	}
	cfg := DefaultSettlementConfig()
	cfg.PollInterval = 10 * time.Millisecond

	w := NewSettlementWorker(dir, fetch, cfg)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	w.Start(ctx)

	// start_time 用今天真实窗口起点：事件按 start_time 日归文件
	now := time.Now().UTC()
	st := time.Date(now.Year(), now.Month(), now.Day(), 0, 30, 0, 0, time.UTC).Unix()

	w.Submit(&Event{ConditionID: "cond1", Slug: "s", StartTime: st,
		TwapOpenPrice: 64000, TwapClosePrice: 64030, CloseSource: "stream"}, false)

	path := filepath.Join(dir, "events_"+time.Now().UTC().Format("2006-01-02")+".jsonl")
	deadline := time.Now().Add(2 * time.Second)
	for {
		data, err := os.ReadFile(path)
		if err == nil && len(data) > 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("事件未落盘")
		}
		time.Sleep(5 * time.Millisecond)
	}
	time.Sleep(50 * time.Millisecond) // 给足时间让潜在的修正轮询误触发
	if fetchCalled != 0 {
		t.Fatalf("不需要修正的事件不应触发官方拉取，实际调用 %d 次", fetchCalled)
	}
	data, _ := os.ReadFile(path)
	if got := strings.Count(string(data), "\n"); got != 1 {
		t.Fatalf("不应有修正行，实际 %d 行", got)
	}
}

// TestWriteCorrection_Dedupe 修正行按 start_time 去重。
func TestWriteCorrection_Dedupe(t *testing.T) {
	dir := t.TempDir()
	corr := &SettlementCorrection{
		EventType:     "settlement_correction",
		StartTime:     1000,
		TwapOpenPrice: 1, TwapClosePrice: 2,
		CloseSource: "official", Outcome: 0,
	}
	written, err := WriteCorrection(dir, corr)
	if err != nil || !written {
		t.Fatalf("首次修正应写入: written=%v err=%v", written, err)
	}
	written, err = WriteCorrection(dir, corr)
	if err != nil || written {
		t.Fatalf("重复修正应跳过: written=%v err=%v", written, err)
	}
	// 不同 start_time 可再写
	written, err = WriteCorrection(dir, &SettlementCorrection{
		EventType: "settlement_correction", StartTime: 1300,
		CloseSource: "official", Outcome: 1,
	})
	if err != nil || !written {
		t.Fatalf("不同窗口修正应写入: written=%v err=%v", written, err)
	}
}

// TestSettlementWorker_SubmitAfterExit worker 退出后 Submit 不得永久阻塞
// （ctx 取消后排空队列即退出，关闭路径的补交调用不能挂死）。
func TestSettlementWorker_SubmitAfterExit(t *testing.T) {
	dir := t.TempDir()
	w := NewSettlementWorker(dir, func(ctx context.Context, start time.Time) (float64, float64, bool) {
		return 0, 0, false
	}, DefaultSettlementConfig())
	ctx, cancel := context.WithCancel(context.Background())
	w.Start(ctx)
	cancel()
	select {
	case <-w.Done():
	case <-time.After(2 * time.Second):
		t.Fatal("worker 未在 ctx 取消后退出")
	}
	done := make(chan struct{})
	go func() {
		w.Submit(&Event{ConditionID: "c", Slug: "s", StartTime: 1000}, false)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("worker 退出后 Submit 永久阻塞")
	}
}

// TestWriteUniqueEvent_NotBlockedByCorrection 事件行去重不能被修正行误挡：
// 文件里只有修正行（无事件行）时，事件仍应写入。
func TestWriteUniqueEvent_NotBlockedByCorrection(t *testing.T) {
	dir := t.TempDir()
	if _, err := WriteCorrection(dir, &SettlementCorrection{
		EventType: "settlement_correction", StartTime: 1000,
		CloseSource: "official", Outcome: 0,
	}); err != nil {
		t.Fatalf("写修正行: %v", err)
	}
	written, err := WriteUniqueEvent(dir, &Event{ConditionID: "c", Slug: "s", StartTime: 1000})
	if err != nil || !written {
		t.Fatalf("修正行不得挡事件写入: written=%v err=%v", written, err)
	}
}
