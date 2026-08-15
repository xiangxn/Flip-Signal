// lab 是特征研究实验室的数据采集器。
//
// 连接 Chainlink TWAP-60 推送（btc-updown-5m 结算口径）、Binance
// WebSocket (aggTrade + depth20，研究对照) 和 Polymarket CLOB
// WebSocket (order books)，以可配置的采样间隔生成 ResearchSnapshot，
// 按 Polymarket conditionId 归类为 5 分钟 Event，并以 JSONL 格式写入磁盘。
// 官方 TWAP 开/收盘价经 crypto-price 接口获取（开盘价从窗口边界起后台
// 轮询、官方到达后修正 snapshot，不阻塞采集；收盘价窗口结束后轮询修正）。
//
// Usage:
//
//	go run ./cmd/lab -output data/btc -symbol BTCUSDT
//	go run ./cmd/lab -output data/btc_1s -interval 1   # 1 秒采样
//
// 默认只读模式 —— 未配置私钥时自动生成临时钱包，
// 仅用于 Polymarket API 数据读取，不进行任何交易。
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/tidwall/gjson"
	"github.com/xiangxn/go-polymarket-sdk/model"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"

	"github.com/necklace/flip-signal/internal/feed"
	"github.com/necklace/flip-signal/internal/lab"
)

func init() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
}

func main() {
	outputDir := flag.String("output", "data/btc", "事件 JSONL 输出目录")
	symbol := flag.String("symbol", "BTCUSDT", "Binance 交易对")
	slugPrefix := flag.String("slug", "btc-updown-5m", "Polymarket slug 前缀")
	tickInterval := flag.Int("interval", lab.DefaultTickIntervalSec, "采样间隔（秒），典型值 1 或 5")
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// ── Polymarket 客户端（未配置私钥则自动生成临时密钥，只读模式）──
	cfg := loadConfig()
	readOnly := false
	if cfg.SDK.Polymarket.OwnerKey == "" {
		key := make([]byte, 32)
		if _, err := rand.Read(key); err != nil {
			log.Fatalf("生成临时密钥失败: %v", err)
		}
		cfg.SDK.Polymarket.OwnerKey = hex.EncodeToString(key)
		readOnly = true
	}

	client := sdk.NewClient(&cfg.SDK)
	if readOnly {
		log.Println("[Lab] ⚠️  未配置 POLYMARKET_OWNER_KEY —— 只读模式运行")
	}

	// ── Binance 适配器 ──
	binanceCfg := feed.BinanceConfig{
		Symbol: *symbol,
		// StreamBaseURL: "wss://stream.binance.com:9443",
		StreamBaseURL: "wss://data-stream.binance.vision",
		RestBaseURL:   "https://data-api.binance.vision",
	}
	binance := feed.NewBinanceAdapterWithConfig(binanceCfg)
	go func() {
		if err := binance.Start(ctx); err != nil {
			log.Printf("[Lab] Binance 启动失败: %v", err)
		}
	}()

	// ── Polymarket 订单簿适配器 ──
	bookAdapter := feed.NewOrderBookAdapter(
		cfg.SDK.Polymarket.ClobWSBaseURL, client,
	)
	bookAdapter.Start(ctx)

	// ── Chainlink TWAP-60 适配器（btc-updown-5m 结算口径基准价格）──
	twapMonitor := sdk.NewCryptoPriceMonitor(client, sdk.MonitorChainlinkTwap, "BTC_60")
	twapAdapter := feed.NewTwapAdapter(twapMonitor, "BTC", sdk.ChainlinkTwapWindowSixty)
	twapAdapter.Start(ctx)
	go func() {
		for {
			err := twapMonitor.Run(ctx)
			if err == nil || ctx.Err() != nil {
				return
			}
			// Run 退出 = WS 重连耗尽（SDK 内部 20 次重试后放弃），重启恢复。
			log.Printf("[Twap] ⚠️ monitor 异常退出: %v —— 5 秒后重启", err)
			select {
			case <-ctx.Done():
				return
			case <-time.After(5 * time.Second):
			}
		}
	}()

	// 订单簿追踪：goroutine 写入，tick 循环加 RLock 读取
	var (
		bookMu  sync.RWMutex
		yesBook *sdk.OrderBook
		noBook  *sdk.OrderBook
		yesTok  string
		noTok   string
	)

	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case book := <-bookAdapter.OrderBook():
				if book == nil || len(book.Bids) == 0 || len(book.Asks) == 0 {
					continue
				}
				bookMu.Lock()
				switch book.AssetId {
				case yesTok:
					yesBook = book
				case noTok:
					noBook = book
				default:
					log.Printf("[Book] 未匹配 token=%s (期望 YES=%s NO=%s)",
						book.AssetId, yesTok, noTok)
				}
				bookMu.Unlock()
			}
		}
	}()

	// ── 采集器 & 写入器 ──
	collector := lab.NewCollector(binance, *tickInterval)
	writer, err := lab.NewWriter(*outputDir)
	if err != nil {
		log.Fatalf("[Lab] writer 创建失败: %v", err)
	}
	defer func() {
		if err := writer.Close(); err != nil {
			log.Printf("[Lab] writer 关闭失败: %v", err)
		}
	}()

	log.Println("========================================")
	log.Printf(" 特征研究实验室 — %s 数据采集", *slugPrefix)
	log.Printf(" 交易对: %s  |  Slug: %s  |  输出: %s  |  间隔: %ds",
		*symbol, *slugPrefix, *outputDir, *tickInterval)
	log.Println(" 数据源: [Chainlink TWAP-60(结算基准)] + [Binance aggTrade+depth20(研究对照)] + [Polymarket CLOB books]")
	log.Println("========================================")

	// 等待初始数据就绪
	log.Println("[Lab] 等待初始数据...")
	time.Sleep(3 * time.Second)

	// ── 市场周期循环 ──
	for {
		select {
		case <-ctx.Done():
			log.Println("[Lab] 正在关闭...")
			return
		default:
		}

		// 步骤 1: 计算下一个 5 分钟对齐边界
		now := time.Now()
		alignedTs := now.Unix() / lab.WindowSec * lab.WindowSec
		nextStart := time.Unix(alignedTs, 0)
		marketSlug := fmt.Sprintf("%s-%d", *slugPrefix, nextStart.Unix())

		// 步骤 2: 等待至窗口起点（5 分整）。官方 TWAP 开盘价接口有数据
		// 延迟，需从边界起轮询才能及时拿到 openPrice。
		if wait := time.Until(nextStart); wait > 0 {
			log.Printf("[Cycle] 下一个窗口 %s, 等待 %v (slug=%s)",
				nextStart.UTC().Format(time.RFC3339), wait.Round(time.Second), marketSlug)
			select {
			case <-ctx.Done():
				return
			case <-time.After(wait):
			}
		}

		// 后台轮询官方 TWAP 开盘价（10s 间隔，最长 30s，即最多 3 次失败），
		// 不阻塞主循环 —— 先以流采样为初值继续采集，官方值到达后经
		// officialOpenCh 在采集循环内修正；超时未就绪则本周期沿用流采样。
		twapOpen, _ := twapAdapter.Latest()
		officialOpenCh := make(chan float64, 1)
		go func() {
			officialOpen, ok := feed.PollOfficialOpenPrice(ctx, client, nextStart,
				int64(lab.WindowSec), sdk.ChainlinkTwapWindowSixty, 30*time.Second)
			if ok {
				officialOpenCh <- officialOpen
				return
			}
			if twapOpen > 0 {
				log.Printf("[Cycle] ⚠️ 官方 TWAP 开盘价超时未就绪，本周期沿用流采样 %.2f", twapOpen)
			} else {
				log.Printf("[Cycle] ⚠️ TWAP 开盘价不可用（官方超时且流无数据）")
			}
		}()

		// 确保 Binance 5m K 线已生成（窗口起点 + 2 秒）
		if wait := time.Until(nextStart.Add(2 * time.Second)); wait > 0 {
			select {
			case <-ctx.Done():
				return
			case <-time.After(wait):
			}
		}

		// 步骤 3: 获取 Binance K 线开盘价
		binance.FetchKlineOpenPrice()

		btc := binance.LatestData()
		openPrice := btc.OpenPrice
		if openPrice == 0 {
			openPrice = btc.Price
			log.Printf("[Cycle] ⚠️  K 线开盘价不可用，使用当前价 %.2f", openPrice)
		}

		// 步骤 4: 获取 Polymarket 市场信息 → conditionId + token IDs
		log.Printf("[Cycle] 获取市场: %s", marketSlug)
		marketData, err := client.FetchMarketBySlug(marketSlug)
		if err != nil {
			log.Printf("[Cycle] 获取市场失败: %v —— 5s 后重试", err)
			select {
			case <-ctx.Done():
				return
			case <-time.After(5 * time.Second):
			}
			continue
		}

		conditionID := marketData.Get("conditionId").String()

		// 解析 token ID 和 outcomes
		var tokenIDs []string
		clobRaw := marketData.Get("clobTokenIds").String()
		for _, v := range gjson.Parse(clobRaw).Array() {
			tokenIDs = append(tokenIDs, v.String())
		}

		outcomesRaw := marketData.Get("outcomes").String()
		var outcomes []string
		for _, v := range gjson.Parse(outcomesRaw).Array() {
			outcomes = append(outcomes, v.String())
		}

		var yesTokenID, noTokenID string
		if len(tokenIDs) >= 2 {
			for i, oc := range outcomes {
				switch oc {
				case "Up", "Yes":
					yesTokenID = tokenIDs[i]
				case "Down", "No":
					noTokenID = tokenIDs[i]
				}
			}
		}

		log.Printf("[Cycle] conditionId=%s YES=%s NO=%s",
			conditionID, yesTokenID, noTokenID)

		// 步骤 5: 订阅新 token（先取消旧订阅）
		if yesTok != "" || noTok != "" {
			var oldTokens []string
			if yesTok != "" {
				oldTokens = append(oldTokens, yesTok)
			}
			if noTok != "" {
				oldTokens = append(oldTokens, noTok)
			}
			bookAdapter.UnsubscribeTokens(oldTokens...)
		}
		bookAdapter.SubscribeTokens(tokenIDs...)

		bookMu.Lock()
		yesTok = yesTokenID
		noTok = noTokenID
		yesBook = nil
		noBook = nil
		bookMu.Unlock()

		// 步骤 6: 启动事件采集
		collector.StartEvent(conditionID, nextStart.Unix(), openPrice)
		collector.SetTwapOpen(twapOpen)
		log.Printf("[Cycle] event=%s binance_open=%.2f twap_open=%.2f 开始采集...",
			conditionID, openPrice, twapOpen)

		ticker := time.NewTicker(time.Duration(*tickInterval) * time.Second)
		snapCount := 0
		lastLogRemaining := 0

	collectLoop:
		for {
			select {
			case <-ctx.Done():
				ticker.Stop()
				return

			case officialOpen := <-officialOpenCh:
				// 官方开盘价后台轮询到达，修正后续 snapshot 的 TwapOpen
				collector.SetTwapOpen(officialOpen)
				log.Printf("[Cycle] ✅ 官方 TWAP 开盘价 %.2f 已修正（边界后 %.1fs）",
					officialOpen, time.Since(nextStart).Seconds())

			case tickTime := <-ticker.C:
				// 读取最新的 Polymarket 订单簿价格
				bookMu.RLock()
				yb := yesBook
				nb := noBook
				bookMu.RUnlock()

				collector.UpdatePolymarket(bestBid(yb), bestBid(nb), maxLatency(yb, nb))

				// 读取最新 TWAP-60（结算口径，特征基准）
				twPrice, twAge := twapAdapter.Latest()
				collector.UpdateTwap(twPrice, twAge)

				snap := collector.Tick(tickTime)
				if snap == nil {
					continue
				}
				snapCount++

				if snap.RemainingSec <= 0 {
					ticker.Stop()
					break collectLoop
				}

				if snap.RemainingSec%30 == 0 && snap.RemainingSec != lastLogRemaining {
					lastLogRemaining = snap.RemainingSec
					log.Printf("[Event] %s remaining=%ds snaps=%d price=%.2f yes=%.4f no=%.4f",
						conditionID, snap.RemainingSec, snapCount,
						snap.CurrentPrice, snap.YesPrice, snap.NoPrice)
				}
			}
		}

		// 步骤 7: 封存并持久化事件
		event := collector.FinalizeEvent()

		// 官方 TWAP 结算价修正：窗口结束后 crypto-price 接口才产出
		// closePrice（与开盘价同源的数据延迟）。轮询至 endTime+9s，
		// 未就绪则沿用流采样值。
		closeTimeout := time.Until(time.Unix(nextStart.Unix()+lab.WindowSec, 0).Add(9 * time.Second))
		if closeTimeout < 0 {
			closeTimeout = 0
		}
		if officialOpen, officialClose, ok := feed.PollOfficialClosePrice(ctx, client, nextStart,
			int64(lab.WindowSec), sdk.ChainlinkTwapWindowSixty, closeTimeout); ok {
			event.TwapOpenPrice = officialOpen
			event.TwapClosePrice = officialClose
			event.Outcome = 1 // Down
			if officialClose > officialOpen {
				event.Outcome = 0 // Up
			}
		} else {
			log.Printf("[Event] ⚠️ %s 官方结算价未就绪，沿用流采样 close", conditionID)
		}

		outcomeLabel := "DOWN/Flat"
		if event.Outcome == 0 {
			outcomeLabel = "UP"
		}
		log.Printf("[Event] %s 完成 — twap open=%.2f close=%.2f outcome=%s | binance open=%.2f close=%.2f snapshots=%d",
			conditionID, event.TwapOpenPrice, event.TwapClosePrice, outcomeLabel,
			event.OpenPrice, event.ClosePrice, len(event.Snapshots))

		if err := writer.Write(event); err != nil {
			log.Printf("[Writer] 写入失败: %v", err)
		}
	}
}

// ---- 辅助函数 ----

func bestBid(book *sdk.OrderBook) float64 {
	if book == nil || len(book.Bids) == 0 {
		return 0
	}
	// Polymarket CLOB: bids 升序排列，最优（最高）出价在最后
	return book.Bids[len(book.Bids)-1].Price
}

// maxLatency 返回两个订单簿中延迟较大的值（毫秒）。
func maxLatency(yb, nb *sdk.OrderBook) int64 {
	var max int64
	if yb != nil && yb.Latency > max {
		max = yb.Latency
	}
	if nb != nil && nb.Latency > max {
		max = nb.Latency
	}
	return max
}

// ---- 配置 ----

type appConfig struct {
	SDK sdk.Config `mapstructure:"sdk"`
}

func loadConfig() *appConfig {
	cfg := &appConfig{
		SDK: sdk.Config{
			Polymarket: sdk.PolymarketConfig{
				ChainID:        137,
				ClobBaseURL:    "https://clob.polymarket.com",
				ClobWSBaseURL:  "wss://ws-subscriptions-clob.polymarket.com",
				GammaBaseURL:   "https://gamma-api.polymarket.com",
				DataAPIBaseURL: "https://data-api.polymarket.com",
				LiveWSBaseURL:  "wss://ws-live-data.polymarket.com",
			},
		},
	}
	// 环境变量覆盖
	if v := os.Getenv("POLYMARKET_OWNER_KEY"); v != "" {
		cfg.SDK.Polymarket.OwnerKey = v
	}
	if v := os.Getenv("POLYMARKET_CLOB_KEY"); v != "" {
		if cfg.SDK.Polymarket.CLOBCreds == nil {
			cfg.SDK.Polymarket.CLOBCreds = &model.ApiKeyCreds{}
		}
		cfg.SDK.Polymarket.CLOBCreds.Key = v
	}
	if v := os.Getenv("POLYMARKET_CLOB_SECRET"); v != "" {
		if cfg.SDK.Polymarket.CLOBCreds == nil {
			cfg.SDK.Polymarket.CLOBCreds = &model.ApiKeyCreds{}
		}
		cfg.SDK.Polymarket.CLOBCreds.Secret = v
	}
	if v := os.Getenv("POLYMARKET_CLOB_PASSPHRASE"); v != "" {
		if cfg.SDK.Polymarket.CLOBCreds == nil {
			cfg.SDK.Polymarket.CLOBCreds = &model.ApiKeyCreds{}
		}
		cfg.SDK.Polymarket.CLOBCreds.Passphrase = v
	}
	if v := os.Getenv("POLYMARKET_FUNDER"); v != "" {
		cfg.SDK.Polymarket.FunderAddress = v
	}
	if v := os.Getenv("POLYMARKET_PROXY"); v != "" {
		cfg.SDK.SocksProxy = v
	}
	return cfg
}
