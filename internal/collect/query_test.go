package collect

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// bigEventRow 造一条**序列化后确实超过** bufio.Scanner 默认上限 64KB 的事件行
// （500 ticks × ~300 字节 ≈ 150KB）。回读函数与写侧同一颗雷，必须覆盖。
func bigEventRow(startTime int64) *Event {
	ev := &Event{ConditionID: "cond-big", Slug: "btc-updown-5m-big", StartTime: startTime}
	for i := 0; i < 500; i++ {
		ev.Ticks = append(ev.Ticks, HFTick{
			Ts:   int64(i * 1000),
			Rem:  299 - i/2,
			Bin:  BinTick{Price: 1.2345, BuyVol: 6.7, SellVol: 8.9, Ticks: 12},
			PM:   PMTick{YesBid: 0.5, YesAsk: 0.51, NoBid: 0.49, NoAsk: 0.5},
			Twap: TwapTick{Price: 12345.6, AgeMs: 2000},
		})
	}
	return ev
}

// appendRaw 直接往文件追加一行 JSON（不走 WriteUniqueEvent 的 flock/去重——
// 这里要精确控制行的顺序与种类）。
func appendRaw(t *testing.T, path string, v any) {
	t.Helper()
	data, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		t.Fatalf("open %s: %v", path, err)
	}
	defer f.Close()
	if _, err := f.Write(append(data, '\n')); err != nil {
		t.Fatalf("write: %v", err)
	}
}

// TestLoadEventByStart 回读的正面路径：跨午夜窗（23:55 起）整行落在**起点日**的文件里；
// 大行（>64KB）不炸；同窗的修正行**排在前**也不被误当成事件行。
func TestLoadEventByStart(t *testing.T) {
	dir := t.TempDir()
	// 跨午夜: 起点 09-30 23:55 UTC, 闭市已是 10-01 —— 归日认**起点**
	start := time.Date(2026, 9, 30, 23, 55, 0, 0, time.UTC).Unix()
	if day := DayForStart(start); day != "2026-09-30" {
		t.Fatalf("DayForStart(跨午夜窗) = %s, 期望起点日 2026-09-30", day)
	}
	path := filepath.Join(dir, "events_2026-09-30.jsonl")

	// 另一个窗的大行（同一天, 排在前面）——回读必须能跨过它
	appendRaw(t, path, bigEventRow(start+300))
	// 同窗的官方修正行（SettlementWorker 会追加在事件行**之前**吗? 顺序不定, 这里
	// 特意排在前）——它与事件行同 start_time, 但没有 ticks, 不能被当成事件返回
	appendRaw(t, path, &SettlementCorrection{
		EventType: "settlement_correction", StartTime: start, TwapOpenPrice: 1, TwapClosePrice: 2,
	})
	target := &Event{
		ConditionID: "cond-target", Slug: "btc-updown-5m-2026-09-30-2355", StartTime: start,
		TwapOpenPrice: 85413.367, TwapClosePrice: 85450.1, CloseSource: SourcePush,
		AnchorSource: SourcePush, Outcome: 1, BinanceOpen: 85420.5,
		Ticks: []HFTick{{Ts: start * 1000, Rem: 299, Bin: BinTick{Price: 85430}}},
	}
	appendRaw(t, path, target)

	ev, ok, err := LoadEventByStart(dir, start)
	if err != nil || !ok {
		t.Fatalf("回读 = (%v, %v, %v), 期望命中", ev, ok, err)
	}
	if ev.ConditionID != "cond-target" || ev.TwapOpenPrice != 85413.367 || len(ev.Ticks) != 1 {
		t.Fatalf("回读到的是别的行: %+v（ticks=%d）", ev, len(ev.Ticks))
	}
	if ev.AnchorSource != SourcePush {
		t.Fatalf("AnchorSource = %q, 期望 %q", ev.AnchorSource, SourcePush)
	}
}

// TestLoadEventByStartMisses 查不到的各种情形都返回 (nil, false, nil)——「没有」不是错误,
// 调用方按「无原始采集」向用户解释。
func TestLoadEventByStartMisses(t *testing.T) {
	dir := t.TempDir()
	start := time.Date(2026, 9, 28, 12, 0, 0, 0, time.UTC).Unix()
	path := filepath.Join(dir, "events_2026-09-28.jsonl")
	appendRaw(t, path, &Event{ConditionID: "other", StartTime: start + 300})

	cases := []struct {
		name      string
		dir       string
		startTime int64
	}{
		{"目录为空串（events_dir 未配置）", "", start},
		{"start_time 非法", dir, 0},
		{"当天文件不存在", dir, start - 24*3600},
		{"文件在但该窗不在（含被落盘红线丢弃的窗）", dir, start},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			ev, ok, err := LoadEventByStart(c.dir, c.startTime)
			if ev != nil || ok || err != nil {
				t.Fatalf("= (%v, %v, %v), 期望 (nil, false, nil)", ev, ok, err)
			}
		})
	}
}

// TestLoadEventByStartSkipsCorrection 只有修正行（该窗的推送口径被官方覆盖过、
// 事件行后来被 compact 合并掉了——或压根没写过）时**不算命中**：修正行没有 ticks,
// 曲线无从画起。
func TestLoadEventByStartSkipsCorrection(t *testing.T) {
	dir := t.TempDir()
	start := time.Date(2026, 9, 29, 8, 0, 0, 0, time.UTC).Unix()
	appendRaw(t, filepath.Join(dir, "events_2026-09-29.jsonl"), &SettlementCorrection{
		EventType: "settlement_correction", StartTime: start,
	})
	if ev, ok, err := LoadEventByStart(dir, start); ev != nil || ok || err != nil {
		t.Fatalf("= (%v, %v, %v), 期望 (nil, false, nil)", ev, ok, err)
	}
}

// TestLoadEventByStartPrefixTrap 前缀陷阱: 找 1000 不能命中 10001。命中错的窗会返回
// **隔壁窗口**的整条事件——曲线画的是另一场，而页面上看不出来。
func TestLoadEventByStartPrefixTrap(t *testing.T) {
	dir := t.TempDir()
	const near, far = 1000, 10001 // 同一 UTC 日（1970-01-01）
	path := filepath.Join(dir, "events_1970-01-01.jsonl")
	appendRaw(t, path, &Event{ConditionID: "far", StartTime: far})

	if ev, ok, _ := LoadEventByStart(dir, near); ok {
		t.Fatalf("1000 命中了 %+v（应是 10001 被前缀吃掉）", ev)
	}
	ev, ok, err := LoadEventByStart(dir, far)
	if err != nil || !ok || ev.ConditionID != "far" {
		t.Fatalf("10001 回读 = (%v, %v, %v), 期望命中 far", ev, ok, err)
	}
}
