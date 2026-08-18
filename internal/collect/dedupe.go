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

// openLocked 以追加模式打开当日数据文件，并持有当日锁文件的 flock 独占锁。
//
// ⚠️ 锁落在独立的 .lock 文件上而非数据文件本身：compact 通过 rename
// 原子替换数据文件（temp+rename），rename 会换掉数据文件的 inode 而锁
// 仍留在旧 inode 上——锁文件不会被 rename 替换，锁身份稳定，追加/重写
// 才互斥。返回数据文件句柄与释放函数（先解锁再关闭）。
func openLocked(path string) (*os.File, func(), error) {
	lock, err := os.OpenFile(path+".lock", os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return nil, nil, fmt.Errorf("open lock %s: %w", path+".lock", err)
	}
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX); err != nil {
		_ = lock.Close()
		return nil, nil, fmt.Errorf("flock: %w", err)
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR|os.O_APPEND, 0o644)
	if err != nil {
		_ = syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
		_ = lock.Close()
		return nil, nil, fmt.Errorf("open %s: %w", path, err)
	}
	release := func() {
		_ = syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
		_ = lock.Close()
		_ = f.Close()
	}
	return f, release, nil
}

// lineHead 是 JSONL 行的类型头：event_type 为空 = 事件行，
// "settlement_correction" = 结算修正行。
type lineHead struct {
	EventType string `json:"event_type"`
	StartTime int64  `json:"start_time"`
}

// scanLines 扫描文件全部行并逐个解析类型头，跳过空行。
func scanLines(f *os.File) ([]lineHead, error) {
	// ⚠️ v2 高频格式每窗口一行（ticks+trades 全量内联），单行实测
	// ~300KB，远超 bufio.Scanner 默认上限 64KB（token too long），
	// 必须显式调大 buffer。
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 16<<20)
	var heads []lineHead
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		var rec lineHead
		if json.Unmarshal(line, &rec) == nil {
			heads = append(heads, rec)
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("scan: %w", err)
	}
	return heads, nil
}

// appendLine 追加一行 JSON（调用方须已持有 flock）。
func appendLine(f *os.File, v any) error {
	data, err := json.Marshal(v)
	if err != nil {
		return fmt.Errorf("marshal record: %w", err)
	}
	data = append(data, '\n')
	if _, err := f.Write(data); err != nil {
		return fmt.Errorf("append: %w", err)
	}
	return nil
}

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

	f, release, err := openLocked(path)
	if err != nil {
		return false, err
	}
	defer release()

	heads, err := scanLines(f)
	if err != nil {
		return false, fmt.Errorf("scan %s: %w", path, err)
	}
	// 只对事件行（event_type 为空）去重；修正行同样携带 start_time，
	// 误匹配会让重启后的事件被修正行挡下而丢失。
	for _, h := range heads {
		if h.EventType == "" && h.StartTime == ev.StartTime {
			return false, nil // 已存在，跳过
		}
	}
	if err := appendLine(f, ev); err != nil {
		return false, err
	}
	return true, nil
}

// WriteCorrection 追加一条官方结算修正行（flock 串行、跨进程安全）。
//
// 同一窗口已存在修正行时跳过（written=false）。修正行与事件行同文件，
// 分析侧按 start_time 合并覆盖事件行的流值口径字段。
func WriteCorrection(dir string, corr *SettlementCorrection) (written bool, err error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return false, fmt.Errorf("create output dir: %w", err)
	}
	day := time.Now().UTC().Format("2006-01-02")
	path := filepath.Join(dir, fmt.Sprintf("events_%s.jsonl", day))

	f, release, err := openLocked(path)
	if err != nil {
		return false, err
	}
	defer release()

	heads, err := scanLines(f)
	if err != nil {
		return false, fmt.Errorf("scan %s: %w", path, err)
	}
	for _, h := range heads {
		if h.EventType == "settlement_correction" && h.StartTime == corr.StartTime {
			return false, nil // 已修正过，跳过
		}
	}
	if err := appendLine(f, corr); err != nil {
		return false, err
	}
	return true, nil
}
