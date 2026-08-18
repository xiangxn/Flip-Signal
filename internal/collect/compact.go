package collect

import (
	"bufio"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"syscall"
)

// CompactDay 将指定 UTC 日的 settlement_correction 修正行合并进事件行，
// 原子重写数据文件（temp+rename，全程持有 .lock 锁文件）。
//
// 合并语义：修正行覆盖事件行的 twap_open_price / twap_close_price /
// close_source / outcome 四个字段（官方结算口径），其余字段不变。
// 无对应事件行的孤立修正行保留在文件末尾（不丢数据）。
// 与采集进程可安全并发：追加与重写经 .lock 锁文件互斥。
//
// 返回合并修正数（merged）与孤立修正数（orphan）。
func CompactDay(dir, day string) (merged, orphan int, err error) {
	path := filepath.Join(dir, fmt.Sprintf("events_%s.jsonl", day))

	lock, err := os.OpenFile(path+".lock", os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return 0, 0, fmt.Errorf("open lock: %w", err)
	}
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX); err != nil {
		_ = lock.Close()
		return 0, 0, fmt.Errorf("flock: %w", err)
	}
	defer func() {
		_ = syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
		_ = lock.Close()
	}()

	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return 0, 0, nil // 当日无数据文件，无事可做
		}
		return 0, 0, fmt.Errorf("open %s: %w", path, err)
	}

	// 逐行读取：事件行保序收集，修正行按 start_time 索引
	type eventLine struct {
		startTime int64
		obj       map[string]any
	}
	var events []eventLine
	corrections := map[int64]map[string]any{}
	orphanCorrections := [][]byte{}

	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 16<<20)
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		var head lineHead
		if json.Unmarshal(line, &head) != nil {
			// 无法解析的行原样保留（不丢数据）
			orphanCorrections = append(orphanCorrections, append([]byte(nil), line...))
			continue
		}
		if head.EventType == "settlement_correction" {
			var obj map[string]any
			if json.Unmarshal(line, &obj) != nil {
				orphanCorrections = append(orphanCorrections, append([]byte(nil), line...))
				continue
			}
			corrections[head.StartTime] = obj
			continue
		}
		var obj map[string]any
		if json.Unmarshal(line, &obj) != nil {
			orphanCorrections = append(orphanCorrections, append([]byte(nil), line...))
			continue
		}
		events = append(events, eventLine{startTime: head.StartTime, obj: obj})
	}
	if err := scanner.Err(); err != nil {
		_ = f.Close()
		return 0, 0, fmt.Errorf("scan %s: %w", path, err)
	}
	_ = f.Close()

	// 合并：修正行覆盖官方结算字段
	for i := range events {
		corr, ok := corrections[events[i].startTime]
		if !ok {
			continue
		}
		for _, k := range []string{"twap_open_price", "twap_close_price", "close_source", "outcome"} {
			if v, exists := corr[k]; exists {
				events[i].obj[k] = v
			}
		}
		delete(corrections, events[i].startTime)
		merged++
	}
	// 剩余修正行 = 无对应事件行的孤立行
	orphan = len(corrections)
	for _, obj := range corrections {
		data, _ := json.Marshal(obj)
		orphanCorrections = append(orphanCorrections, data)
	}

	// 原子重写：tmp + fsync + rename
	tmp, err := os.CreateTemp(dir, fmt.Sprintf("events_%s.jsonl.tmp.*", day))
	if err != nil {
		return 0, 0, fmt.Errorf("create tmp: %w", err)
	}
	tmpName := tmp.Name()
	defer func() {
		if tmp != nil {
			_ = tmp.Close()
			_ = os.Remove(tmpName)
		}
	}()

	w := bufio.NewWriter(tmp)
	for _, e := range events {
		data, err := json.Marshal(e.obj)
		if err != nil {
			return 0, 0, fmt.Errorf("marshal event: %w", err)
		}
		data = append(data, '\n')
		if _, err := w.Write(data); err != nil {
			return 0, 0, fmt.Errorf("write tmp: %w", err)
		}
	}
	for _, line := range orphanCorrections {
		if _, err := w.Write(append(line, '\n')); err != nil {
			return 0, 0, fmt.Errorf("write tmp: %w", err)
		}
	}
	if err := w.Flush(); err != nil {
		return 0, 0, fmt.Errorf("flush tmp: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		return 0, 0, fmt.Errorf("sync tmp: %w", err)
	}
	if err := tmp.Close(); err != nil {
		tmp = nil
		return 0, 0, fmt.Errorf("close tmp: %w", err)
	}
	tmp = nil
	if err := os.Rename(tmpName, path); err != nil {
		return 0, 0, fmt.Errorf("rename: %w", err)
	}
	log.Printf("[Compact] %s 完成: 事件 %d 合并修正 %d 孤立修正 %d", day, len(events), merged, orphan)
	return merged, orphan, nil
}
