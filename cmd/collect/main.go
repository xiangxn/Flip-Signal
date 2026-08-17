// collect 是数据格式 v2 的高频数据采集器（全新程序，与旧 lab 完全独立）。
//
// 采集内容（见 docs/recollection_plan_2026-08-16.md）:
//   P0-1 Binance 1s 聚合（价格/主动买卖量/成交笔数/深度和）
//   P0-2 PM 盘口 1s 快照（yes/no bid+ask、top5 数量、盘口时戳）
//   P0-3 PM last_trade_price 聚合（每 token 每秒: 主动买卖笔数量/max单/vwap）
//        ⚠️ 成交流必须用 last_trade_price；price_change 实测是挂单流
//        （镜像对 buy≡sell 恒等，方向信息被抹平），见 2026-08-18 数据审计
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

	"github.com/tidwall/gjson"
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
		"CLOB 订阅自定义特性标志。A/B 实测（2026-08-16）: 订阅上行与该标志无关，"+
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

	// ── PM MarketMonitor（直接使用 SDK，需 book + last_trade_price 双通道）──
	// customFeatureEnabled: A/B 实测（2026-08-16）订阅上行与该标志无关，
	// 默认 false 与 OrderBookAdapter 一致。
	monitor := sdk.NewMarketMonitor(cfg.SDK.Polymarket.ClobWSBaseURL, false, client, *customFeature)

	// 订阅状态（重启恢复用）与当前 token 映射（last_trade_price → YES/NO）
	var (
		subMu    sync.RWMutex
		subTokens []string
		tokMu    sync.RWMutex
		yesTok   string
		noTok    string
		// 当前窗口成交聚合器（last_trade_price goroutine 并发写）
		currentBucketer atomic.Pointer[collect.TradeBucketer]
		// 下一窗口市场预取缓存：窗口尾部异步预取的结果跨窗口迭代传递
		// （连续运行时等待期预取没有空窗，平滑过渡靠这里）
		nextMarketMu    sync.Mutex
		nextMarketCache *gjson.Result
		nextMarketErr   error
		nextMarketSlug  string
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

	// last_trade_price 消费：按 token 映射聚合进当前窗口 bucketer。
	// 用成交自身时戳归桶（消息内服务端时间）；成交时刻的盘口上下文
	// 取本簿当前最优 bid/ask（成交后簿可能已被后续更新覆盖，此处为
	// 本地到达时刻的近似上下文，book_ts 可辅助判定陈旧度）。
	go func() {
		ch := monitor.SubscribeLastTradePrice()
		eventCount := 0
		for {
			select {
			case <-ctx.Done():
				return
			case trade := <-ch:
				if trade == nil {
					continue
				}
				eventCount++
				if eventCount%500 == 0 {
					// 实盘成交率约 5-10 笔/秒，500 一报 ≈ 每 1-2 分钟一条
					log.Printf("[Trade] last_trade_price 累计 %d（最近 ts=%d）", eventCount, trade.Timestamp)
				}
				tokMu.RLock()
				yt, nt := yesTok, noTok
				tokMu.RUnlock()
				b := currentBucketer.Load()
				if b == nil {
					continue
				}
				token := ""
				switch trade.AssetID {
				case yt:
					token = "YES"
				case nt:
					token = "NO"
				default:
					continue // 旧窗口迟到的笔
				}
				bookMu.RLock()
				var bb, ba float64
				switch token {
				case "YES":
					bb, ba = collect.BestBid(yesBook), collect.BestAsk(yesBook)
				case "NO":
					bb, ba = collect.BestBid(noBook), collect.BestAsk(noBook)
				}
				bookMu.RUnlock()
				tsMs := trade.Timestamp
				if tsMs == 0 {
					tsMs = time.Now().UnixMilli() // 消息缺时戳时回退本地到达时刻
				}
				b.Add(token, tsMs, trade.Side,
					parseFloat(trade.Price), parseFloat(trade.Size),
					bb, ba, trade.TransactionHash)
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

	log.Println("========================================")
	log.Printf(" 高频数据采集 v2 — %s | 输出: %s（全新目录）", *slugPrefix, *outputDir)
	log.Println(" 数据源: [Binance aggTrade+depth20 1s聚合] + [PM CLOB books 1s快照] + [PM last_trade_price 秒聚合] + [Chainlink TWAP-60]")
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

		// 已错过窗口起点（>2s 宽限，启动容差）：跳过当前窗口，
		// 等待下一个完整窗口。中途重启不再产生半窗口事件，也避免与
		// 重启前已写入的事件重复（写入侧有 flock 去重兜底）。
		if time.Until(nextStart.Add(2*time.Second)) <= 0 {
			log.Printf("[Cycle] 窗口 %s 已开始，等待下一个完整窗口",
				nextStart.UTC().Format(time.RFC3339))
			nextStart = nextStart.Add(collect.WindowSec * time.Second)
		}
		slug := fmt.Sprintf("%s-%d", *slugPrefix, nextStart.Unix())

		// 步骤 2: 等待窗口起点，同时提前预取下一窗口市场信息（缓存）。
		// FetchMarketBySlug 是同步 HTTP（~1s），若等到边界才调用，订阅
		// 会延迟 1-3s、窗口头部 tick 丢失；市场在窗口开始前已上架 gamma，
		// 提前取到并缓存，边界一到立即订阅（首 tick ≈ 边界后 1s）。
		var (
			cachedMarket *gjson.Result
			cachedErr    error
		)
		if wait := time.Until(nextStart.Add(-prefetchLead)); wait > 0 {
			log.Printf("[Cycle] 下一个窗口 %s, 等待 %v（边界前 %ds 预取市场）",
				nextStart.UTC().Format(time.RFC3339), wait.Round(time.Second),
				int(prefetchLead/time.Second))
			select {
			case <-ctx.Done():
				return
			case <-time.After(wait):
			}
		}
		// 边界前 20s 内预取，失败每 5s 重试直至边界前 2s
		for time.Until(nextStart) > 2*time.Second {
			log.Printf("[Cycle] 预取市场: %s", slug)
			cachedMarket, cachedErr = client.FetchMarketBySlug(slug)
			if cachedErr == nil {
				log.Printf("[Cycle] ✅ 市场预取成功（边界前 %.1fs）",
					time.Until(nextStart).Seconds())
				break
			}
			log.Printf("[Cycle] 预取市场失败: %v —— 5s 后重试", cachedErr)
			wait := 5 * time.Second
			if rem := time.Until(nextStart.Add(-2 * time.Second)); rem < wait {
				wait = rem
			}
			if wait <= 0 {
				break
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(wait):
			}
		}
		// 等待至窗口起点
		if wait := time.Until(nextStart); wait > 0 {
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

		// 步骤 3: Binance K 线开盘价 —— 异步获取，不阻塞采集启动。
		// 新 K 线在边界后 1-2s 才稳定生成，goroutine 内稍等再取；
		// 初值用当前价，K 线到达后经 klineCh 在采集循环内修正事件元数据。
		btc := binance.LatestData()
		binanceOpen := btc.Price
		klineCh := make(chan float64, 1)
		go func() {
			time.Sleep(2 * time.Second)
			binance.FetchKlineOpenPrice()
			klineCh <- binance.LatestData().OpenPrice
		}()

		// 步骤 4: 使用预取的市场信息。
		// 优先级: 窗口内预取（连续运行的主路径，slug 校验防陈旧值串窗）
		// → 等待期预取（启动/跳窗后）→ 边界后同步兜底。
		nextMarketMu.Lock()
		nm, nme, nslug := nextMarketCache, nextMarketErr, nextMarketSlug
		nextMarketCache, nextMarketErr, nextMarketSlug = nil, nil, "" // 用后即清
		nextMarketMu.Unlock()
		var marketData *gjson.Result
		var err error
		switch {
		case nm != nil && nslug == slug:
			marketData, err = nm, nme
		case cachedMarket != nil:
			marketData, err = cachedMarket, cachedErr
		default:
			log.Printf("[Cycle] 预取未就绪，边界后兜底获取市场: %s", slug)
			marketData, err = client.FetchMarketBySlug(slug)
		}
		if err != nil {
			// 获取失败：跳过本窗口，直接等待下一个完整窗口边界。
			// 不再走 5s 重试——窗口已开始，重试只会产生半窗口数据，
			// 且重试路径会反复触发"窗口已开始"跳过日志（日志噪音）。
			log.Printf("[Cycle] 获取市场失败: %v —— 跳过本窗口，等待下一窗口", err)
			waitTo := nextStart.Add(collect.WindowSec * time.Second)
			select {
			case <-ctx.Done():
				return
			case <-time.After(time.Until(waitTo)):
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

		// 下一窗口市场预取：采集最后 20s 内异步启动（结果写入主作用域
		// nextMarketCache，跨迭代传递），窗口结束即可无缝订阅下一窗口。
		nextPrefetched := false

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

					// 窗口尾部：异步预取下一窗口市场（只触发一次），
					// 窗口结束即可无缝订阅下一窗口
					if !nextPrefetched && rem <= int(prefetchLead/time.Second) {
						nextPrefetched = true
						nextSlug := fmt.Sprintf("%s-%d", *slugPrefix,
							nextStart.Add(collect.WindowSec*time.Second).Unix())
						go func() {
							m, e := client.FetchMarketBySlug(nextSlug)
							nextMarketMu.Lock()
							nextMarketCache, nextMarketErr, nextMarketSlug = m, e, nextSlug
							nextMarketMu.Unlock()
							if e != nil {
								log.Printf("[Cycle] 下一窗口预取失败: %v（边界后兜底）", e)
							} else {
								log.Printf("[Cycle] ✅ 下一窗口市场预取成功 %s", nextSlug)
							}
						}()
					}
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
		// 流采样收盘价作为 close 初值（窗口结束时刻的 TWAP-60 流值，与官方
		// 结算同源）：官方接口有分钟级延迟（有时数十分钟不产出），先填流值，
		// 60s 内官方到达则覆盖，否则以流值定稿不再管。
		twapCloseStream, _ := twapAdapter.Latest()
		if twapCloseStream == 0 {
			twapCloseStream = finalTwapOpen
		}

		go func() {
			// 官方收盘 5s 间隔 × 60s 上限（异步执行，不阻塞主循环）
			twapClose := twapCloseStream
			outcome := 1 // Down
			closeSource := "stream"
			if twapCloseStream >= finalTwapOpen {
				outcome = 0 // Up
			}
			if officialOpen, officialClose, ok := feed.PollOfficialClosePrice(ctx, client, nextStart,
				collect.WindowSec, sdk.ChainlinkTwapWindowSixty, 60*time.Second); ok {
				finalTwapOpen = officialOpen
				twapClose = officialClose
				closeSource = "official"
				if officialClose >= officialOpen {
					outcome = 0 // Up
				}
			} else if ctx.Err() == nil {
				log.Printf("[Event] ⚠️ %s 官方结算价 60s 内未产出，沿用流采样（官方延迟可达数十分钟）", conditionID)
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
				CloseSource:    closeSource,
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

			// 去重写入（flock 跨进程安全，防重启/多实例重复写同一窗口）
			written, err := collect.WriteUniqueEvent(*outputDir, &event)
			if err != nil {
				log.Printf("[Writer] 写入失败: %v", err)
			} else if !written {
				log.Printf("[Event] ⚠️ %s 重复窗口，跳过写入", conditionID)
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

// parseFloat 解析价格/数量字符串，失败返回 0（SDK last_trade_price 字段为字符串）。
func parseFloat(s string) float64 {
	var f float64
	fmt.Sscanf(s, "%f", &f)
	return f
}

// prefetchLead 是市场信息预取提前量：窗口边界前多少秒开始调
// FetchMarketBySlug 并缓存结果（市场在窗口开始前已上架 gamma）。
// 边界一到用缓存立即订阅新市场，平滑过渡到下一个窗口的采集。
const prefetchLead = 20 * time.Second
