// twap_now 是 TWAP 快速查询工具：打印当前 5 分钟周期及最近几窗的
// 官方 TWAP 开/收盘价（crypto-price 接口，btc-updown-5m 结算同源口径）。
//
// 当前进行中窗口：openPrice 在边界后约 10-90s 内产出（实测 87s 已就绪），
// closePrice 需窗口结束后才产出（分钟级延迟，见 cmd/probe_close 实测）。
//
// Usage:
//
//	go run ./cmd/twap_now -n 4
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"flag"
	"fmt"
	"os"
	"time"

	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

func main() {
	lookback := flag.Int("n", 4, "回溯显示的窗口数（含当前进行中窗口）")
	timeout := flag.Duration("timeout", 8*time.Second, "单窗口查询超时")
	flag.Parse()

	cfg := newConfig()
	client := sdk.NewClient(cfg)

	now := time.Now().UTC()
	curStart := time.Unix(now.Unix()/300*300, 0).UTC()
	fmt.Printf("当前时刻: %s CST\n", time.Now().Format("15:04:05"))
	fmt.Printf("当前窗口: %s - %s（已进行 %s）\n",
		curStart.Add(8*time.Hour).Format("15:04"), curStart.Add(8*time.Hour+5*time.Minute).Format("15:04"),
		time.Since(curStart).Round(time.Second))
	fmt.Println("----------------------------------------------------------")
	fmt.Printf("%-14s %-14s %-14s %-8s %s\n", "窗口(CST)", "官方open", "官方close", "状态", "范围")

	for i := *lookback - 1; i >= 0; i-- {
		start := curStart.Add(-time.Duration(i) * 300 * time.Second)
		end := start.Add(300 * time.Second)
		ctx, cancel := context.WithTimeout(context.Background(), *timeout)
		o, c := client.FetchOpenPriceContext(ctx, sdk.BTC, start, end, sdk.Fiveminute, true, 60)
		cancel()

		status := ""
		switch {
		case o > 0 && c > 0:
			status = "已结算"
		case o > 0:
			status = "进行中(open就绪)"
		default:
			status = "open未产出"
		}
		label := start.Add(8 * time.Hour).Format("15:04")
		if i == 0 {
			label += " ← 当前"
		}
		oStr, cStr := "—", "—"
		if o > 0 {
			oStr = fmt.Sprintf("%.1f", o)
		}
		if c > 0 {
			cStr = fmt.Sprintf("%.1f", c)
		}
		rng := "—"
		if o > 0 && c > 0 {
			rng = fmt.Sprintf("%+.1f", c-o)
		}
		fmt.Printf("%-16s %-14s %-14s %-10s %s\n", label, oStr, cStr, status, rng)
	}
}

// newConfig 构造只读客户端配置（复用 cmd/collect 的环境变量约定，
// 未配置私钥则自动生成临时密钥）。
func newConfig() *sdk.Config {
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
	if v := envOrEmpty("POLYMARKET_OWNER_KEY"); v != "" {
		cfg.Polymarket.OwnerKey = v
	} else {
		key := make([]byte, 32)
		_, _ = rand.Read(key)
		cfg.Polymarket.OwnerKey = hex.EncodeToString(key)
	}
	return cfg
}

func envOrEmpty(k string) string {
	v := os.Getenv(k)
	return v
}
