package collect

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// TestCompactDay_Merge 验证修正行合并进事件行：官方字段覆盖、事件其余
// 字段保留、修正行消失；孤立修正行保留在末尾；幂等（二次 compact 无变化）。
func TestCompactDay_Merge(t *testing.T) {
	dir := t.TempDir()
	now := time.Now().UTC()
	day := now.Format("2006-01-02")
	// start_time 用今天真实窗口起点：事件/修正行按 start_time 日归文件，
	// 用 1970 年代的伪造时间会落进别的日期文件
	midnight := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, time.UTC)
	st1 := midnight.Add(30 * time.Minute).Unix()
	st2 := midnight.Add(35 * time.Minute).Unix()
	stOrphan := midnight.Add(40 * time.Minute).Unix()

	// 两个事件 + 窗口 1 的修正 + 一个孤立修正（无对应事件）
	ev1 := &Event{ConditionID: "c1", Slug: "s1", StartTime: st1,
		TwapOpenPrice: 64000, TwapClosePrice: 64030, CloseSource: "stream", Outcome: 0,
		Ticks: []HFTick{{Ts: st1*1000 + 1000, Rem: 299}}}
	ev2 := &Event{ConditionID: "c2", Slug: "s2", StartTime: st2,
		TwapOpenPrice: 64030, TwapClosePrice: 64010, CloseSource: "stream", Outcome: 1}
	if _, err := WriteUniqueEvent(dir, ev1); err != nil {
		t.Fatalf("写事件1: %v", err)
	}
	if _, err := WriteUniqueEvent(dir, ev2); err != nil {
		t.Fatalf("写事件2: %v", err)
	}
	if _, err := WriteCorrection(dir, &SettlementCorrection{
		EventType: "settlement_correction", StartTime: st1,
		TwapOpenPrice: 64001, TwapClosePrice: 64041,
		CloseSource: "official", Outcome: 0,
	}); err != nil {
		t.Fatalf("写修正: %v", err)
	}
	if _, err := WriteCorrection(dir, &SettlementCorrection{
		EventType: "settlement_correction", StartTime: stOrphan, // 孤立
		TwapOpenPrice: 1, TwapClosePrice: 2, CloseSource: "official", Outcome: 1,
	}); err != nil {
		t.Fatalf("写孤立修正: %v", err)
	}

	merged, orphan, err := CompactDay(dir, day)
	if err != nil {
		t.Fatalf("compact: %v", err)
	}
	if merged != 1 || orphan != 1 {
		t.Fatalf("期望 merged=1 orphan=1，实际 merged=%d orphan=%d", merged, orphan)
	}

	path := filepath.Join(dir, "events_"+day+".jsonl")
	data, _ := os.ReadFile(path)
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) != 3 { // 2 事件 + 1 孤立修正
		t.Fatalf("期望 3 行（2 事件 + 1 孤立修正），实际 %d", len(lines))
	}

	var e1 Event
	if err := json.Unmarshal([]byte(lines[0]), &e1); err != nil {
		t.Fatalf("事件1 解析失败: %v", err)
	}
	if e1.StartTime != st1 || e1.TwapOpenPrice != 64001 || e1.TwapClosePrice != 64041 ||
		e1.CloseSource != "official" || e1.Outcome != 0 {
		t.Fatalf("事件1 修正未合并: %+v", e1)
	}
	if len(e1.Ticks) != 1 || e1.Ticks[0].Ts != st1*1000+1000 {
		t.Fatalf("事件1 原有字段应保留: %+v", e1)
	}

	var e2 Event
	if err := json.Unmarshal([]byte(lines[1]), &e2); err != nil {
		t.Fatalf("事件2 解析失败: %v", err)
	}
	if e2.StartTime != st2 || e2.CloseSource != "stream" {
		t.Fatalf("事件2 不应被修改: %+v", e2)
	}

	var orphanLine SettlementCorrection
	if err := json.Unmarshal([]byte(lines[2]), &orphanLine); err != nil {
		t.Fatalf("孤立修正解析失败: %v", err)
	}
	if orphanLine.StartTime != stOrphan {
		t.Fatalf("孤立修正应保留: %+v", orphanLine)
	}

	// 幂等：二次 compact 结果不变
	merged2, _, err := CompactDay(dir, day)
	if err != nil || merged2 != 0 {
		t.Fatalf("二次 compact 应无变化: merged=%d err=%v", merged2, err)
	}
}

// TestCompactDay_Empty 当日无数据文件时返回零值不报错，且不留下 .lock 垃圾文件。
func TestCompactDay_Empty(t *testing.T) {
	dir := t.TempDir()
	merged, orphan, err := CompactDay(dir, "2020-01-01")
	if err != nil || merged != 0 || orphan != 0 {
		t.Fatalf("空目录应返回零值: merged=%d orphan=%d err=%v", merged, orphan, err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("readdir: %v", err)
	}
	if len(entries) != 0 {
		t.Fatalf("无数据文件的 compact 不应留下任何文件: %v", entries)
	}
}
