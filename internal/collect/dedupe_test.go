package collect

import (
	// 标准库
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// TestWriteUniqueEvent_OversizedLine 回归测试：v2 高频格式单行远超
// bufio.Scanner 默认 64KB 上限（曾导致首个窗口后所有写入 token too long），
// 验证大行存在时查重扫描与追加仍正常。
func TestWriteUniqueEvent_OversizedLine(t *testing.T) {
	dir := t.TempDir()

	// start_time 用今天真实窗口起点：事件按 start_time 日归文件，
	// 伪造的 1970 时间会落进别的日期文件
	now := time.Now().UTC()
	midnight := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, time.UTC)
	st1 := midnight.Add(30 * time.Minute).Unix()
	st2 := midnight.Add(35 * time.Minute).Unix()

	makeEvent := func(startTime int64) *Event {
		ev := &Event{
			ConditionID: "cond",
			Slug:        "btc-updown-5m-1",
			StartTime:   startTime,
			Outcome:     1,
		}
		// 500 ticks × ~300 字节 ≈ 150KB，确保序列化行超过 64KB
		for i := 0; i < 500; i++ {
			ev.Ticks = append(ev.Ticks, HFTick{
				Ts:   int64(i * 1000),
				Rem:  299 - i/2,
				Bin:  BinTick{Price: 1.2345, BuyVol: 6.7, SellVol: 8.9, Ticks: 12},
				PM:   PMTick{UpBid: 0.5, UpAsk: 0.51, DownBid: 0.49, DownAsk: 0.5},
				Twap: TwapTick{Price: 12345.6, AgeMs: 2000},
			})
		}
		return ev
	}

	// 前置断言：测试构造的行确实超过默认上限，防止测试失效
	data, err := json.Marshal(makeEvent(1))
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if len(data) <= 64*1024 {
		t.Fatalf("测试事件行 %d 字节未超过默认上限 64KB，无法覆盖回归场景", len(data))
	}

	// 第一个事件写入
	if written, err := WriteUniqueEvent(dir, makeEvent(st1)); err != nil || !written {
		t.Fatalf("首个事件写入失败: written=%v err=%v", written, err)
	}
	// 第二个事件写入 —— 需扫描已有的大行，旧代码在此报 token too long
	if written, err := WriteUniqueEvent(dir, makeEvent(st2)); err != nil || !written {
		t.Fatalf("第二个事件写入失败: written=%v err=%v", written, err)
	}
	// 重复事件被去重跳过
	if written, err := WriteUniqueEvent(dir, makeEvent(st1)); err != nil || written {
		t.Fatalf("重复事件应跳过: written=%v err=%v", written, err)
	}

	// 文件最终恰好 2 行，且行内容可解析（同日命名与 WriteUniqueEvent 一致）
	path := filepath.Join(dir, "events_"+time.Now().UTC().Format("2006-01-02")+".jsonl")
	f, err := os.Open(path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 16<<20)
	lines := 0
	for scanner.Scan() {
		var rec struct {
			StartTime int64 `json:"start_time"`
		}
		if err := json.Unmarshal(scanner.Bytes(), &rec); err != nil {
			t.Fatalf("第 %d 行解析失败: %v", lines+1, err)
		}
		lines++
	}
	if err := scanner.Err(); err != nil {
		t.Fatalf("scan: %v", err)
	}
	if lines != 2 {
		t.Fatalf("期望 2 行，实际 %d", lines)
	}
}

// TestWriteDayAttribution_CrossMidnight 回归测试：事件与修正行按窗口起点
// 日归文件。跨午夜窗口（23:55 的事件在窗口末落盘、官方修正分钟级延迟
// 到达）若按写入时刻归日，事件与修正行会落进不同文件，compact 永远
// 合并不上——两者必须同归窗口起点日的文件。
func TestWriteDayAttribution_CrossMidnight(t *testing.T) {
	dir := t.TempDir()
	now := time.Now().UTC()
	yday := now.Add(-24 * time.Hour)
	yMidnight := time.Date(yday.Year(), yday.Month(), yday.Day(), 0, 0, 0, 0, time.UTC)
	st := yMidnight.Add(23*time.Hour + 55*time.Minute).Unix()

	if _, err := WriteUniqueEvent(dir, &Event{ConditionID: "c", Slug: "s", StartTime: st}); err != nil {
		t.Fatalf("写事件: %v", err)
	}
	if _, err := WriteCorrection(dir, &SettlementCorrection{
		EventType: "settlement_correction", StartTime: st,
		TwapOpenPrice: 1, TwapClosePrice: 2, CloseSource: "official",
	}); err != nil {
		t.Fatalf("写修正: %v", err)
	}

	path := filepath.Join(dir, "events_"+yday.Format("2006-01-02")+".jsonl")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("事件与修正应写入窗口起点日文件 %s: %v", path, err)
	}
	if got := strings.Count(string(data), "\n"); got != 2 {
		t.Fatalf("期望 2 行（事件+修正同文件），实际 %d", got)
	}
	// 修正行不得写进今天的文件
	todayPath := filepath.Join(dir, "events_"+now.Format("2006-01-02")+".jsonl")
	if _, err := os.Stat(todayPath); !os.IsNotExist(err) {
		t.Fatalf("跨午夜修正行不应写入今天文件 %s", todayPath)
	}
}
