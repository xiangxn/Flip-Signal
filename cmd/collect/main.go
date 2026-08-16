// collect 是数据格式 v2 的高频数据采集器（全新程序，与旧 lab 完全独立）。
//
// 采集内容（见 docs/data_recollection_plan_2026-08-16.md）:
//   P0-1 Binance 1s 聚合（价格/主动买卖量/成交笔数/深度和）
//   P0-2 PM 盘口 1s 快照（yes/no bid+ask、top5 数量、盘口时戳）
//   P0-3 PM price_change 聚合（每 token 每秒: 主动买卖笔数量/max单/vwap）
//   每 tick 附 TWAP-60 采样; 窗口结束后补官方 TWAP 开/收盘与 outcome。
//
// 输出: data/btc/events_YYYY-MM-DD.jsonl —— 每窗口一行 JSON（ticks+trades）。
// ⚠️ 启动前须先把旧 5s 快照目录 data/btc 改名移走（如 data/btc_5s），
// 否则新旧格式会混写进同名文件。
//
// Usage:
//
//	go run ./cmd/collect -output data/btc
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
	"sync/atomic"
	"syscall"
	"time"

	"github.com/xiangxn/go-polymarket-sdk/model"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"

	"github.com/necklace/flip-signal/internal/feed"
	"github.com/necklace/flip-signal/internal/collect"
)

func init() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
}

func main() {
	outputDir := flag.String("output", "data/btc", "事件 JSONL 输出目录（⚠️ 旧 5s 数据需先移走，见文档 §3.1）")
	symbol := flag.String("symbol", "BTCUSDT", "Binance 交易对")
	slugPrefix := flag.String("slug", "btc-updown-5m", "Polymarket slug 前缀")
	customFeature := flag.Bool("custom-feature", false,
		"CLOB 订阅自定义特性标志。A/B 实测（2026-08-16）: price_change 上行与该标志无关，"+
			"默认 false 与 OrderBookAdapter 一致")
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
		log.Println("[Collect] ⚠️  未配置 POLYMARKET_OWNER_KEY —— 只读模式运行")
	}

	// ── Binance 适配器（复用 feed，仅新增 TradeCount 只读字段）──
	binanceCfg := feed.BinanceConfig{
		Symbol:        *symbol,
		StreamBaseURL: "wss://data-stream.binance.vision",
		RestBaseURL:   "https://data-api.binance.vision",
	}
	binance := feed.NewBinanceAdapterWithConfig(binanceCfg)
	go func() {
		if err := binance.Start(ctx); err != nil {
			log.Printf("[Collect] Binance 启动失败: %v", err)
		}
	}()

	// ── PM MarketMonitor（直接使用 SDK，需 book + price_change 双通道）──
	// customFeatureEnabled: A/B 实测 price_change 上行与该标志无关（false 时
	// 150s 仍收到 5.2 万事件），默认 false 与 OrderBookAdapter 一致。
	monitor := sdk.NewMarketMonitor(cfg.SDK.Polymarket.ClobWSBaseURL, false, client, *customFeature)

	// 订阅状态（重启恢复用）与当前 token 映射（price_change → YES/NO）
	var (
		subMu    sync.RWMutex
		subTokens []string
		tokMu    sync.RWMutex
		yesTok   string
		noTok    string
		// 当前窗口成交聚合器（price_change goroutine 并发写）
		currentBucketer atomic.Pointer[collect.TradeBucketer]
	)

	// 盘口追踪：book 通道持续消费，保存最新 YES/NO 簿（1s 采样读取）
	var (
		bookMu  sync.RWMutex
		yesBook *sdk.OrderBook
		noBook  *sdk.OrderBook
	)
	go func() {
		ch := monitor.SubscribeOrderBook()
		for {
			select {
			case <-ctx.Done():
				return
			case book := <-ch:
				if book == nil || len(book.Bids) == 0 || len(book.Asks) == 0 {
					continue
				}
				tokMu.RLock()
				yt, nt := yesTok, noTok
				tokMu.RUnlock()
				bookMu.Lock()
				switch book.AssetId {
				case yt:
					yesBook = book
				case nt:
					noBook = book
				}
				bookMu.Unlock()
			}
		}
	}()

	// price_change 消费：按 token 映射聚合进当前窗口 bucketer
	go func() {
		ch := monitor.SubscribePriceChange()
		eventCount := 0
		for {
			select {
			case <-ctx.Done():
				return
			case info := <-ch:
				if info == nil {
					continue
				}
				eventCount++
				if eventCount%5000 == 0 {
					// 活跃期约 200-500 事件/秒，5000 一报 ≈ 每 10-25s 一条
					log.Printf("[Trade] price_change 事件累计 %d（最近 ts=%d）", eventCount, info.Timestamp)
				}
				tokMu.RLock()
				yt, nt := yesTok, noTok
				tokMu.RUnlock()
				b := currentBucketer.Load()
				if b == nil {
					continue
				}
				nowMs := time.Now().UnixMilli()
				for _, pc := range info.PriceChanges {
					token := ""
					switch pc.AssetID {
					case yt:
						token = "YES"
					case nt:
						token = "NO"
					default:
						continue // 旧窗口迟到的笔
					}
					b.Add(token, nowMs, pc.Side,
						parseFloat(pc.Price), parseFloat(pc.Size),
						parseFloat(pc.BestBid), parseFloat(pc.BestAsk))
				}
			}
		}
	}()

	// monitor Run 重启（复用 OrderBookAdapter 的重启模式）:
	// Run 退出后 SDK 会清空内部订阅，需恢复本地订阅副本再重启。
	go func() {
		for {
			err := monitor.Run(ctx)
			if err == nil || ctx.Err() != nil {
				return
			}
			log.Printf("[Collect] ⚠️ MarketMonitor 异常退出: %v —— 5 秒后重启", err)
			select {
			case <-ctx.Done():
				return
			case <-time.After(5 * time.Second):
			}
			subMu.RLock()
			tokens := append([]string(nil), subTokens...)
			subMu.RUnlock()
			monitor.SubscribeTokens(tokens...)
		}
	}()

	// ── Chainlink TWAP-60（每 tick 采样）──
	twapMonitor := sdk.NewCryptoPriceMonitor(client, sdk.MonitorChainlinkTwap, "BTC_60")
	twapAdapter := feed.NewTwapAdapter(twapMonitor, "BTC", sdk.ChainlinkTwapWindowSixty)
	twapAdapter.Start(ctx)
	go func() {
		for {
			err := twapMonitor.Run(ctx)
			if err == nil || ctx.Err() != nil {
				return
			}
			log.Printf("[Collect] ⚠️ TWAP monitor 异常退出: %v —— 5 秒后重启", err)
			select {
			case <-ctx.Done():
				return
			case <-time.After(5 * time.Second):
			}
		}
	}()

	// ── 写入器（抽出后的通用按日 JSONL Writer）──
	writer, err := collect.NewJSONLWriter(*outputDir, "events")
	if err != nil {
		log.Fatalf("[Collect] writer 创建失败: %v", err)
	}
	defer func() {
		if err := writer.Close(); err != nil {
			log.Printf("[Collect] writer 关闭失败: %v", err)
		}
	}()

	log.Println("========================================")
	log.Printf(" 高频数据采集 v2 — %s | 输出: %s（全新目录）", *slugPrefix, *outputDir)
	log.Println(" 数据源: [Binance aggTrade+depth20 1s聚合] + [PM CLOB books 1s快照] + [PM price_change 秒聚合] + [Chainlink TWAP-60]")
	log.Println("========================================")

	// 等待初始数据就绪
	log.Println("[Collect] 等待初始数据...")
	time.Sleep(3 * time.Second)

	// ── 市场周期循环 ──
	for {
		select {
		case <-ctx.Done():
			log.Println("[Collect] 正在关闭...")
			return
		default:
		}

		// 步骤 1: 计算下一个 5 分钟对齐边界
		now := time.Now()
		alignedTs := now.Unix() / collect.WindowSec * collect.WindowSec
		nextStart := time.Unix(alignedTs, 0)
		slug := fmt.Sprintf("%s-%d", *slugPrefix, nextStart.Unix())

		// 步骤 2: 等待至窗口起点（5 分整）。官方 TWAP 开盘价接口有数据
		// 延迟，需从边界起轮询才能及时拿到 openPrice。
		if wait := time.Until(nextStart); wait > 0 {
			log.Printf("[Cycle] 下一个窗口 %s, 等待 %v (slug=%s)",
				nextStart.UTC().Format(time.RFC3339), wait.Round(time.Second), slug)
			select {
			case <-ctx.Done():
				return
			case <-time.After(wait):
			}
		}

		// 后台轮询官方 TWAP 开盘价（10s 间隔，最长 30s），不阻塞主循环
		twapOpen, _ := twapAdapter.Latest()
		officialOpenCh := make(chan float64, 1)
		go func() {
			officialOpen, ok := feed.PollOfficialOpenPrice(ctx, client, nextStart,
				collect.WindowSec, sdk.ChainlinkTwapWindowSixty, 30*time.Second)
			if ok {
				officialOpenCh <- officialOpen
				return
			}
			if twapOpen > 0 {
				log.Printf("[Cycle] ⚠️ 官方 TWAP 开盘价超时未就绪，本周期沿用流采样 %.2f", twapOpen)
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

		// 步骤 3: Binance K 线开盘价 —— 异步获取，不阻塞采集启动。
		// 初值用当前价，K 线到达后经 klineCh 在采集循环内修正事件元数据。
		btc := binance.LatestData()
		binanceOpen := btc.Price
		klineCh := make(chan float64, 1)
		go func() {
			binance.FetchKlineOpenPrice()
			klineCh <- binance.LatestData().OpenPrice
		}()

		// 步骤 4: 获取市场 → conditionID + YES/NO token
		log.Printf("[Cycle] 获取市场: %s", slug)
		marketData, err := client.FetchMarketBySlug(slug)
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
		yesTokenID, noTokenID := collect.ParseMarketTokens(marketData)
		log.Printf("[Cycle] conditionId=%s YES=%s NO=%s", conditionID, yesTokenID, noTokenID)

		// 步骤 5: 订阅切换（先退订旧 token，保留副本供 monitor 重启恢复）
		subMu.Lock()
		if len(subTokens) > 0 {
			monitor.UnsubscribeTokens(subTokens...)
		}
		subTokens = []string{yesTokenID, noTokenID}
		subMu.Unlock()
		monitor.SubscribeTokens(yesTokenID, noTokenID)

		tokMu.Lock()
		yesTok = yesTokenID
		noTok = noTokenID
		tokMu.Unlock()
		bookMu.Lock()
		yesBook = nil
		noBook = nil
		bookMu.Unlock()

		// 步骤 6: 窗口状态
		currentBucketer.Store(collect.NewTradeBucketer(nextStart.UnixMilli()))
		endTime := nextStart.Add(collect.WindowSec * time.Second)
		var ticks []collect.HFTick

		log.Printf("[Cycle] event=%s binance_open=%.2f twap_open=%.2f 开始采集...",
			conditionID, binanceOpen, twapOpen)

		// 丢弃窗口间隙累计的成交量：上一窗口 finalize + 官方收盘轮询 +
		// 市场切换期间的 Binance 成交不属于本窗口首秒，否则首 tick 会出现
		// 周期性量尖峰（数据质量 bug，实测发现）。
		binance.ConsumeVolume()

		ticker := time.NewTicker(time.Second)

	collectLoop:
		for {
			select {
			case <-ctx.Done():
				ticker.Stop()
				return

			case officialOpen := <-officialOpenCh:
				twapOpen = officialOpen
				log.Printf("[Cycle] ✅ 官方 TWAP 开盘价 %.2f 已修正（边界后 %.1fs）",
					officialOpen, time.Since(nextStart).Seconds())

			case klineOpen := <-klineCh:
				if klineOpen > 0 {
					binanceOpen = klineOpen
					log.Printf("[Cycle] ✅ Binance K 线开盘价 %.2f 已修正", klineOpen)
				} else {
					log.Printf("[Cycle] ⚠️  K 线开盘价不可用，沿用当前价 %.2f", binanceOpen)
				}

			case tickTime := <-ticker.C:
				rem := int(endTime.Sub(tickTime).Seconds())
				if rem <= 0 {
					ticker.Stop()
					break collectLoop
				}

				// P0-1: Binance 1s 聚合
				// 先读累计（价格/深度快照 + 自上次 ConsumeVolume 以来的
				// 买卖量与成交笔数），再重置 —— bd 中即本秒增量。
				bd := binance.LatestData()
				binance.ConsumeVolume()

				// P0-2: PM 盘口 1s 快照
				bookMu.RLock()
				yb, nb := yesBook, noBook
				bookMu.RUnlock()

				// TWAP 采样
				twPrice, twAge := twapAdapter.Latest()

				ticks = append(ticks, collect.HFTick{
					Ts:  tickTime.UnixMilli(),
					Rem: rem,
					Bin: collect.BinTick{
						Price:   bd.Price,
						BuyVol:  bd.BuyVolume,
						SellVol: bd.SellVolume,
						Ticks:   int(bd.TradeCount),
						Bid5:    bd.BidDepth5,
						Ask5:    bd.AskDepth5,
						Bid10:   bd.BidDepth10,
						Ask10:   bd.AskDepth10,
					},
					PM: collect.MakePMTick(yb, nb),
					Twap: collect.TwapTick{Price: twPrice, AgeMs: twAge},
				})
			}
		}

		// 步骤 7: 官方 TWAP 结算价修正 —— 异步获取，不阻塞主循环进入下一窗口。
		// 同步轮询 ~9-12s 会让下一窗口的订阅与首 tick 延迟，每窗口损失
		// 前 ~10 秒数据。窗口数据先快照，官方收盘价到达后（或超时回退
		// 流采样）在后台写入事件；下一窗口事件至少 300s 后才写入，
		// 写入顺序天然保持。
		snapTicks := ticks
		snapTrades := currentBucketer.Load().Snapshot()
		currentBucketer.Store(nil)
		finalTwapOpen := twapOpen

		go func() {
			// 异步后可放宽轮询窗口（12s ≈ 2 次请求），提高官方收盘命中率
			twapClose := finalTwapOpen
			outcome := 1 // Down
			if officialOpen, officialClose, ok := feed.PollOfficialClosePrice(ctx, client, nextStart,
				collect.WindowSec, sdk.ChainlinkTwapWindowSixty, 12*time.Second); ok {
				finalTwapOpen = officialOpen
				twapClose = officialClose
				if officialClose > officialOpen {
					outcome = 0 // Up
				}
			} else if ctx.Err() == nil {
				log.Printf("[Event] ⚠️ %s 官方结算价未就绪，沿用流采样", conditionID)
			}
			if ctx.Err() != nil {
				log.Printf("[Event] %s 正在关闭，丢弃未写入窗口", conditionID)
				return
			}

			event := collect.Event{
				ConditionID:    conditionID,
				Slug:           slug,
				StartTime:      nextStart.Unix(),
				TwapOpenPrice:  finalTwapOpen,
				TwapClosePrice: twapClose,
				Outcome:        outcome,
				BinanceOpen:    binanceOpen,
				Ticks:          snapTicks,
				Trades:         snapTrades,
			}

			outcomeLabel := "DOWN/Flat"
			if outcome == 0 {
				outcomeLabel = "UP"
			}
			log.Printf("[Event] %s 完成 — twap open=%.2f close=%.2f outcome=%s | ticks=%d trades_agg=%d",
				conditionID, finalTwapOpen, twapClose, outcomeLabel, len(snapTicks), len(snapTrades))

			if err := writer.Write(event); err != nil {
				log.Printf("[Writer] 写入失败: %v", err)
			}
		}()
	}
}

// ---- 配置（与 cmd/lab 同款环境变量约定）----

type appConfig struct {
	SDK sdk.Config `mapstructure:"sdk"`
}

func loadConfig() *appConfig {
	cfg := &appConfig{
		SDK: sdk.Config{
			HttpTimeout: 10 * time.Second, // 所有 REST（市场/crypto-price）兜底超时
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

// parseFloat 解析价格/数量字符串，失败返回 0（SDK price_change 字段为字符串）。
func parseFloat(s string) float64 {
	var f float64
	fmt.Sscanf(s, "%f", &f)
	return f
}
