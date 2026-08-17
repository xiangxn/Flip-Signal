package collect

import (
	// 标准库
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// TestWriteUniqueEvent_OversizedLine 回归测试：v2 高频格式单行远超
// bufio.Scanner 默认 64KB 上限（曾导致首个窗口后所有写入 token too long），
// 验证大行存在时查重扫描与追加仍正常。
func TestWriteUniqueEvent_OversizedLine(t *testing.T) {
	dir := t.TempDir()

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
				Ts:  int64(i * 1000),
				Rem: 299 - i/2,
				Bin: BinTick{Price: 1.2345, BuyVol: 6.7, SellVol: 8.9, Ticks: 12},
				PM:  PMTick{YesBid: 0.5, YesAsk: 0.51, NoBid: 0.49, NoAsk: 0.5},
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
	if written, err := WriteUniqueEvent(dir, makeEvent(1)); err != nil || !written {
		t.Fatalf("首个事件写入失败: written=%v err=%v", written, err)
	}
	// 第二个事件写入 —— 需扫描已有的大行，旧代码在此报 token too long
	if written, err := WriteUniqueEvent(dir, makeEvent(2)); err != nil || !written {
		t.Fatalf("第二个事件写入失败: written=%v err=%v", written, err)
	}
	// 重复事件被去重跳过
	if written, err := WriteUniqueEvent(dir, makeEvent(1)); err != nil || written {
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
