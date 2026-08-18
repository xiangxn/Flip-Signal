// compact 是日终 compact 工具：把指定 UTC 日的 settlement_correction
// 修正行合并进事件行（官方结算口径覆盖流值口径），原子重写数据文件。
//
// 与采集进程可安全并发（.lock 锁文件互斥），可随时重跑（幂等）。
// 默认处理昨天的数据文件（"日终"语义，今天的文件采集仍在追加）；
// -day 指定日期（YYYY-MM-DD, UTC），-all 处理全部文件。
//
// Usage:
//
//	go run ./cmd/compact -output data/btc            # 昨天
//	go run ./cmd/compact -output data/btc -all       # 全部
package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"strings"
	"time"

	"github.com/necklace/flip-signal/internal/collect"
)

func main() {
	outputDir := flag.String("output", "data/btc", "事件 JSONL 输出目录")
	day := flag.String("day", "", "指定日期 YYYY-MM-DD（UTC）；默认昨天")
	all := flag.Bool("all", false, "compact 目录下全部 events_*.jsonl")
	flag.Parse()

	if *all {
		entries, err := os.ReadDir(*outputDir)
		if err != nil {
			log.Fatalf("读取目录失败: %v", err)
		}
		for _, e := range entries {
			name := e.Name()
			if !strings.HasPrefix(name, "events_") || !strings.HasSuffix(name, ".jsonl") {
				continue
			}
			d := strings.TrimSuffix(strings.TrimPrefix(name, "events_"), ".jsonl")
			run(*outputDir, d)
		}
		return
	}

	d := *day
	if d == "" {
		d = time.Now().UTC().Add(-24 * time.Hour).Format("2006-01-02")
	}
	run(*outputDir, d)
}

func run(dir, day string) {
	merged, orphan, err := collect.CompactDay(dir, day)
	if err != nil {
		log.Printf("[Compact] %s 失败: %v", day, err)
		return
	}
	fmt.Printf("%s: 合并修正 %d 行, 孤立修正 %d 行\n", day, merged, orphan)
}
