package collect

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"time"
)

// WriteUniqueEvent 将事件以 start_time 去重后追加到当日文件（跨进程安全）。
//
// 在 flock 独占锁内「扫描文件已有 start_time → 不存在才追加」，防止
// 程序重启（sequential）或多实例并发（concurrent）对同一窗口重复写入——
// append 模式不会覆盖，重复事件会污染分析数据。
//
// 返回 written=true 表示已写入；false 表示该窗口已存在被跳过。
func WriteUniqueEvent(dir string, ev *Event) (written bool, err error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return false, fmt.Errorf("create output dir: %w", err)
	}
	day := time.Now().UTC().Format("2006-01-02")
	path := filepath.Join(dir, fmt.Sprintf("events_%s.jsonl", day))

	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR|os.O_APPEND, 0o644)
	if err != nil {
		return false, fmt.Errorf("open %s: %w", path, err)
	}
	defer f.Close()

	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
		return false, fmt.Errorf("flock: %w", err)
	}
	defer syscall.Flock(int(f.Fd()), syscall.LOCK_UN)

	// 锁内扫描查重。⚠️ v2 高频格式每窗口一行（ticks+trades 全量内联），
	// 单行实测 ~300KB，远超 bufio.Scanner 默认上限 64KB（token too long），
	// 必须显式调大 buffer —— 否则首个窗口后所有写入失败。
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 16<<20)
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		var rec struct {
			StartTime int64 `json:"start_time"`
		}
		if json.Unmarshal(line, &rec) == nil && rec.StartTime == ev.StartTime {
			return false, nil // 已存在，跳过
		}
	}
	if err := scanner.Err(); err != nil {
		return false, fmt.Errorf("scan %s: %w", path, err)
	}

	data, err := json.Marshal(ev)
	if err != nil {
		return false, fmt.Errorf("marshal event: %w", err)
	}
	data = append(data, '\n')
	if _, err := f.Write(data); err != nil {
		return false, fmt.Errorf("append: %w", err)
	}
	return true, nil
}
