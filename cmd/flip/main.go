// Command flip 是「狗@0.2」策略引擎入口（口径文档 docs/engine_plan_dog020_2026-09-02.md）。
//
// 连接 Polymarket CLOB WebSocket（UP/DOWN 订单簿）、Chainlink TWAP-60（锚/σ）与
// Binance BTCUSDT spot（浅洞腿），每秒驱动 flip.Engine 状态机检测「触底观测」——
// 某侧 ask 首次砸到 ≤0.20 的下狗机会：急跌(m_45) × 浅洞(dist_s) × 时间(rem) 三腿
// 全过即 ok 信号，纸面模拟成交（PaperExecutor），官方结算后回填完整 P&L 到 JSONL
// （touches_YYYY-MM-DD.jsonl，按日切分）。成功与失败的观测都落盘（频率校准用）。
//
// 结算轮询只注册 ok 信号；崩溃后重启按磁盘 pending 恢复注册。
//
// 用法：
//
//	go run ./cmd/flip -output data/v4 -dashboard :8090
//	go run ./cmd/flip --trigger-ask-max 0.2 --crash-min-ask 0.4 --stake 2
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
	"sync"
	"syscall"
	"time"

	"github.com/tidwall/gjson"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"

	"github.com/necklace/flip-signal/internal/dashboard"
	"github.com/necklace/flip-signal/internal/feed"
	"github.com/necklace/flip-signal/internal/flip"
	"github.com/necklace/flip-signal/internal/trading"
)

// windowSec 是 btc-updown-5m 窗口长度（秒）。
const windowSec = 300

// prefetchLead 是市场信息预取提前量：窗口边界前多少秒开始调
// FetchMarketBySlug 并缓存结果。
const prefetchLead = 20 * time.Second

// lateLimit 是订阅迟到阈值：已落后窗口边界超过该时长则跳过本窗口。
// v4 从窗口起点就开始观测判定（无 v3 前 40s 检测盲区），迟到 >15s 意味着
// 盘口证据缺失 15 个槽位，会把 m_45 急跌窗的头部真实打薄——整窗跳过
// 防假截断（迟到 ≤15s 时 45 槽窗仍基本完整，与回测 ±1-2 tick 相位差同级）。
const lateLimit = 15 * time.Second

// spotFreshMs 是 Binance spot 新鲜度阈值: 距本地接收 >此毫秒判现货缺失。
// BTC 常态每秒多笔成交，>2s 无推送基本等于链路断流；用本地接收时刻而非
// 交易所成交时间戳（链路排队/服务器时钟都会让后者失真，见 BinanceAdapter）。
const spotFreshMs = 2000

// twapMaxStale 是 TWAP 推送新鲜度阈值: 超过该时长未收到推送则重建订阅
// （2026-09-01 服务器实测断流事件，TwapAdapter 内建看门狗，见其注释）。
const twapMaxStale = 2 * time.Minute

// twapLookbackSeconds 是结算口径 TWAP 回看窗口秒数（Chainlink TWAP-60）。
const twapLookbackSeconds = 60

// histWindows 是 σ 滚动窗容量（前 ≤18 个已完成窗口的振幅均值）——
// 与回测 hist 窗口数一致（python/v4/01_backtest_r1.py）。
const histWindows = 18

// histMin 是 σ 可用所需最少窗口数: 不足则 no_hist（冷启动期）。
const histMin = 3

// marketCache 缓存下一窗口的市场信息（稳态预取: 本窗 tick 尾部预取，loop 顶部复用）。
type marketCache struct {
	slug string
	res  *gjson.Result
}

// histState 维护 σ 的滚动窗口: hist_bps = 前 ≤histWindows 个已完成窗口
// |tw_close − tw_open| 的均值，换算成 bps = mean/anchor·1e4（与回测口径一致，
// dist = Δ价/anchor·1e4/hist_bps）。
//
// 启动时用官方历史范围预热（feed.FetchTwapRanges，消除冷启动 no_hist 期）；
// live 每窗口结束追加本窗 |close − anchor|。push 严格发生在窗口结束后——
// σ 永远只用已结束窗口，不混入当前窗。
type histState struct {
	mu   sync.Mutex
	vals []float64 // 振幅（$），时间正序
}

func newHistState() *histState { return &histState{} }

// seed 预热: 官方范围整表替换（热启动段在 ~19s 内完成，先于任何 live push）。
func (h *histState) seed(vals []float64) {
	if len(vals) == 0 {
		return
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(vals) > histWindows {
		vals = vals[len(vals)-histWindows:] // 只留最近的
	}
	h.vals = append(h.vals[:0], vals...)
}

// push 窗口结束后追加一个振幅（$）。
func (h *histState) push(amp float64) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.vals = append(h.vals, amp)
	if len(h.vals) > histWindows {
		h.vals = append(h.vals[:0], h.vals[len(h.vals)-histWindows:]...)
	}
}

// bps 返回当前可用 σ（bps 口径）；不足 histMin 窗返回 0（不可用）。anchor ≤0 恒不可用。
func (h *histState) bps(anchor float64) float64 {
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(h.vals) < histMin || anchor <= 0 {
		return 0
	}
	var sum float64
	for _, v := range h.vals {
		sum += v
	}
	return sum / float64(len(h.vals)) / anchor * 1e4
}

// count 返回已收集窗口数（日志用）。
func (h *histState) count() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.vals)
}

func init() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
}

func main() {
	// ── CLI 参数（与回测脚本同名，优先级最高）──
	outputDir := flag.String("output", "data/v4", "观测 JSONL 输出目录（按日切分）")
	dashboardAddr := flag.String("dashboard", "", "Dashboard 监听地址（如 :8090）")
	slugPrefix := flag.String("slug", "btc-updown-5m", "Polymarket slug 前缀")
	mode := flag.String("mode", "paper", "成交模式: paper|live（live 未实现）")
	triggerAskMax := flag.Float64("trigger-ask-max", 0.20, "触发阈值: 某侧 ask ≤ 此值 即触底观测")
	crashMinAsk := flag.Float64("crash-min-ask", 0.40, "急跌腿: m_45 窗内同侧 ask 曾 ≥ 此值")
	crashWindow := flag.Int("crash-window", 45, "急跌窗: 触发前 N 个 tick 槽位内求 max")
	distLo := flag.Float64("dist-lo", -0.5, "浅洞带下界: dist_s 必须 > 此值（开区间）")
	distHi := flag.Float64("dist-hi", 0.0, "浅洞带上界: dist_s 必须 < 此值（开区间）")
	remMin := flag.Int("rem-min", 180, "时间腿: 仅 rem > 此值的触发有效")
	stake := flag.Float64("stake", 2, "每信号投入 USDC")
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// ── 策略配置 ──
	cfg := flip.Config{
		TriggerAskMax: *triggerAskMax,
		CrashMinAsk:   *crashMinAsk,
		CrashWindow:   *crashWindow,
		DistLo:        *distLo,
		DistHi:        *distHi,
		RemMin:        *remMin,
		Stake:         *stake,
	}
	executor := flip.NewExecutor(*mode)

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
		log.Println("[Dog] ⚠️  未配置 POLYMARKET_OWNER_KEY —— 只读运行（纸面交易）")
	}

	// ── PM 订单簿订阅（SDK MarketMonitor）──
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
	// monitor 重启恢复（SDK 清空订阅，无条件重启）
	go func() {
		for {
			err := monitor.Run(ctx)
			if ctx.Err() != nil {
				return
			}
			if err != nil {
				log.Printf("[Dog] ⚠️ MarketMonitor 异常退出: %v —— 5 秒后重启", err)
			} else {
				log.Printf("[Dog] ⚠️ MarketMonitor 干净退出 —— 5 秒后重启")
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

	// ── Chainlink TWAP-60（anchor/σ 数据源，结算口径）──
	// symbol 后缀: SDK 将 "BTC" 解析为 30s 窗口，须显式 "BTC_60" 才订阅 twap_sixty。
	twapAdapter := feed.NewTwapAdapter(client, "BTC", sdk.ChainlinkTwapWindowSixty, twapMaxStale)
	twapAdapter.StartWithMonitor(ctx)

	// ── Binance BTCUSDT spot（浅洞腿现货参考价，不出单）──
	// Start 首次拨号失败不自愈（返回 err），外层包装指数退避重试直至连上；
	// 后续断线由 runReadLoop 自愈重连。拨号走 http.ProxyFromEnvironment
	// （部署机勿设指向不通代理的 HTTP(S)_PROXY）。
	binance := feed.NewBinanceAdapter()
	go func() {
		backoff := time.Second
		for {
			err := binance.Start(ctx)
			if err == nil || ctx.Err() != nil {
				return
			}
			log.Printf("[Binance] ⚠️ 首次拨号失败: %v —— %v 后重试", err, backoff.Round(time.Millisecond))
			select {
			case <-ctx.Done():
				return
			case <-time.After(backoff):
			}
			backoff = min(backoff*2, 30*time.Second)
		}
	}()

	// ── 记录器与结算轮询 ──
	recorder, err := flip.NewRecorder(*outputDir)
	if err != nil {
		log.Fatalf("[Dog] 记录器创建失败: %v", err)
	}
	defer recorder.Close()

	resolutionPoller := trading.NewResolutionPoller(
		client.FetchMarketBySlug,
		10*time.Second,
		func(conditionID string, outcome int) error {
			// 命中返回 true（含已结算重复命中）；未命中说明 pending 已移除/不存在
			recorder.Resolve(conditionID, outcome, time.Now())
			return nil
		},
	)
	// 重启恢复: 磁盘上未结算信号（崩溃遗留）重新注册结算轮询
	for _, sig := range recorder.PendingSignals() {
		resolutionPoller.Register(sig.ConditionID, sig.Slug)
		log.Printf("[Dog] 🔄 恢复未结算信号: %s slug=%s", sig.ConditionID, sig.Slug)
	}
	go resolutionPoller.Run(ctx)

	// ── 运行时状态（Dashboard 数据源）──
	runtime := &runtimeState{
		Executor:    executor,
		Recorder:    recorder,
		TwapAdapter: twapAdapter,
		Binance:     binance,
		Stake:       cfg.Stake,
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

	// σ 冷启动预热: 官方历史 |close−open| 范围回填（≤18 窗，逐窗间隔 1s ≈ 19s，
	// crypto-price 接口限速低）。预热完成后首窗即有 σ；全部缺失则回退冷启动
	// （前 3 个完成窗口前 no_hist，与回测口径一致）
	hist := newHistState()
	go func() {
		log.Printf("[Cycle] σ 预热: 拉取官方 TWAP 历史范围（≤%d 窗）...", histWindows)
		vals := feed.FetchTwapRanges(client, histWindows, windowSec, twapLookbackSeconds)
		hist.seed(vals)
		log.Printf("[Cycle] σ 预热完成: %d 窗可用", len(vals))
	}()

	log.Println("========================================")
	log.Printf(" Dog@0.2 触底策略 — 纸面交易（mode=%s）", *mode)
	log.Printf(" 输出: %s  |  Slug: %s", *outputDir, *slugPrefix)
	log.Printf(" 参数: ask≤%.2f 急跌m%d≥%.2f 浅洞(%.2f,%.2f)σ rem>%ds stake=%.0fUSDC",
		cfg.TriggerAskMax, cfg.CrashWindow, cfg.CrashMinAsk,
		cfg.DistLo, cfg.DistHi, cfg.RemMin, cfg.Stake)
	log.Println(" 数据源: [PM CLOB books 1s] + [Chainlink TWAP-60 锚/σ] + [Binance spot 浅洞]")
	log.Println("========================================")

	// ── 市场周期主循环 ──
	// nextCache 跨窗口缓存下一窗市场信息（稳态预取，见 collectLoop 内 rem≤20 逻辑）
	var nextCache *marketCache
	for {
		select {
		case <-ctx.Done():
			log.Println("[Dog] 正在关闭...")
			return
		default:
		}

		// 步骤 1: 对齐下一个 5 分钟窗口。
		// 已落后边界 ≤lateLimit 时直接进入本窗口（迟到订阅安全，稳态收尾
		// 普遍晚几百毫秒）；>lateLimit 才跳过（证据缺失太多防假判定）。
		now := time.Now()
		alignedTs := now.Unix() / windowSec * windowSec
		nextStart := time.Unix(alignedTs, 0)
		if elapsed := time.Since(nextStart); elapsed > lateLimit {
			log.Printf("[Cycle] ⚠️ 已落后窗口边界 %v（>%v），跳过本窗口 %s",
				elapsed.Round(time.Second), lateLimit,
				nextStart.UTC().Format(time.RFC3339))
			nextStart = nextStart.Add(windowSec * time.Second)
		}
		slug := fmt.Sprintf("%s-%d", *slugPrefix, nextStart.Unix())

		// 步骤 2: 市场信息（优先用本窗 tick 期间预取的缓存；未命中则按
		// 边界前 20s 预取 + 5s 重试，仅首窗/跳窗后走此路径）
		var cachedMarket *gjson.Result
		var cachedErr error
		if nextCache != nil && nextCache.slug == slug {
			cachedMarket = nextCache.res
			log.Printf("[Cycle] ✅ 使用预取缓存 %s", slug)
			nextCache = nil
		} else {
			nextCache = nil // 丢弃过期缓存（窗口被跳过或预取失败）
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
		upTokenID, downTokenID := parseMarketTokens(marketData)
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

		// 步骤 5: 注入窗口上下文（anchor/σ），启动 1s tick 采集
		// anchor = 边界瞬间的 TWAP-60 流值（官方开盘价 p50 偏差 0.08bps / p99
		// 1.14bps，口径文档已量化——记录在案，复验按分布对比不做逐笔对账）
		anchor, _ := twapAdapter.Latest()
		engine := flip.NewEngine(cfg)
		engine.BeginWindow(anchor, hist.bps(anchor))
		runtime.setWindow(engine, conditionID, slug, nextStart.Unix())
		endTime := nextStart.Add(windowSec * time.Second)

		log.Printf("[Cycle] event=%s 窗口开始 anchor=%.2f hist_bps=%.2f（%d 窗）",
			conditionID, anchor, hist.bps(anchor), hist.count())

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

				// 引擎驱动（首个触底 tick → 观测判定 → 执行/落盘/注册结算）
				if o := engine.ProcessTick(lastTick); o != nil {
					if rec := handleObservation(o, runtime, conditionID, slug, nextStart.Unix()); rec != nil && rec.OK {
						resolutionPoller.Register(conditionID, slug)
					}
				}
				if rem == 0 {
					ticker.Stop()
					break collectLoop
				}
				// 稳态预取: 本窗 rem≤20（即下一窗边界前 20s）预取下一窗市场信息，
				// rem%5==0 提供失败重试点（20/15/10/5 共 4 次）；loop 顶部
				// 按 slug 匹配复用，窗口被跳过时自动丢弃
				if nextCache == nil && rem <= 20 && rem%5 == 0 {
					nextSlug := fmt.Sprintf("%s-%d", *slugPrefix, nextStart.Add(windowSec*time.Second).Unix())
					res, err := client.FetchMarketBySlug(nextSlug)
					if err != nil {
						log.Printf("[Cycle] ⚠️ 下一窗预取失败 %s: %v（rem=%d 时重试）", nextSlug, err, rem)
					} else {
						nextCache = &marketCache{slug: nextSlug, res: res}
						log.Printf("[Cycle] ✅ 下一窗预取成功 %s（rem=%d）", nextSlug, rem)
					}
				}
				// 每 30s 打印一次窗口进度（rem 每秒递减，无重复）
				if rem%30 == 0 {
					log.Printf("[Event] %s rem=%ds up=%.3f/%.3f down=%.3f/%.3f spot=%.2f state=%s",
						conditionID, rem, lastTick.UpBid, lastTick.UpAsk,
						lastTick.DownBid, lastTick.DownAsk, lastTick.BinPrice, engine.State())
				}
			}
		}

		// 步骤 6: 窗口结束 → σ 滚动窗追加本窗振幅（严格只用已结束窗口）。
		// close 采自边界瞬间的 TWAP 流值（与官方收盘价口径差异已在文档量化）。
		// 无触底的窗口无记录（回测 extract 同款语义），本窗结算注册已在触发时完成。
		if anchor > 0 && lastTick.TwapPrice > 0 {
			hist.push(math.Abs(lastTick.TwapPrice - anchor))
			log.Printf("[Cycle] 窗口结束 %s: |close−anchor|=%.2f, σ 现 %d 窗",
				conditionID, math.Abs(lastTick.TwapPrice-anchor), hist.count())
		} else {
			log.Printf("[Cycle] ⚠️ 窗口结束 %s 但 close/anchor 缺失，本窗不计入 σ", conditionID)
		}
	}
}

// runtimeState 聚合主循环需要共享的组件引用（Dashboard 数据源）。
// mu 保护每窗口换装的字段（Engine/ConditionID/Slug/EventStart）:
// 主循环写（窗口起点），Dashboard goroutine 经 Snapshot 读。
type runtimeState struct {
	mu sync.RWMutex

	Engine      *flip.Engine
	Executor    flip.Executor
	Recorder    *flip.Recorder
	TwapAdapter *feed.TwapAdapter
	Binance     *feed.BinanceAdapter
	ConditionID string
	Slug        string
	EventStart  int64
	Stake       float64   // 每信号投入（构造后不变）
	Mode        string    // 成交模式: paper/live（构造后不变）
	StartedAt   time.Time // 进程启动时刻（构造后不变）
	books       func() (*sdk.OrderBook, *sdk.OrderBook)
	tokens      func() (string, string)
}

// Snapshot 实现 dashboard.Snapshotter（Dashboard 每 5s 轮询取快照）。
// Engine 指针在锁内读取；指针自身稳定（主循环只换不释放），
// Engine.State() 内部另有锁，读后调用安全。
func (rt *runtimeState) Snapshot() dashboard.LiveSnapshot {
	yb, nb := rt.books()
	pm := makePMTick(yb, nb)
	_, twAge := rt.TwapAdapter.Latest()
	bin := rt.Binance.LatestData()

	// spot 显示口径: 未推送显示 0/−1（前端判灰）；有推送则显示最近价与
	// 本地接收龄（前端按 >2s 标红——与引擎判 stale 的阈值一致）
	spotPrice, spotAgeMs := 0.0, int64(-1)
	if bin.RxAtMs > 0 {
		spotPrice = bin.Price
		spotAgeMs = time.Now().UnixMilli() - bin.RxAtMs
	}

	rt.mu.RLock()
	snap := dashboard.LiveSnapshot{
		Mode:        rt.Mode,
		StartedAt:   rt.StartedAt,
		ConditionID: rt.ConditionID,
		Slug:        rt.Slug,
		EventStart:  rt.EventStart,
		EngineState: rt.Engine.State().String(),
		UpBid:       pm.UpBid,
		UpAsk:       pm.UpAsk,
		DownBid:     pm.DownBid,
		DownAsk:     pm.DownAsk,
		BookLatMs:   pm.BookLatMs,
		TwapAgeMs:   twAge,
		SpotPrice:   spotPrice,
		SpotAgeMs:   spotAgeMs,
	}
	rt.mu.RUnlock()
	return snap
}

// setWindow 在窗口起点换装引擎与元字段（主循环持有）。
func (rt *runtimeState) setWindow(engine *flip.Engine, conditionID, slug string, eventStart int64) {
	rt.mu.Lock()
	rt.Engine = engine
	rt.ConditionID = conditionID
	rt.Slug = slug
	rt.EventStart = eventStart
	rt.mu.Unlock()
}

// sampleTick 读取当前盘口/现货/TWAP 构造一条引擎 tick（1s 粒度）。
// 盘口缺失时 bid/ask 为 0（引擎判无效 tick 不检）；spot 新鲜度超阈值置 0
// （= missing_spot）；TWAP 现值随身携带（dist_t 观察腿 + 窗口 close 采样）。
func sampleTick(t time.Time, rem int, rt *runtimeState) flip.Tick {
	yb, nb := rt.books()
	pm := makePMTick(yb, nb)

	// Binance spot（浅洞腿输入）: 本地接收新鲜度 ≤spotFreshMs 才有效——
	// 断流后保留的最后价必须判 stale（交易所时间戳不可作新鲜度判据）
	bin := rt.Binance.LatestData()
	spot := 0.0
	if bin.RxAtMs > 0 && t.UnixMilli()-bin.RxAtMs <= spotFreshMs {
		spot = bin.Price
	}

	twapPrice, twAge := rt.TwapAdapter.Latest()
	return flip.Tick{
		Ts:        t.UnixMilli(),
		Rem:       rem,
		UpBid:     pm.UpBid,
		UpAsk:     pm.UpAsk,
		DownBid:   pm.DownBid,
		DownAsk:   pm.DownAsk,
		BookLatMs: pm.BookLatMs,
		BinPrice:  spot,
		TwapPrice: twapPrice,
		TwapAgeMs: twAge,
	}
}

// handleObservation 处理引擎产出的触底观测（成功与失败都落盘，频率校准用）:
// ok 信号先纸面执行（PaperExecutor 校验 fill>0），随后 RecordObservation 立即
// 落盘（行级 flush，崩溃不丢）。返回落盘记录（落盘失败返回 nil）——ok 信号的
// 结算轮询注册由调用方按 rec.OK 决定。
func handleObservation(o *flip.Observation, rt *runtimeState, conditionID, slug string, eventStart int64) *flip.Record {
	if o.OK {
		// 信号 → 纸面执行（PaperExecutor 恒 filled；live 模式后续接入）
		if err := rt.Executor.Execute(o); err != nil {
			log.Printf("[Trading] ⚠️ 信号未执行: %v（仍记录观测）", err)
		}
		log.Printf("[Event] 🎯 触底信号 side=%s rem=%ds fill=%.3f m20=%.2f m30=%.2f m45=%.2f dist_s=%.2f dist_t=%.2f shares=%.1f",
			o.Side, o.Rem, o.Fill, o.M20, o.M30, o.M45, o.DistS, o.DistT, o.Shares)
	} else {
		log.Printf("[Event] 触底否决 side=%s rem=%ds fill=%.3f m45=%.2f dist_s=%.2f reason=%s",
			o.Side, o.Rem, o.Fill, o.M45, o.DistS, o.RejectReason)
	}
	rec, err := rt.Recorder.RecordObservation(conditionID, slug, eventStart, o, rt.Stake)
	if err != nil {
		log.Printf("[Dog] 观测落盘失败: %v", err)
		return nil
	}
	return rec
}

// defaultSDKConfig 构造 SDK 配置（环境变量覆盖）。
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
