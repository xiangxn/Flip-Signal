package collect

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
)

// LoadWrittenStartTimes 扫描目录下 events_*.jsonl 已记录的窗口起点集合，
// 供写入前去重：程序在窗口中途重启会再次采集同一窗口（append 模式不覆盖），
// 重复事件会污染分析数据。
//
// 每日文件仅 ~288 行，启动时全量扫描开销可忽略。
func LoadWrittenStartTimes(dir string) map[int64]bool {
	seen := make(map[int64]bool)
	paths, err := filepath.Glob(filepath.Join(dir, "events_*.jsonl"))
	if err != nil {
		return seen
	}
	sort.Strings(paths)
	for _, path := range paths {
		f, err := os.Open(path)
		if err != nil {
			continue
		}
		dec := json.NewDecoder(f)
		for dec.More() {
			var rec struct {
				StartTime int64 `json:"start_time"`
			}
			if err := dec.Decode(&rec); err != nil {
				break
			}
			if rec.StartTime > 0 {
				seen[rec.StartTime] = true
			}
		}
		f.Close()
	}
	return seen
}
