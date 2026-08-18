// probe_close 是官方 TWAP 收盘价延迟探针（一次性诊断工具）。
//
// 逐个监测已结束的 5 分钟窗口：从窗口结束 +5s 起按固定间隔轮询
// crypto-price 接口，记录官方 open/close 首次产出的时刻，输出
// "窗口结束 → close 可获取" 的延迟分布。
//
// 用途：为采集架构的结算修正队列确定轮询预算（此前 60s 预算实测
// 142/142 全部未命中，官方收盘延迟为分钟级）。
//
// Usage:
//
//	go run ./cmd/probe_close -n 8 -interval 15s
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"flag"
	"fmt"
	"log"
	"math"
	"os"
	"os/signal"
	"syscall"
	"time"

	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

func main() {
	nWindows := flag.Int("n", 8, "监测的窗口数（每窗 5 分钟）")
	interval := flag.Duration("interval", 15*time.Second, "轮询间隔")
	maxWait := flag.Duration("max-wait", 50*time.Minute, "单窗最长轮询时间")
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	client := newClient()
	log.Println("========================================")
	log.Printf(" 官方收盘延迟探针 — %d 窗 | 轮询 %s | 单窗上限 %s", *nWindows, *interval, *maxWait)
	log.Println("========================================")

	var results []probeResult

	for i := 0; i < *nWindows; i++ {
		// 对齐到下一个完整窗口
		now := time.Now().UTC()
		start := time.Unix(now.Unix()/300*300, 0).Add(300 * time.Second).UTC()
		log.Printf("[Probe] 目标窗口 %s（边界后 %s 开始轮询）", start.Format("15:04"),
			start.Add(300*time.Second).Sub(time.Now().UTC()).Round(time.Second)+5*time.Second)

		// 等到窗口结束 +5s
		wait := time.Until(start.Add(305 * time.Second))
		if wait > 0 {
			select {
			case <-ctx.Done():
				return
			case <-time.After(wait):
			}
		}

		r := probeWindow(ctx, client, start, *interval, *maxWait)
		results = append(results, r)
	}

	// 汇总
	fmt.Println("\n=== 汇总 ===")
	var closes []time.Duration
	for _, r := range results {
		if r.closed {
			closes = append(closes, r.closeSeen)
		}
		fmt.Printf("  %s: open %s, close %s (命中=%v)\n",
			r.start.Format("15:04"), durStr(r.openSeen), durStr(r.closeSeen), r.closed)
	}
	if len(closes) > 0 {
		fmt.Printf("close 延迟: n=%d/%d 中位 %s 最小 %s 最大 %s\n",
			len(closes), len(results), durStr(median(closes)), durStr(minDur(closes)), durStr(maxDur(closes)))
	}
}

// probeResult 是单窗的官方价格产出延迟记录。
type probeResult struct {
	start     time.Time
	openSeen  time.Duration // 相对窗口结束，首次拿到官方 open
	closeSeen time.Duration // 相对窗口结束，首次拿到官方 close
	closed    bool
}

// probeWindow 从窗口结束 +5s 起轮询，直到 close 产出或 maxWait 超时。
func probeWindow(ctx context.Context, client *sdk.PolymarketClient, start time.Time,
	interval, maxWait time.Duration) (r probeResult) {
	r.start = start
	end := start.Add(300 * time.Second)
	deadline := time.Now().Add(maxWait)
	var openSeen time.Time

	for {
		if time.Now().After(deadline) || ctx.Err() != nil {
			break
		}
		o, c := client.FetchOpenPrice(sdk.BTC, start, end, sdk.Fiveminute, true, 60)
		now := time.Now()
		if o > 0 && openSeen.IsZero() {
			openSeen = now
			log.Printf("[Probe] %s 官方 open 首次产出 %.2f（窗口结束 +%.0fs）",
				start.Format("15:04"), o, now.Sub(end).Seconds())
		}
		if c > 0 {
			r.openSeen = now.Sub(end) // open 可能未单独记录，用 close 到达时刻近似
			r.closeSeen = now.Sub(end)
			r.closed = true
			log.Printf("[Probe] %s 官方 close 首次产出 %.2f（窗口结束 +%.0fs）",
				start.Format("15:04"), c, now.Sub(end).Seconds())
			break
		}
		select {
		case <-ctx.Done():
			return r
		case <-time.After(interval):
		}
	}
	if !openSeen.IsZero() {
		r.openSeen = openSeen.Sub(end)
	}
	return r
}

func newClient() *sdk.PolymarketClient {
	cfg := &sdk.Config{
		HttpTimeout: 10 * time.Second,
		Polymarket: sdk.PolymarketConfig{
			ChainID:        137,
			ClobBaseURL:    "https://clob.polymarket.com",
			ClobWSBaseURL:  "wss://ws-subscriptions-clob.polymarket.com",
			GammaBaseURL:   "https://gamma-api.polymarket.com",
			DataAPIBaseURL: "https://data-api.polymarket.com",
			LiveWSBaseURL:  "wss://ws-live-data.polymarket.com",
		},
	}
	if v := os.Getenv("POLYMARKET_OWNER_KEY"); v != "" {
		cfg.Polymarket.OwnerKey = v
	} else {
		key := make([]byte, 32)
		_, _ = rand.Read(key)
		cfg.Polymarket.OwnerKey = hex.EncodeToString(key)
	}
	return sdk.NewClient(cfg)
}

func durStr(d time.Duration) string {
	if d <= 0 {
		return "未命中"
	}
	return fmt.Sprintf("+%.0fs", d.Seconds())
}

func median(ds []time.Duration) time.Duration {
	if len(ds) == 0 {
		return 0
	}
	// 简单中位（n 小，直接排序）
	sorted := append([]time.Duration(nil), ds...)
	for i := 1; i < len(sorted); i++ {
		for j := i; j > 0 && sorted[j] < sorted[j-1]; j-- {
			sorted[j], sorted[j-1] = sorted[j-1], sorted[j]
		}
	}
	return sorted[len(sorted)/2]
}

func minDur(ds []time.Duration) time.Duration {
	m := time.Duration(math.MaxInt64)
	for _, d := range ds {
		if d < m {
			m = d
		}
	}
	return m
}

func maxDur(ds []time.Duration) time.Duration {
	m := time.Duration(0)
	for _, d := range ds {
		if d > m {
			m = d
		}
	}
	return m
}
