// Command flip 是「自信崩溃」翻转策略引擎入口（docs/strategy_plan_2026-08-31.md）。
//
// 连接 Polymarket CLOB WebSocket（UP/DOWN 订单簿）与 Chainlink TWAP-60（诊断），
// 每秒驱动 flip.Engine 状态机检测穿越信号，纸面模拟成交（PaperExecutor），
// 官方结算后记录完整 P&L 到 JSONL（按日切分）。
//
// 纸面/实盘同源：成交执行由 -mode 区分（live 未实现，纸面验证后接入）。
//
// 用法：
//
//	go run ./cmd/flip -output data/v3 -dashboard :8090
//	go run ./cmd/flip --trigger-bid-min 0.73 --post-end-max 0.66 --stake 2
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
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"

	"github.com/necklace/flip-signal/internal/collect"
	"github.com/necklace/flip-signal/internal/dashboard"
	"github.com/necklace/flip-signal/internal/feed"
	"github.com/necklace/flip-signal/internal/flip"
	"github.com/necklace/flip-signal/internal/trading"
)

// windowSec 是 btc-updown-5m 窗口长度（秒）。
const windowSec = 300

// prefetchLead 是市场信息预取提前量：窗口边界前多少秒开始调
// FetchMarketBySlug 并缓存结果（与 cmd/collect 同款约定）。
const prefetchLead = 20 * time.Second

func init() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
}

func main() {
	// ── CLI 参数（与回测脚本同名，优先级最高）──
	outputDir := flag.String("output", "data/v3", "信号 JSONL 输出目录（按日切分）")
	dashboardAddr := flag.String("dashboard", "", "Dashboard 监听地址（如 :8090）")
	slugPrefix := flag.String("slug", "btc-updown-5m", "Polymarket slug 前缀")
	mode := flag.String("mode", "paper", "成交模式: paper|live（live 未实现）")
	triggerThreshold := flag.Float64("trigger-threshold", 0.7, "穿越阈值（触发侧 bid 首次超过）")
	triggerBidMin := flag.Float64("trigger-bid-min", 0.73, "C1 高度自信: 穿越时刻触发侧 bid 下限")
	postEndMax := flag.Float64("post-end-max", 0.66, "C2 快速崩溃: 确认时刻触发侧 bid 上限")
	stake := flag.Float64("stake", 2, "每信号投入 USDC")
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// ── 策略配置 ──
	cfg := flip.Config{
		TriggerThreshold: *triggerThreshold,
		TriggerBidMin:    *triggerBidMin,
		PostEndMax:       *postEndMax,
		ConfirmSec:       10, // +10s 确认窗口（+2s 变体已证伪，勿改）
		MaxRemaining:     260,
		MinRemaining:     15,
		Stake:            *stake,
	}
	executor, err := flip.NewExecutor(cfg, flip.ExecMode(*mode))
	if err != nil {
		log.Fatalf("[Flip] %v", err)
	}

	// ── Polymarket 客户端（未配置私钥则自动生成临时密钥，只读运行）──
	cfgSDK := defaultSDKConfig()
	readOnly := false
	if cfgSDK.Polymarket.OwnerKey == "" {
		key := make([]byte, 32)
		if _, err := rand.Read(key); err != nil {
			log.Fatalf("生成临时密钥失败: %v", err)
		}
		cfgSDK.Polymarket.OwnerKey = hex.EncodeToString(key)
		readOnly = true
	}
	client := sdk.NewClient(&cfgSDK)
	if readOnly {
		log.Println("[Flip] ⚠️  未配置 POLYMARKET_OWNER_KEY —— 只读运行（纸面交易）")
	}

	// ── PM 订单簿订阅（SDK MarketMonitor，参照 cmd/collect 模式）──
	monitor := sdk.NewMarketMonitor(cfgSDK.Polymarket.ClobWSBaseURL, false, client, false)
	var (
		subMu     sync.RWMutex
		subTokens []string
		tokMu     sync.RWMutex
		upTok     string
		downTok   string
		bookMu    sync.RWMutex
		upBook    *sdk.OrderBook
		downBook  *sdk.OrderBook
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
				yt, nt := upTok, downTok
				tokMu.RUnlock()
				bookMu.Lock()
				switch book.AssetId {
				case yt:
					upBook = book
				case nt:
					downBook = book
				}
				bookMu.Unlock()
			}
		}
	}()
	// monitor 重启恢复（参照 collect：Run 退出后 SDK 清空订阅，无条件重启）
	go func() {
		for {
			err := monitor.Run(ctx)
			if ctx.Err() != nil {
				return
			}
			if err != nil {
				log.Printf("[Flip] ⚠️ MarketMonitor 异常退出: %v —— 5 秒后重启", err)
			} else {
				log.Printf("[Flip] ⚠️ MarketMonitor 干净退出 —— 5 秒后重启")
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

	// ── Chainlink TWAP-60（诊断字段，策略本身不用 BTC 特征）──
	// 注意 symbol 后缀: SDK 将 "BTC" 解析为 30s 窗口，须显式 "BTC_60" 才订阅 twap_sixty
	twapMonitor := sdk.NewCryptoPriceMonitor(client, sdk.MonitorChainlinkTwap, "BTC_60")
	twapAdapter := feed.NewTwapAdapter(twapMonitor, "BTC", sdk.ChainlinkTwapWindowSixty)
	twapAdapter.Start(ctx)
	go func() {
		for {
			err := twapMonitor.Run(ctx)
			if err == nil || ctx.Err() != nil {
				return
			}
			log.Printf("[Twap] ⚠️ monitor 异常退出: %v —— 5 秒后重启", err)
			select {
			case <-ctx.Done():
				return
			case <-time.After(5 * time.Second):
			}
		}
	}()

	// ── 记录器与结算轮询 ──
	recorder, err := flip.NewRecorder(*outputDir)
	if err != nil {
		log.Fatalf("[Flip] 记录器创建失败: %v", err)
	}
	defer recorder.Close()

	resolutionPoller := trading.NewResolutionPoller(
		client.FetchMarketBySlug,
		10*time.Second,
		func(conditionID string, outcome int) {
			if err := recorder.Resolve(conditionID, outcome); err != nil {
				log.Printf("[Flip] 结算失败: %v", err)
			}
		},
	)
	go resolutionPoller.Run(ctx)

	// ── 运行时状态（Dashboard 数据源）──
	runtime := &runtimeState{
		Engine:      flip.NewEngine(cfg),
		Executor:    executor,
		Recorder:    recorder,
		TwapAdapter: twapAdapter,
		Mode:        *mode,
		StartedAt:   time.Now(),
	}
	runtime.books = func() (*sdk.OrderBook, *sdk.OrderBook) {
		bookMu.RLock()
		defer bookMu.RUnlock()
		return upBook, downBook
	}
	runtime.tokens = func() (string, string) {
		tokMu.RLock()
		defer tokMu.RUnlock()
		return upTok, downTok
	}

	// ── Dashboard（手机浏览器兼容的单页前端）──
	if *dashboardAddr != "" {
		dashState := dashboard.NewState(recorder, runtime, cfg, *mode)
		go dashState.ListenAndServe(*dashboardAddr)
	}

	log.Println("========================================")
	log.Printf(" Flip Signal Detection — 纸面交易（mode=%s）", *mode)
	log.Printf(" 输出: %s  |  Slug: %s", *outputDir, *slugPrefix)
	log.Printf(" 参数: trigger>%.2f C1 bid>%.2f C2 post_end≤%.2f stake=%.0fUSDC confirm=+%ds",
		cfg.TriggerThreshold, cfg.TriggerBidMin, cfg.PostEndMax, cfg.Stake, cfg.ConfirmSec)
	log.Println(" 数据源: [PM CLOB books 1s] + [Chainlink TWAP-60 诊断]")
	log.Println("========================================")

	// ── 市场周期主循环 ──
	for {
		select {
		case <-ctx.Done():
			log.Println("[Flip] 正在关闭...")
			return
		default:
		}

		// 步骤 1: 对齐下一个 5 分钟窗口；已开始则等下一个完整窗口
		now := time.Now()
		alignedTs := now.Unix() / windowSec * windowSec
		nextStart := time.Unix(alignedTs, 0)
		if time.Until(nextStart.Add(2*time.Second)) <= 0 {
			nextStart = nextStart.Add(windowSec * time.Second)
		}
		slug := fmt.Sprintf("%s-%d", *slugPrefix, nextStart.Unix())

		// 步骤 2: 边界前 20s 预取市场信息（缓存，失败 5s 重试）
		var cachedMarket *gjson.Result
		var cachedErr error
		if wait := time.Until(nextStart.Add(-prefetchLead)); wait > 0 {
			log.Printf("[Cycle] 下一窗口 %s, 等待 %v（边界前 %ds 预取）",
				nextStart.UTC().Format(time.RFC3339), wait.Round(time.Second),
				int(prefetchLead/time.Second))
			select {
			case <-ctx.Done():
				return
			case <-time.After(wait):
			}
		}
		for time.Until(nextStart) > 2*time.Second {
			cachedMarket, cachedErr = client.FetchMarketBySlug(slug)
			if cachedErr == nil {
				log.Printf("[Cycle] ✅ 市场预取成功 %s（边界前 %.1fs）", slug, time.Until(nextStart).Seconds())
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
		if wait := time.Until(nextStart); wait > 0 {
			select {
			case <-ctx.Done():
				return
			case <-time.After(wait):
			}
		}

		// 步骤 3: 市场信息 → conditionId + token
		var marketData *gjson.Result
		if cachedMarket != nil && cachedErr == nil {
			marketData = cachedMarket
		} else {
			var err error
			marketData, err = client.FetchMarketBySlug(slug)
			if err != nil {
				log.Printf("[Cycle] 获取市场失败: %v —— 跳过本窗口", err)
				select {
				case <-ctx.Done():
					return
				case <-time.After(time.Until(nextStart.Add(windowSec * time.Second))):
				}
				continue
			}
		}
		conditionID := marketData.Get("conditionId").String()
		upTokenID, downTokenID := collect.ParseMarketTokens(marketData)
		if upTokenID == "" || downTokenID == "" {
			log.Printf("[Cycle] ⚠️ 市场 %s token 解析为空，跳过本窗口", slug)
			select {
			case <-ctx.Done():
				return
			case <-time.After(time.Until(nextStart.Add(windowSec * time.Second))):
			}
			continue
		}
		log.Printf("[Cycle] conditionId=%s UP=%s DOWN=%s", conditionID, upTokenID, downTokenID)

		// 步骤 4: 订阅切换（先退订旧 token，保留副本供 monitor 重启恢复）
		subMu.Lock()
		if len(subTokens) > 0 {
			monitor.UnsubscribeTokens(subTokens...)
		}
		subTokens = []string{upTokenID, downTokenID}
		subMu.Unlock()
		monitor.SubscribeTokens(upTokenID, downTokenID)

		tokMu.Lock()
		upTok, downTok = upTokenID, downTokenID
		tokMu.Unlock()
		bookMu.Lock()
		upBook, downBook = nil, nil
		bookMu.Unlock()

		// 步骤 5: 启动事件采集与检测
		endTime := nextStart.Add(windowSec * time.Second)
		engine := flip.NewEngine(cfg)
		runtime.Engine = engine
		runtime.ConditionID = conditionID
		runtime.Slug = slug
		runtime.EventStart = nextStart.Unix()
		log.Printf("[Cycle] event=%s 开始采集（窗口 %s）...", conditionID, slug)

		ticker := time.NewTicker(time.Second)
		lastTick := flip.Tick{}

	collectLoop:
		for {
			select {
			case <-ctx.Done():
				ticker.Stop()
				return
			case tickTime := <-ticker.C:
				rem := int(endTime.Sub(tickTime).Seconds())
				if rem < 0 {
					rem = 0
				}
				lastTick = sampleTick(tickTime, rem, runtime)

				// 引擎驱动（穿越/确认/判定）
				if c := engine.ProcessTick(lastTick); c != nil {
					handleCross(c, runtime, cfg.TriggerThreshold)
				}
				if rem == 0 {
					ticker.Stop()
					break collectLoop
				}
				// 每 30s 打印一次窗口进度（rem 每秒递减，无重复）
				if rem%30 == 0 {
					log.Printf("[Event] %s rem=%ds up=%.3f/%.3f down=%.3f/%.3f state=%s",
						conditionID, rem, lastTick.UpBid, lastTick.UpAsk,
						lastTick.DownBid, lastTick.DownAsk, engine.State())
				}
			}
		}

		// 步骤 6: 窗口结束 → Finalize（补判定 + cls）→ 记录 → 注册结算
		res := engine.Finalize(lastTick)
		if res.Cross != nil {
			if err := recorder.RecordCross(conditionID, slug, nextStart.Unix(), res.Cross, res.Cls); err != nil {
				log.Printf("[Flip] 记录失败: %v", err)
			} else {
				status := "失败"
				if res.Cross.OK {
					status = "🎯 信号"
				}
				log.Printf("[Event] %s 穿越观测: %s side=%s trigger=%.3f post_end=%.3f fill=%.3f shares=%.1f (%s)",
					conditionID, status, res.Cross.Side, res.Cross.TriggerBid,
					res.Cross.PostEnd, res.Cross.Fill, res.Cross.Shares, res.Cross.RejectReason)
			}
		} else {
			log.Printf("[Event] %s 无穿越观测 cls=%s", conditionID, res.Cls)
		}

		// 注册异步结算（gamma umaResolutionStatus → Resolve 回填 P&L）
		resolutionPoller.Register(conditionID, slug)
	}
}

// runtimeState 聚合主循环需要共享的组件引用（Dashboard 数据源）。
type runtimeState struct {
	Engine      *flip.Engine
	Executor    flip.Executor
	Recorder    *flip.Recorder
	TwapAdapter *feed.TwapAdapter
	ConditionID string
	Slug        string
	EventStart  int64
	Mode        string    // 成交模式: paper/live
	StartedAt   time.Time // 进程启动时刻
	books       func() (*sdk.OrderBook, *sdk.OrderBook)
	tokens      func() (string, string)
}

// Snapshot 实现 dashboard.Snapshotter（Dashboard 每 5s 轮询取快照，无锁读）。
func (rt *runtimeState) Snapshot() dashboard.LiveSnapshot {
	yb, nb := rt.books()
	pm := collect.MakePMTick(yb, nb)
	_, twAge := rt.TwapAdapter.Latest()
	return dashboard.LiveSnapshot{
		Mode:        rt.Mode,
		StartedAt:   rt.StartedAt,
		ConditionID: rt.ConditionID,
		Slug:        rt.Slug,
		EventStart:  rt.EventStart,
		EngineState: rt.Engine.State(),
		UpBid:       pm.UpBid,
		UpAsk:       pm.UpAsk,
		DownBid:     pm.DownBid,
		DownAsk:     pm.DownAsk,
		BookLatMs:   pm.BookLatMs,
		TwapAgeMs:   twAge,
	}
}

// sampleTick 读取当前盘口构造一条引擎 tick（1s 粒度）。
func sampleTick(t time.Time, rem int, rt *runtimeState) flip.Tick {
	yb, nb := rt.books()
	pm := collect.MakePMTick(yb, nb)
	// TWAP-60 仅作诊断（TwapAgeMs），策略本身不用任何 BTC 特征
	_, twAge := rt.TwapAdapter.Latest()
	return flip.Tick{
		Ts:        t.UnixMilli(),
		Rem:       rem,
		UpBid:     pm.UpBid,
		UpAsk:     pm.UpAsk,
		DownBid:   pm.DownBid,
		DownAsk:   pm.DownAsk,
		BookLatMs: pm.BookLatMs,
		TwapAgeMs: twAge,
	}
}

// handleCross 处理引擎产出的穿越判定（成功或失败）。
func handleCross(c *flip.Cross, rt *runtimeState, triggerThreshold float64) {
	if !c.OK {
		log.Printf("[Flip] 穿越否决: side=%s trigger=%.3f post_end=%.3f reason=%s",
			c.Side, c.TriggerBid, c.PostEnd, c.RejectReason)
		return
	}
	// 信号 → 纸面执行（PaperExecutor 恒 filled；live 模式后续接入）
	res, err := rt.Executor.Execute(c)
	if err != nil {
		log.Printf("[Trading] ⚠️ 信号未执行: %v", err)
		return
	}
	log.Printf("[Flip] 🎯 SIGNAL: %s>%.2f C1(%.3f) C2(%.3f) fill=%.3f shares=%.1f | status=%s avg_fill=%.3f",
		c.Side, triggerThreshold, c.TriggerBid, c.PostEnd, c.Fill, c.Shares,
		res.Status, res.AvgFillPrice)
}

// defaultSDKConfig 构造 SDK 配置（环境变量覆盖，参照 cmd/collect 约定）。
func defaultSDKConfig() sdk.Config {
	cfg := sdk.Config{
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
	}
	if v := os.Getenv("POLYMARKET_PROXY"); v != "" {
		cfg.SocksProxy = v
	}
	return cfg
}
