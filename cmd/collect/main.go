// collect 是数据格式 v2 的高频数据采集器（全新程序，与旧 lab 完全独立）。
//
// 采集内容（见 docs/recollection_plan_2026-08-16.md）:
//
//	P0-1 Binance 1s 聚合（价格/主动买卖量/成交笔数/深度和）
//	P0-2 PM 盘口 1s 快照（yes/no bid+ask、top5 数量、盘口时戳）
//	P0-3 PM last_trade_price 聚合（每 token 每秒: 主动买卖笔数量/max单/vwap）
//	     ⚠️ 成交流必须用 last_trade_price；price_change 实测是挂单流
//	     （镜像对 buy≡sell 恒等，方向信息被抹平），见 2026-08-18 数据审计
//	每 tick 附 TWAP-60 采样；窗口起/止两个边界价取**边界那一秒的 TWAP 推送**
//	（精确匹配, 决策 #15/#19）——它与官方 openPrice/closePrice 逐位同源。
//
// 支持任意 5 分钟 updown 标的（btc / eth / sol / bnb …）：全套命名由**资产名**经
// internal/feed.Asset 派生——slug 前缀、Binance 交易对、Chainlink symbol、输出目录
// 一次给全（见 feed/asset.go）。市场结构（UP/DOWN、Chainlink TWAP-60 结算）各标的相同。
//
// 输出: <output>/events_YYYY-MM-DD.jsonl —— 每窗口一行 JSON（ticks+trades）。
// ⚠️ 启动前须先把旧 5s 快照目录（如 data/btc）改名移走（如 data/btc_5s），
// 否则新旧格式会混写进同名文件。
//
// Usage:
//
//	go run ./cmd/collect -config v4.config.yaml -asset eth        # → data/eth
//	go run ./cmd/collect -config v4.config.yaml -asset sol        # → data/sol
//	go run ./cmd/collect -config v4.config.yaml -slug eth-updown-5m   # 等价, 显式 slug
//	go run ./cmd/collect -config v4.config.yaml                   # 用配置的 slug_prefix（默认 btc）
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
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/tidwall/gjson"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"

	"github.com/necklace/flip-signal/internal/collect"
	"github.com/necklace/flip-signal/internal/config"
	"github.com/necklace/flip-signal/internal/feed"
)

func init() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
}

func main() {
	configPath := flag.String("config", "", "配置文件路径（空 = 完全不读文件, 用代码默认值）")
	assetFlag := flag.String("asset", "", "资产名：btc|eth|sol|...（派生 slug/交易对/TWAP 符号/目录；空 = 用 -slug 或配置的 runtime.slug_prefix）")
	slugFlag := flag.String("slug", "", "Polymarket slug 前缀（如 eth-updown-5m；给了它则以它反推资产，优先于 -asset）")
	symbolFlag := flag.String("symbol", "", "Binance 交易对（空 = 由资产派生 <ASSET>USDT）")
	outputFlag := flag.String("output", "", "事件 JSONL 输出目录（空 = 由资产派生 data/<asset>）")
	customFeature := flag.Bool("custom-feature", false,
		"CLOB 订阅自定义特性标志。A/B 实测（2026-08-16）: 订阅上行与该标志无关，"+
			"默认 false 与 OrderBookAdapter 一致")
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// ── 配置（三层: flag > 配置文件 > 代码默认值）──
	// 凭证与端点全部走 internal/config（POLYMARKET_* 环境变量已废弃）。
	cfg, err := config.Load(*configPath)
	if err != nil {
		log.Fatalf("[Collect] 配置加载失败: %v", err)
	}

	// ── 资产参数（BTC / ETH / SOL … 共用一套代码，差异只在这几行）──
	// 优先级: -slug（完整市场前缀）> -asset（资产名）> 配置 runtime.slug_prefix。
	// 三者最终都归到 feed.Asset 的四个命名: slug / Binance 交易对 / Chainlink 符号 / 目录。
	asset := feed.AssetFromSlug(cfg.Runtime.SlugPrefix)
	switch {
	case *slugFlag != "":
		asset = feed.AssetFromSlug(*slugFlag)
		if *assetFlag != "" && !strings.EqualFold(asset.Name, *assetFlag) {
			log.Printf("[Collect] ⚠️ -asset=%s 与 -slug=%s 不一致，以 -slug 为准（资产 %s）",
				*assetFlag, *slugFlag, asset.Name)
		}
	case *assetFlag != "":
		asset = feed.AssetFor(*assetFlag)
	}
	if *symbolFlag != "" {
		asset.Binance = *symbolFlag
	}
	slugPrefix := asset.Slug
	outputDir := asset.DataDir()
	if *outputFlag != "" {
		outputDir = *outputFlag
	}

	// ── Polymarket 客户端（未配置私钥则自动生成临时密钥，只读模式）──
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
		log.Println("[Collect] ⚠️  未配置凭证 —— 只读模式运行")
	}

	// ── Binance 适配器（复用 feed）──
	// symbol 已由资产派生（-symbol 显式覆盖优先），此处只做"留空才填"的兜底。
	binance := feed.NewBinanceAdapterWithConfig(asset.ApplyBinance(cfg.Binance))
	go func() {
		// 初始拨号失败需重试：Start 仅在首拨失败时返回错误（成功后的断线
		// 由 runReadLoop 自愈重连），不重试的话整个进程生命周期内
		// Binance 数据流都是死的（仅启动时网络抖动即全损）。
		for {
			err := binance.Start(ctx)
			if err == nil || ctx.Err() != nil {
				return
			}
			log.Printf("[Collect] Binance 启动失败: %v —— 5 秒后重试", err)
			select {
			case <-ctx.Done():
				return
			case <-time.After(5 * time.Second):
			}
		}
	}()

	// ── PM MarketMonitor（直接使用 SDK，需 book + last_trade_price 双通道）──
	monitor := sdk.NewMarketMonitor(cfg.SDK.Polymarket.ClobWSBaseURL, false, client, *customFeature)

	// 订阅状态（重启恢复用）与当前 token 映射（last_trade_price → YES/NO）。
	// prevYesTok/prevNoTok 保留上一窗口 token：窗口过渡期（步骤 5 换映射后）
	// 上一窗口尾盘迟到的成交仍可路由进旧 bucketer（server 时戳越界会自动
	// 丢弃），否则这些笔在 consumer 层就被当作陌生 token 丢掉。
	var (
		subMu      sync.RWMutex
		subTokens  []string
		tokMu      sync.RWMutex
		yesTok     string
		noTok      string
		prevYesTok string
		prevNoTok  string
		// 当前窗口成交聚合器（last_trade_price goroutine 并发写）
		currentBucketer atomic.Pointer[collect.TradeBucketer]
		// 下一窗口市场预取缓存：窗口尾部异步预取的结果跨窗口迭代传递
		// （连续运行时等待期预取没有空窗，平滑过渡靠这里）
		nextMarketMu    sync.Mutex
		nextMarketCache *gjson.Result
		nextMarketErr   error
		nextMarketSlug  string

		// 盘口追踪：book 通道持续消费，保存最新 YES/NO 簿（1s 采样读取）
		bookMu  sync.RWMutex
		yesBook *sdk.OrderBook
		noBook  *sdk.OrderBook
	)

	// 盘口追踪：book 通道持续消费，保存最新 YES/NO 簿（1s 采样读取）。
	//
	// ⚠️ 守卫只能判 `book == nil`（决策 #21）：盘口是**整簿快照**，赢家侧在尾盘
	// 会被整侧撤空（asks 为空数组），若因此丢弃整条消息，内存里就留着**撤单前
	// 那一份旧簿**，采样会一路报 0.95~0.99 的假价（实测簿龄涨到 46.9s 而页面
	// 照旧报 0.99）。空侧照存 ⇒ BestAsk/BestBid 返回 0 ⇒ 该 tick 四档缺一，
	// 与回测宇宙同口径（缺一即丢）。
	go func() {
		ch := monitor.SubscribeOrderBook()
		for {
			select {
			case <-ctx.Done():
				return
			case book := <-ch:
				if book == nil {
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
				yt, nt, pyt, pnt := yesTok, noTok, prevYesTok, prevNoTok
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
				case pyt:
					token = "YES" // 上一窗口尾盘迟到笔（过渡期路由，桶边界校验兜底）
				case pnt:
					token = "NO"
				default:
					continue // 陌生 token（更早窗口的迟到笔）
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
	// ⚠️ ctx 未取消时无条件重启（含 Run 干净退出 err==nil 的场景，
	// 否则数据流会永久死亡——2026-08-18 review 修复）。
	go func() {
		for {
			err := monitor.Run(ctx)
			if ctx.Err() != nil {
				return
			}
			if err != nil {
				log.Printf("[Collect] ⚠️ MarketMonitor 异常退出: %v —— 5 秒后重启", err)
			} else {
				log.Printf("[Collect] ⚠️ MarketMonitor 干净退出 —— 5 秒后重启")
			}
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

	// ── Chainlink TWAP-60（每 tick 采样 + 边界推送缓存）──
	// v4 的 TwapAdapter 自带新鲜度看门狗（推送停更超 maxStale 自动重建订阅，
	// 半开连接场景 SDK 内部重连探测不到——2026-08-18 实测 TWAP 流曾冻结
	// 近 2 小时），故 eth 版那段手工的 startTwap/守护 goroutine 整体删除。
	twapAdapter := feed.NewTwapAdapter(client, string(asset.Chainlink), sdk.ChainlinkTwapWindowSixty, twapMaxStale)
	twapAdapter.StartWithMonitor(ctx)

	// ── 结算修正队列：写盘立即、官方价后台修正（见 internal/collect/settle.go）──
	// 只服务**推送缺失**的窗口（推送与官方逐位同源，正常窗无需修正）。
	pairFetch := feed.NewPricePairFetcher(client, asset.Chainlink, sdk.Fiveminute, sdk.ChainlinkTwapWindowSixty)
	settlementWorker := collect.NewSettlementWorker(outputDir,
		func(ctx context.Context, start time.Time) (float64, float64, bool) {
			open, close := pairFetch(ctx, start, start.Add(collect.WindowSec*time.Second))
			return open, close, open > 0 && close > 0
		}, collect.DefaultSettlementConfig())
	settlementWorker.Start(ctx)

	// 待定稿事件：窗口结束只封存 ticks + 起收盘推送等待，下一轮步骤 6
	// 补拍 trades（换桶前，捕获边界迟到的成交）后交 finalizeWindow 提交。
	// 为什么不在这里等：主循环必须立刻回去开下一个窗口——边界后停留超过
	// 2s 会让步骤 1 的宽限判据把整个下一窗当成「已开始」跳过。
	var (
		pendingEvent *collect.Event
		pendingClose <-chan float64
	)
	defer func() {
		// 关闭时补交未定稿窗口（部分窗口数据，尽力而为）。
		// 不走 Submit：ctx 取消后 worker 随时可能已排空队列退出，阻塞入队
		// 会让关闭流程永久挂起；直接落盘（WriteUniqueEvent 自带 flock 去重，
		// 与 worker 并发写安全）。收盘推送不再等（尽力取一次）。
		if pendingEvent != nil {
			if b := currentBucketer.Load(); b != nil {
				pendingEvent.Trades = b.Snapshot()
			}
			select {
			case p := <-pendingClose:
				if p > 0 {
					pendingEvent.TwapClosePrice = p
					pendingEvent.CloseSource = collect.SourcePush
					pendingEvent.Outcome = outcomeFor(pendingEvent.TwapOpenPrice, p)
				}
			default:
			}
			if _, err := collect.WriteUniqueEvent(outputDir, pendingEvent); err != nil {
				log.Printf("[Collect] ⚠️ 关闭前补交未定稿窗口 %d 失败: %v",
					pendingEvent.StartTime, err)
			} else {
				log.Printf("[Collect] 关闭前补交未定稿窗口 %d（trades=%d, close_source=%s）",
					pendingEvent.StartTime, len(pendingEvent.Trades), pendingEvent.CloseSource)
			}
		}
	}()

	log.Println("========================================")
	log.Printf(" 高频数据采集 v2 — %s（资产 %s）| 输出: %s（全新目录）",
		slugPrefix, asset.Name, outputDir)
	log.Printf(" 数据源: [Binance %s aggTrade+depth20 1s聚合] + [PM CLOB books 1s快照] + "+
		"[PM last_trade_price 秒聚合] + [Chainlink TWAP-60 %s]",
		asset.ApplyBinance(cfg.Binance).Symbol, asset.Chainlink)
	log.Printf(" 边界价: 边界那一秒的 TWAP 推送（精确匹配, 决策 #15/#19）; 推送缺失才走官方兜底")
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
		slug := fmt.Sprintf("%s-%d", slugPrefix, nextStart.Unix())

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

		// 步骤 2b: 精确取锚（决策 #15）——窗口开局起 500ms 节拍 × 40 次 = 20s，
		// 取**评估时刻恰好等于边界**的那条推送（= 官方 openPrice，逐位同源），
		// 命中即终局，通道关闭。官方 HTTP 段显式休眠（fetch=nil, Schedule 空）。
		// 兜底初值取 Latest()（到达口径，即 t=0 口径）——只在精确推送始终没到时
		// 用作近似锚，并以 anchor_source=stream 留痕 + 触发官方修正。
		anchorPrice, anchorSrc := 0.0, collect.SourceStream
		if p, age := twapAdapter.Latest(); p > 0 {
			anchorPrice = p
			log.Printf("[Cycle] 锚初值 %.2f（到达口径, age=%dms; 待边界那一秒的推送替换）", p, age)
		}
		anchorCh := feed.RecoverAnchor(ctx, nil, twapAdapter.PushNearest, nextStart,
			nextStart.Add(collect.WindowSec*time.Second),
			feed.AnchorUpgradeOpts{Attempts: anchorExactAttempts, Interval: anchorExactInterval})

		// 步骤 3: Binance K 线开盘价 —— 异步获取，不阻塞采集启动。
		// 新 K 线在边界后 1-2s 才稳定生成，goroutine 内稍等再取；
		// 初值用当前价，K 线到达后经 klineCh 在采集循环内修正事件元数据。
		snap := binance.LatestData()
		binanceOpen := snap.Price
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
		if yesTokenID == "" || noTokenID == "" {
			// gamma 结构异常或市场未上架 token：订阅空 token 会静默采一整窗空数据
			log.Printf("[Cycle] ⚠️ 市场 %s token 解析为空（YES=%q NO=%q），跳过本窗口",
				slug, yesTokenID, noTokenID)
			waitTo := nextStart.Add(collect.WindowSec * time.Second)
			select {
			case <-ctx.Done():
				return
			case <-time.After(time.Until(waitTo)):
			}
			continue
		}
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
		prevYesTok, prevNoTok = yesTok, noTok
		yesTok = yesTokenID
		noTok = noTokenID
		tokMu.Unlock()
		bookMu.Lock()
		yesBook = nil
		noBook = nil
		bookMu.Unlock()

		// 步骤 6: 窗口状态
		// 先定稿上一窗口（补拍边界迟到的尾盘成交）再安装新 bucketer。
		// finalizeWindow 在独立 goroutine 里等收盘推送，主循环立即继续。
		if pendingEvent != nil {
			pendingEvent.Trades = currentBucketer.Load().Snapshot()
			go finalizeWindow(pendingEvent, pendingClose, settlementWorker)
			pendingEvent, pendingClose = nil, nil
		}
		currentBucketer.Store(collect.NewTradeBucketer(nextStart.UnixMilli()))
		endTime := nextStart.Add(collect.WindowSec * time.Second)
		var ticks []collect.HFTick

		log.Printf("[Cycle] event=%s binance_open=%.2f 开始采集...", conditionID, binanceOpen)

		// 丢弃窗口间隙累计的成交量：上一窗口 finalize + 官方收盘轮询 +
		// 市场切换期间的 Binance 成交不属于本窗口首秒，否则首 tick 会出现
		// 周期性量尖峰（数据质量 bug，实测发现）。
		binance.ConsumeVolume()

		// 下一窗口市场预取：采集最后 20s 内异步启动（结果写入主作用域
		// nextMarketCache，跨迭代传递），窗口结束即可无缝订阅下一窗口。
		nextPrefetched := false

		ticker := time.NewTicker(time.Second)

		// sampleTick 采集一个 1s 快照行（tick 定时器与窗口终点补采共用）。
		// P0-1: 先读累计（价格/深度快照 + 自上次 ConsumeVolume 以来的
		// 买卖量与成交笔数），再重置 —— bd 中即本秒增量。
		sampleTick := func(tickTime time.Time) {
			rem := max(int(endTime.Sub(tickTime).Seconds()), 0)
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
				PM:   collect.MakePMTick(yb, nb),
				Twap: collect.TwapTick{Price: twPrice, AgeMs: twAge},
			})
		}

	collectLoop:
		for {
			select {
			case <-ctx.Done():
				ticker.Stop()
				return

			case rec, ok := <-anchorCh:
				if !ok {
					anchorCh = nil // 预算耗尽（20s）且无锚：停用该分支
					n, newestOff, noTs := twapAdapter.CacheStat()
					log.Printf("[Anchor] ⚠️ 窗口 %s 未取到边界那一秒的 open（缓存 %d 条, "+
						"最新一条评估偏移 %+dms, 缺时间戳 %d 条）——沿用到达口径近似锚 %.2f",
						conditionID, n, newestOff, noTs, anchorPrice)
					break
				}
				anchorPrice, anchorSrc = rec.Price, anchorSource(rec.Source)
				log.Printf("[Anchor] ✅ 窗口 %s 锚 %.2f（%s, 边界后 +%dms）",
					conditionID, rec.Price, rec.Source, rec.AtMs-nextStart.UnixMilli())
				anchorCh = nil // 精确匹配下不可能有更好的取值, 命中即终局

			case klineOpen := <-klineCh:
				if klineOpen > 0 {
					binanceOpen = klineOpen
					log.Printf("[Cycle] ✅ Binance K 线开盘价 %.2f 已修正", klineOpen)
				} else {
					log.Printf("[Cycle] ⚠️  K 线开盘价不可用，沿用当前价 %.2f", binanceOpen)
				}

			case tickTime := <-ticker.C:
				if !tickTime.Before(endTime) {
					ticker.Stop()
					break collectLoop
				}
				sampleTick(tickTime)

				// 窗口尾部：异步预取下一窗口市场（只触发一次），
				// 窗口结束即可无缝订阅下一窗口
				if !nextPrefetched && time.Until(endTime) <= prefetchLead {
					nextPrefetched = true
					nextSlug := fmt.Sprintf("%s-%d", slugPrefix,
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
		// 补采窗口终点快照（rem=0）：覆盖最后一秒的成交量增量。
		// 旧实现 rem 截断在窗口结束前 1s 提前 break，最后一秒数据缺失
		// （实测每窗口 298 ticks 而非 300）。
		sampleTick(endTime)

		// 步骤 7: 窗口封存（ticks 部分）→ 待下一轮步骤 6 补拍 trades 后提交。
		// 收盘价初值取流采样（到达口径），同时**立即**起收盘推送等待 goroutine：
		// 精确匹配「评估时刻 == 闭市那一秒」的那条推送（= 官方 closePrice）。
		// 推送实测 p50 +2.0s 到达（880 个实盘窗里只有 +1s/+2s 两档、无一晚于 +2s），
		// 预算 10s = 5 倍上界（决策 #19 的推送层时点）。迟到或缺失 ⇒ 事件行留
		// close_source=stream 并触发官方修正层。
		// ⚠️ 不在此处 Store(nil) bucketer：旧 bucketer 保留到下一轮步骤 6，
		// 边界迟到的尾盘成交仍可入桶并被 snapshot 捕获。
		streamClose, streamCloseAge := twapAdapter.Latest()
		if streamClose == 0 {
			streamClose = anchorPrice
		}
		pendingClose = waitPushAsync(twapAdapter, endTime, closePushWait)
		pendingEvent = &collect.Event{
			ConditionID:    conditionID,
			Slug:           slug,
			StartTime:      nextStart.Unix(),
			TwapOpenPrice:  anchorPrice,
			TwapClosePrice: streamClose,
			CloseSource:    collect.SourceStream,
			AnchorSource:   anchorSrc,
			Outcome:        outcomeFor(anchorPrice, streamClose),
			BinanceOpen:    binanceOpen,
			Ticks:          ticks,
		}
		if anchorPrice <= 0 {
			// 红线（决策 #15）: 宁可丢窗也不留 anchor=0 的行——下游（python 分析）
			// 拿 twap_open_price 当锚，0 会静默产出 inf/垃圾特征。
			log.Printf("[Collect] ⚠️ 窗口 %s 锚与近似锚均不可用（无推送）——本窗不落盘", slug)
			pendingEvent, pendingClose = nil, nil
			continue
		}
		log.Printf("[Cycle] 窗口 %s 采集完成: ticks=%d 流值 close=%.2f（age=%dms）, 待精确收盘推送",
			slug, len(ticks), streamClose, streamCloseAge)
	}
}

// anchorSource 把 feed 的锚来源词表翻译成事件行的 close_source/anchor_source 词表。
//
// ⚠️ 两个包的 "stream" **不是一回事**（同名不同义，最容易串的地方）:
//   - feed.AnchorSourceStream   = 评估时刻恰好等于窗口边界的那条 TWAP 推送
//     ——即精确边界推送（决策 #15），它就是官方 openPrice（决策 #19）⇒ collect.SourcePush
//   - collect.SourceStream      = **到达口径**采样（Latest()，可能陈旧 ~2s）
//     ——采集侧的兜底初值，是近似锚 ⇒ 本函数不会产出它，只有调用点的初值用它
//
// 不翻译直接落盘的话，取锚成功的窗会被标成"到达口径"，进而被误判为需要官方修正。
func anchorSource(feedSrc string) string {
	switch feedSrc {
	case feed.AnchorSourceOfficial:
		return collect.SourceOfficial
	default: // feed.AnchorSourceStream（精确边界推送）
		return collect.SourcePush
	}
}

// outcomeFor 按事件行口径判胜负: close >= open → Up(0)，否则 Down(1)。
func outcomeFor(open, close float64) int {
	if close >= open {
		return 0
	}
	return 1
}

// waitPushAsync 在后台等「评估时刻恰好等于 at」的那条 TWAP 推送，返回结果通道
// （命中为价格，预算耗尽为 0）。**必须异步**：调用点是窗口结束，主循环要立刻
// 回去开下一个窗口——边界后停留超过 2s 会让步骤 1 的宽限判据把整个下一窗跳过。
func waitPushAsync(ad *feed.TwapAdapter, at time.Time, budget time.Duration) <-chan float64 {
	ch := make(chan float64, 1)
	go func() {
		deadline := time.Now().Add(budget)
		for {
			if p, _, ok := ad.PushNearest(at); ok {
				ch <- p
				return
			}
			if !time.Now().Before(deadline) {
				ch <- 0
				return
			}
			time.Sleep(closePushPoll)
		}
	}()
	return ch
}

// finalizeWindow 定稿一个窗口：等精确收盘推送（≤ closePushWait）→ 补丁价格/来源/
// outcome → 交 SettlementWorker（推送缺失时才排队官方修正）。
// 由步骤 6 以 goroutine 启动：等待期间主循环已在跑下一个窗口的 tick。
func finalizeWindow(ev *collect.Event, closeCh <-chan float64, w *collect.SettlementWorker) {
	if p := <-closeCh; p > 0 {
		ev.TwapClosePrice = p
		ev.CloseSource = collect.SourcePush
		ev.Outcome = outcomeFor(ev.TwapOpenPrice, p)
	}
	needsCorrection := ev.CloseSource != collect.SourcePush || ev.AnchorSource != collect.SourcePush
	w.Submit(ev, needsCorrection)
}

// parseFloat 解析价格/数量字符串，失败返回 0（SDK last_trade_price 字段为字符串）。
func parseFloat(s string) float64 {
	f, _ := strconv.ParseFloat(s, 64)
	return f
}

const (
	// prefetchLead 是市场信息预取提前量：窗口边界前多少秒开始调
	// FetchMarketBySlug 并缓存结果（市场在窗口开始前已上架 gamma）。
	// 边界一到用缓存立即订阅新市场，平滑过渡到下一个窗口的采集。
	prefetchLead = 20 * time.Second

	// twapMaxStale 是 TWAP 推送新鲜度阈值：超过该时长未收到推送则重建订阅
	// （正常推送间隔 ~2s，冻结场景 age 无限增长）。
	twapMaxStale = 2 * time.Minute

	// 精确取锚（决策 #15）的节拍与预算，与 cmd/flip 同值：边界那一秒的推送
	// 实测 p50 +2.0s / p90 +2.3s 到达，500ms × 40 = 20s 即 p50 的 10 倍余量。
	anchorExactAttempts = 40
	anchorExactInterval = 500 * time.Millisecond

	// closePushWait 是收盘推送的等待预算（决策 #19 的推送层时点: 闭市 +10s）。
	// 闭市那一秒的推送实测只有 +1s/+2s 两档、无一晚于 +2s。
	closePushWait = 10 * time.Second
	// closePushPoll 是收盘推送的轮询节拍（与取锚通道同为 500ms）。
	closePushPoll = 500 * time.Millisecond
)
