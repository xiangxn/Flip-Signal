// Command flip 是「狗@0.2」策略引擎入口（口径文档 docs/engine_plan_dog020_2026-09-02.md）。
//
// 连接 Polymarket CLOB WebSocket（UP/DOWN 订单簿）、Chainlink TWAP-60（锚/σ）与
// Binance BTCUSDT spot（浅洞腿），每秒驱动 flip.Engine 状态机检测「触底观测」——
// 某侧 ask 首次砸到 ≤0.20 的下狗机会：急跌(m_45) × 浅洞(dist_s) × 时间(rem) 三腿
// 全过即 ok 信号。成交按 -mode 分流（默认 paper 模拟全额成交; live = 真实 CLOB
// GTC 限价挂单 @ 触发 ask——即时能吃的吃掉、余量留在簿上等对手方, 到 rem ≤
// 策略时间腿（flip.rem_min = 180s）由我们自己撤掉未成交余量。两阶段落盘 +
// 风控闸, 编排见 internal/flip ExecState; 挂单终态由 trading.FillTracker 撤销时
// 查询 size_matched 定稿）, 官方
// 结算后回填完整 P&L 到 JSONL（touches_YYYY-MM-DD.jsonl，按日切分）。成功与失败的
// 观测都落盘（频率校准用）。
//
// 结算轮询只注册确定持仓（paper 恒成交; live filled/partial——GTC 的 resting 行
// 要等 FillTracker 定稿后才注册）；
// 崩溃后重启按磁盘 pending 恢复注册, submitting/resting/未知结果行打 ⚠️ 人工核对
// （resting 会被 FillTracker 接管自动定稿）。
//
// 配置见包 internal/config 与根目录 v4.config.yaml（全量默认值示例）：
// 优先级 = CLI flag > 配置文件 > 代码默认值，**不读环境变量**（唯一例外是解密密文
// 凭证用的 PM_CONFIG_DECRYPT_PASSWORD）。本文件只保留 4 个 flag。
//
// 用法：
//
//	go run ./cmd/flip                                       # 代码默认值（不开 Dashboard）
//	go run ./cmd/flip -config v4.config.yaml -dashboard :8090
//	go run ./cmd/flip -config v4.config.yaml -mode live     # live 需配置文件里有密文凭证
//	go run ./cmd/flip -stake 5                              # 单点覆盖（最高优先级）
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
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/tidwall/gjson"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"

	"github.com/necklace/flip-signal/internal/config"
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

// twapMaxStale 是 TWAP 推送新鲜度阈值: 超过该时长未收到推送则重建订阅
// （2026-09-01 服务器实测断流事件，TwapAdapter 内建看门狗，见其注释）。
const twapMaxStale = 2 * time.Minute

// 数据源新鲜度阈值（spot/twap）与日亏熔断线的默认值已于 2026-09-16 迁入
// internal/config（`feed.*` / `risk.max_daily_loss`）——本文件不再持有默认值，
// 原「为什么是 2s / 10s」的论证见 internal/config/config.go defaults() 注释，
// 出处 docs/dog020_risk_latency_plan_2026-09-16.md §2.2。

// twapLookbackSeconds 是结算口径 TWAP 回看窗口秒数（Chainlink TWAP-60）。
const twapLookbackSeconds = 60

// 精确取锚参数（2026-09-19, docs/dog020_anchor_exact_open_2026-09-19.md）: 锚 =
// **边界那一秒**的 Chainlink TWAP-60 评估值——实测它与官方 openPrice 收敛值逐位相同
// （8/8 窗, 差 ≤0.0006bps ≈ 3 厘美元）, 故它既是最准的口径, 又不必等官方。
// 该条推送实测 p50 +2.0s / p90 +2.3s 到达（观测到最晚 +12.1s）, 20s 预算（40 × 500ms）
// 即 p50 的 10 倍余量; 命中即终局。预算耗尽 = 本窗无锚（引擎 anchor ≤0 只占槽不判定、
// 不产出观测）, 且**不设过渡锚**——宁可丢窗也不拿近似锚判定。
// 官方 HTTP 路径**已休眠**（+40s 才收敛, 本窗机会早过）: 工具代码保留在 feed 包,
// 将来若允许 40s+ 延迟, 接上 feed.NewOpenPriceFetcher 并给 opts 填 Schedule 即可。
// 与 prefetchLead/lateLimit 同为 main 常量，暂不配置化。
const (
	anchorExactAttempts = 40                     // 精确取锚尝试次数（× 间隔 = 20s 预算）
	anchorExactInterval = 500 * time.Millisecond // 精确取锚尝试间隔
)

// anchorInfo 是本窗锚的可见性字段（winstats 落盘用）。
// 只由取锚 goroutine 写、collectLoop 之后（cancel + join 之后）读——channel close
// 建立的 happens-before 保证无数据竞争。
type anchorInfo struct {
	src  string // feed.AnchorSourceOfficial | feed.AnchorSourceStream（空 = 本窗未取到锚）
	atMs int64  // 锚值可用时刻（unix 毫秒; 仅取到锚时非 0; stream = 推送本地到达时刻）
}

// localFreshMax 是 σ 本地预热的新鲜度上限: 最新已落盘窗口结束距今 ≤ 该值才可信
// （= 引擎最近在跑，「马上重启」场景本地覆盖完整）；停机更久则本地缺停机期的
// 窗口，回退官方网络预热（FetchTwapRanges 能取停机期间的窗口）。
// 容差取 σ 冷启动门槛本身（σ 容量/下限常量 flip.HistWindows/HistMin 随滚动窗
// 实现在 internal/flip sigma.go——与回测 hist 窗口数一致）。
const localFreshMax = time.Duration(flip.HistMin*windowSec) * time.Second

// marketCache 缓存下一窗口的市场信息（稳态预取: 本窗 tick 尾部预取，loop 顶部复用）。
type marketCache struct {
	slug string
	res  *gjson.Result
}

func init() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
}

func main() {
	// ── CLI 参数（仅 4 个，其余一律走配置文件; 优先级最高）──
	configPath := flag.String("config", "", "配置文件路径（YAML; 空 = 只用代码默认值）")
	dashboardAddr := flag.String("dashboard", "", "Dashboard 监听地址（覆盖 runtime.dashboard_addr）")
	mode := flag.String("mode", "", "成交模式: paper|live（覆盖 runtime.mode）")
	stake := flag.Float64("stake", 0, "每信号投入 USDC（覆盖 flip.stake）")
	flag.Parse()

	// set 记录「哪些 flag 被显式给出」。用 flag.Visit 而非比零值: 这样 -dashboard ""
	// 能真的关掉配置文件里的 :8090，-stake 0 也会走校验报错（响亮）而不是静默回退。
	set := map[string]bool{}
	flag.Visit(func(f *flag.Flag) { set[f.Name] = true })

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// ── 配置加载（CLI flag > 配置文件 > 代码默认值; 不读环境变量）──
	cfg, err := config.Load(*configPath)
	if err != nil {
		log.Fatalf("[Config] %v", err)
	}
	if set["dashboard"] {
		cfg.Runtime.DashboardAddr = *dashboardAddr
	}
	if set["mode"] {
		cfg.Runtime.Mode = *mode
	}
	if set["stake"] {
		cfg.Flip.Stake = *stake
	}
	// 校验判的是最终生效值，必须在覆盖之后
	warns, verr := config.Validate(cfg)
	if verr != nil {
		log.Fatalf("[Config] 配置校验失败: %v", verr)
	}
	for _, w := range warns {
		log.Printf("⚠️  [Config] %s", w)
	}

	// ── Polymarket 客户端（配置文件未写 sdk.polymarket.owner_key 则自动生成
	// 临时密钥，只读运行）──
	cfgSDK := cfg.SDK
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
		log.Println("[Dog] ⚠️  配置文件未提供 sdk.polymarket.owner_key —— 只读运行（纸面交易）")
	}

	// ── 成交执行器 + live 分流（一个函数: 默认纸面; -mode live 且凭证齐 → 真单）──
	effMode, executor := resolveLiveMode(cfg.Runtime.Mode, cfgSDK, readOnly, client)

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
	binance := feed.NewBinanceAdapterWithConfig(cfg.Binance)
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
	recorder, err := flip.NewRecorder(cfg.Runtime.OutputDir)
	if err != nil {
		log.Fatalf("[Dog] 记录器创建失败: %v", err)
	}
	defer recorder.Close()

	// ── 🔴 未决 GTC 挂单 × 非 live 模式 = 拒绝启动 ──
	// resting 行只可能来自 live（paper 即时定稿, 永不落 resting）——那笔真单还在
	// CLOB 簿上等对手方。paper 模式没有凭证撤单、也不做终态跟踪: 重启后它会一直吃到
	// 闭市（rem ≤ 策略时间腿之后的成交正是决策 #16 要截掉的逆向选择样本）, 而且永远
	// 无人结算——行悬在磁盘上, 日亏熔断与 P&L 都看不见它。故宁可拒绝启动（与配置
	// 拼错即失败同一原则: 静默带病运行比崩溃危险）, 逼运维带凭证以 live 重启接管
	// （接管后首轮查询就能读到终态、把未成交余量撤掉）, 或按 order_id 人工结清该行。
	if effMode != "live" {
		if pend := pendingResting(recorder.Observations()); len(pend) > 0 {
			for _, rec := range pend {
				log.Printf("[Dog] 🔴 未决挂单遗留: order=%q status=%s slug=%s event_start=%d note=%q",
					rec.OrderID, rec.ExecStatus, rec.Slug, rec.EventStart, rec.ExecNote)
			}
			log.Fatalf("[Dog] 🔴 检测到 %d 条未决 GTC 挂单（磁盘 resting 行）, 而本次是 %s 模式——该模式既不能撤单也不能定稿。请带凭证以 -mode live 重启接管（首轮查询即定稿并撤余量）; 若该单早已了结, 按 order_id 去 data-api/UI 核对后人工结清该行再跑 paper",
				len(pend), effMode)
		}
	}

	resolutionPoller := trading.NewResolutionPoller(
		client.FetchMarketBySlug,
		10*time.Second,
		func(conditionID string, outcome int) error {
			// Resolve 返回 false = recorder.pending 中无此市场（从未注册/早已结算
			// 摘除——重复回调、或轮询表与记录器失步）。poller 先回调后移除，正常
			// 每市场仅命中一次；未命中即结算从未回填（won/pnl 永远悬空，静默），
			// 记日志兜底排查。磁盘重写失败不影响此布尔（recorder 内部已记 ⚠️）。
			if !recorder.Resolve(conditionID, outcome, time.Now()) {
				log.Printf("[Dog] ⚠️ 结算回填未命中 %s outcome=%d（pending 中无此市场）",
					conditionID, outcome)
			}
			return nil
		},
	)
	// 重启恢复: 磁盘上未结算信号（崩溃遗留）重新注册结算轮询
	for _, sig := range recorder.PendingSignals() {
		resolutionPoller.Register(sig.ConditionID, sig.Slug)
		log.Printf("[Dog] 🔄 恢复未结算信号: %s slug=%s", sig.ConditionID, sig.Slug)
	}
	go resolutionPoller.Run(ctx)

	// ── live 启动告警（载入期逐条 ⚠️ 已打, 这里给总量与目录提示; paper 无此语义）──
	if effMode == "live" {
		warnLiveStartup(recorder)
	}

	// ── 运行时状态（Dashboard 数据源 + 信号执行编排）──
	// runtimeState 只背窗口/数据源快照; 执行编排（executor/闸/两阶段/风控）收敛在
	// flip.ExecState——paper/live 同一形态, 差异仅在 Ex 实现是否真实 POST
	// （编排实现 internal/flip exec_state.go; live 时 Ex 为本函数刚构造的
	// trading.LiveExecutor）。
	runtime := &runtimeState{
		TwapAdapter: twapAdapter,
		Binance:     binance,
		Mode:        effMode,
		StartedAt:   time.Now(),
		Exec: &flip.ExecState{
			Rec:          recorder,
			Ex:           executor,
			Live:         effMode == "live",
			Stake:        cfg.Flip.Stake,
			MaxDailyLoss: cfg.Risk.MaxDailyLoss,
			FirstWindow:  effMode == "live", // live 首窗禁单（重启防双单缝隙）
			Tokens: func() (string, string) {
				tokMu.RLock()
				defer tokMu.RUnlock()
				return upTok, downTok
			},
		},
	}
	runtime.books = func() (*sdk.OrderBook, *sdk.OrderBook) {
		bookMu.RLock()
		defer bookMu.RUnlock()
		return upBook, downBook
	}

	// ── GTC 挂单跟踪（live 专用语义; paper 无挂单, 恒空转）──
	// GTC 下单的成交量在 POST 之后仍可能增长（挂单在簿等对手方）,
	// 故 resting 行的定稿走这里: 到 rem ≤ 策略时间腿（flip.rem_min）撤掉未成交
	// 余量 → 查 CLOB size_matched → ApplyFillFinal 落盘 → 再注册结算轮询
	// （顺序不可反: 结算按最终 shares/cost 记 P&L）。
	// 撤单提前量取自配置（不是常量）: 它就是回测的时间腿判据 rem > RemMin——
	// rem ≤ 它之后的成交不属于这条策略（触发瞬间必成交是回测前提）。
	fillTracker := trading.NewFillTracker(&trading.SdkClient{Client: client},
		time.Duration(cfg.Flip.RemMin)*time.Second, func(f flip.FillFinal) {
			rec := runtime.Exec.ApplyFillFinal(f)
			if rec != nil && rec.IsFilled() {
				resolutionPoller.Register(rec.ConditionID, rec.Slug)
			}
		})
	go fillTracker.Run(ctx)
	// 重启接管: 进程死在挂单期间 → 磁盘上的 resting 行交回跟踪（撤单点早已过则
	// 立即尝试撤单并定稿; 查不到的保持 resting + 人工核对标记, 绝不按 0 成交记）
	if effMode == "live" {
		for _, rec := range recorder.Observations() {
			if rec.ExecStatus == flip.ExecStatusResting {
				fillTracker.Register(rec, time.Unix(rec.EventStart, 0).Add(windowSec*time.Second), true)
			}
		}
	}

	// ── Dashboard（手机浏览器兼容的单页前端）──
	if cfg.Runtime.DashboardAddr != "" {
		// 三源新鲜度阈值下发（前端按阈值标红——book/twap 无颜色语义的缺口补上）
		limits := dashboard.SourceLimits{
			BookLatMs: cfg.Flip.MaxBookLatMs,
			SpotAgeMs: cfg.Feed.MaxSpotAgeMs,
			TwapAgeMs: cfg.Feed.MaxTwapAgeMs,
		}
		dashState := dashboard.NewState(recorder, runtime, cfg.Flip, effMode, limits)
		go dashState.ListenAndServe(cfg.Runtime.DashboardAddr)
	}

	// σ 启动预热（本地 windows_*.jsonl 优先, 不足/陈旧回退官方网络预热——见 warmupSigma）
	hist := flip.NewHistState()
	warmupSigma(hist, recorder, client)

	log.Println("========================================")
	if effMode == "live" {
		log.Printf(" Dog@0.2 触底策略 — 🔒 实盘交易（GTC 限价挂单 @ 触发 ask, rem≤%ds 撤未成交余量, 日亏熔断 ≤%.1fU）",
			cfg.Flip.RemMin, cfg.Risk.MaxDailyLoss)
	} else {
		// 纸面同样打印熔断线: 闸判据两模式同源（方案 A）——纸面被闸行照记照结算,
		// 只多 gate_reason 字段（分析脚本需过滤, 见 plan §3.5/§3.7）
		log.Printf(" Dog@0.2 触底策略 — 纸面交易（mode=%s, 日亏熔断 ≤%.1fU 影子: 只标记不拦单）",
			effMode, cfg.Risk.MaxDailyLoss)
	}
	log.Printf(" 输出: %s  |  Slug: %s", cfg.Runtime.OutputDir, cfg.Runtime.SlugPrefix)
	log.Printf(" 参数: ask≤%.2f 急跌m%d≥%.2f 浅洞 yes(%.2f,%.2f)/no(%.2f,%.2f)σ rem>%ds stake=%.0fUSDC",
		cfg.Flip.TriggerAskMax, cfg.Flip.CrashWindow, cfg.Flip.CrashMinAsk,
		cfg.Flip.DistLoYes, cfg.Flip.DistHi, cfg.Flip.DistLoNo, cfg.Flip.DistHi, cfg.Flip.RemMin, cfg.Flip.Stake)
	log.Printf(" 新鲜度闸: book_lat≤%dms + spot_age≤%dms + twap_age≤%dms（生效值，见 docs/dog020_risk_latency_plan_2026-09-16.md）",
		cfg.Flip.MaxBookLatMs, cfg.Feed.MaxSpotAgeMs, cfg.Feed.MaxTwapAgeMs)
	log.Println(" 数据源: [PM CLOB books 1s] + [Chainlink TWAP-60 锚/σ] + [Binance spot 浅洞]")
	log.Println("========================================")

	// logWinstats 落盘一行本窗 tick 健康度（winstats_YYYY-MM-DD.jsonl）。
	// 每窗无条件一行——含被跳过的窗口（skip 非空）：逐日行数（≈288）本身即「主循环
	// 跑满」的证据；而 LostTriggers 明细是「延迟到底吃掉了多少信号」的唯一可见性
	// 来源（此前无效 tick 走 pushSlots 直接 return，磁盘上零痕迹——
	// docs/dog020_risk_latency_plan_2026-09-16.md §1.3）。失败只告警，不影响主循环。
	logWinstats := func(condID, slug string, eventStart int64, anchor, hb float64,
		st flip.WindowStats, skip string, ar anchorInfo) {
		e := flip.WindowStatsEntry{
			Ts: time.Now().UnixMilli(), ConditionID: condID, Slug: slug,
			EventStart: eventStart, Skip: skip, Anchor: anchor, HistBps: hb,
			WindowStats: st, AnchorExact: ar.src != "", AnchorSrc: ar.src,
		}
		if ar.atMs > 0 {
			e.AnchorRecoveredMs = ar.atMs - eventStart*1000 // 锚可用时刻距边界（仅取到锚时）
		}
		if err := recorder.LogWindowStats(e); err != nil {
			log.Printf("[Cycle] ⚠️ 窗口健康度落盘失败: %v", err)
		}
	}

	// ── 市场周期主循环 ──
	// nextCache 跨窗口缓存下一窗市场信息（稳态预取，见 collectLoop 内 rem≤20 逻辑）
	var nextCache *marketCache
	// prevWindowStart 是上一轮迭代处理的窗口起点（unix 秒; 0 = 本进程还没处理过
	// 任何窗口）。用途: 区分「刚跑完/刚跳过的窗口」与真·迟到——collectLoop 收尾是
	// `if rem == 0 { break }` 而 rem 由 int() 截断取整，循环总在边界前 0~1s 回来，
	// 此刻 floor(now/300) 仍指回刚结束的那一窗（见 docs/dog020_anchor_recovery_
	// 2026-09-16.md §9.1）。按身份判定而非时间启发式: 那几毫秒内没有任何真实状态
	// 变化, 唯一区别是「这窗我已经处理过了」。
	var prevWindowStart int64
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
			// 例外: floor 指回的正是上一轮刚处理过的那一窗（收尾提前于边界几毫秒
			// 回来, 见 prevWindowStart 注释）——不是迟到, 静默顺延到下一窗: 不告警
			// 也不落 skip=late 空格行（否则 winstats 同 event_start 两行, 逐日行数
			// ≈288「主循环跑满」的自查口径失效）。
			if nextStart.Unix() != prevWindowStart {
				log.Printf("[Cycle] ⚠️ 已落后窗口边界 %v（>%v），跳过本窗口 %s",
					elapsed.Round(time.Second), lateLimit,
					nextStart.UTC().Format(time.RFC3339))
				runtime.clearWindow() // 本窗被跳过: 快照不留上一窗陈旧状态（跳到下一窗, 等待最长 ~5min）
				logWinstats("", "", nextStart.Unix(), 0, 0, flip.WindowStats{}, "late", anchorInfo{})
			}
			nextStart = nextStart.Add(windowSec * time.Second)
		}
		// 本轮迭代处理的窗口（含上面的顺延）: 后面每条出口——跳过通道 continue
		// （no_market/no_token/dup_record/no_sigma）与跑满窗口的末路径——处理的都是
		// 这一窗, 故此处一次赋值即覆盖全部出口（重启后 prevWindowStart=0 不复用）。
		prevWindowStart = nextStart.Unix()
		slug := fmt.Sprintf("%s-%d", cfg.Runtime.SlugPrefix, nextStart.Unix())

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
				runtime.clearWindow() // 本窗不跑（整窗等待）, 快照不留上一窗陈旧状态
				logWinstats("", slug, nextStart.Unix(), 0, 0, flip.WindowStats{}, "no_market", anchorInfo{})
				select {
				case <-ctx.Done():
					return
				case <-time.After(time.Until(nextStart.Add(windowSec * time.Second))):
				}
				continue
			}
		}
		conditionID := marketData.Get("conditionId").String()
		upTokenID, downTokenID := feed.ParseMarketTokens(marketData)
		if upTokenID == "" || downTokenID == "" {
			log.Printf("[Cycle] ⚠️ 市场 %s token 解析为空，跳过本窗口", slug)
			runtime.clearWindow() // 本窗不跑（整窗等待）, 快照不留上一窗陈旧状态
			logWinstats(conditionID, slug, nextStart.Unix(), 0, 0, flip.WindowStats{}, "no_token", anchorInfo{})
			select {
			case <-ctx.Done():
				return
			case <-time.After(time.Until(nextStart.Add(windowSec * time.Second))):
			}
			continue
		}
		// 快速重启防重入（2026-09-09 review）: 崩溃后 ≤15s 内重启会按「迟到准入」
		// 重入上一进程未跑完的同一窗口——若崩溃前该窗已触发落盘, 重跑会产出同
		// conditionID 双记录（Recorder.pending 以 conditionID 为键, 后记覆盖先记
		// → 先记的一笔永不结算回填）且可能同窗二次触发。已有记录即整窗跳过
		// （每窗至多一次首触观测; σ 缺此窗由 recentBlock 600s 缺口容差吸收,
		// 与 >15s 迟跳同效; 未触发即崩溃的窗口无记录, 仍按原准入续跑）。
		if recorder.HasRecord(conditionID) {
			log.Printf("[Cycle] ⚠️ 窗口 %s 已有落盘记录（%s 残留，快速重启重入），跳过整窗防双记", slug, conditionID)
			runtime.clearWindow() // 本窗不跑（整窗等待）, 快照不留上一窗陈旧状态
			logWinstats(conditionID, slug, nextStart.Unix(), 0, 0, flip.WindowStats{}, "dup_record", anchorInfo{})
			select {
			case <-ctx.Done():
				return
			case <-time.After(time.Until(nextStart.Add(windowSec * time.Second))):
			}
			continue
		}
		// σ 未就绪整窗跳过（2026-09-16）: 冷启动时本地 windows_* 不可用（断档/
		// 陈旧）会回退官方网络预热——warmupSigma 的预热是异步 goroutine, 429
		// 退避下实测 >70s, 边界落在它完成之前时 hist.Bps 恒 0。此时本窗任何触底
		// 都被 no_hist 拒（首触即 Done → 必然零信号）, 却仍落一条 hist_bps=0 观测,
		// 踩中 06_oos_review.py 的关键字段 0 异常硬检查。与锚缺失同一条原则
		// （数据源不可信 → 本窗不观测, 决策 #12）, 故整窗跳过并留 skip=no_sigma;
		// 下一窗在 5min 后, 预热早已完成, 不会连跳。无 close 采样 → windows_*
		// 缺一行, 等价于停机窗（RecentBlock 600s 缺口容差吸收, 同 dup_record）。
		// 位置在订阅/预取之前: 本段到步骤 5 之间只有同步调用, σ 取值不变。
		if hist.Count() < flip.HistMin {
			log.Printf("[Cycle] ⚠️ 窗口 %s σ 未就绪（%d < %d 窗，预热中），跳过本窗口",
				slug, hist.Count(), flip.HistMin)
			runtime.clearWindow() // 本窗不跑（整窗等待）, 快照不留上一窗陈旧状态
			logWinstats(conditionID, slug, nextStart.Unix(), 0, 0, flip.WindowStats{}, "no_sigma", anchorInfo{})
			select {
			case <-ctx.Done():
				return
			case <-time.After(time.Until(nextStart.Add(windowSec * time.Second))):
			}
			continue
		}
		// live 每窗预热 tick size/negRisk/feeRate: SDK CreateOrder 恒走
		// ResolveTickSize + GetNegRisk 网调——不预热则信号路径多 1-2 次串行网调;
		// 用本窗 gamma 数据预热, 与下单同源、信号路径零额外网调（见 prefetch.go）。
		if effMode == "live" {
			trading.PrefetchTokenInfo(client, marketData, []string{upTokenID, downTokenID})
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

		// 步骤 5: 启动 1s tick 采集（σ 就绪由前置闸保证, 锚由取锚通道回填——两条闸见上）
		// **不设过渡锚**: 窗口开始时不取 Latest()——它是「边界**前**一秒」的到达口径
		// 近似（服务器发布延迟 p50 ≈ 2.0s）, 与锚的真实口径（边界那一秒的评估值 =
		// 官方 openPrice）差 p90 0.24bps。锚一律留 0, 由取锚通道精确命中后经
		// UpgradeAnchor 注入（锚与 σ 同源同换）。锚未到手期间引擎照常收 tick 占槽、
		// 只闸住触发判定（决策 #12/#14 原则）, 20s 预算耗尽则本窗不产出观测——
		// 宁可丢窗, 也不拿近似锚判定出信号。
		engine := flip.NewEngine(cfg.Flip)
		engine.BeginWindow(0, 0)
		runtime.setWindow(engine, conditionID, slug, nextStart.Unix())
		endTime := nextStart.Add(windowSec * time.Second)

		// 精确取锚通道（2026-09-19, docs/dog020_anchor_exact_open_2026-09-19.md）: 每窗
		// 都起一条, 按 500ms × 40（= 20s）反复从推送缓存里取**评估时刻 == 边界**的那条
		// （实测 p50 +2.0s 到达）, 命中即终局。取锚只改锚与 σ、不追溯已判定的 tick
		// （引擎 UpgradeAnchor 在产出观测后冻结）; 命中前引擎 anchor ≤0 天然不产出观测
		// （观测行 anchor 恒 >0, 06 复验硬检查），无需额外闸。
		// 官方 HTTP 路径已休眠（需求: +40s 才收敛, 本窗机会早过）, fetch 传 nil;
		// 将来恢复只需接回 feed.NewOpenPriceFetcher 并给 opts 填 Schedule/SettleAfter。
		// 窗口级 ctx: 窗口结束即取消并 join（见 collectLoop 之后的收尾段）。
		var (
			rec        anchorInfo
			cancelAnch context.CancelFunc
			anchorDone chan struct{}
		)

		winCtx, cancel := context.WithCancel(ctx)
		cancelAnch = cancel
		anchorDone = make(chan struct{})
		go func() {
			defer close(anchorDone)
			ups := feed.RecoverAnchor(winCtx, nil, twapAdapter.PushNearest, nextStart, endTime,
				feed.AnchorUpgradeOpts{
					Attempts: anchorExactAttempts,
					Interval: anchorExactInterval,
				})
			for r := range ups {
				bps := hist.Bps(r.Price)
				if !engine.UpgradeAnchor(r.Price, bps) {
					continue // 本窗已产出观测 → 锚已冻结
				}
				rec.src, rec.atMs = r.Source, r.AtMs
				log.Printf("[Anchor] ✅ 窗口 %s 锚 %.2f（%s, σ=%.2fbps; 边界后 +%dms）",
					conditionID, r.Price, r.Source, bps, r.AtMs-nextStart.UnixMilli())
			}
			// 预算耗尽（通道已关且本窗始终无锚）: 打一行诊断——缓存快照直接回答是
			// 服务器没发、我们收晚了、还是时间戳缺失。本窗不产出观测（主循环照常跑完）。
			if rec.src == "" {
				n, newestOff, noTs := twapAdapter.CacheStat()
				log.Printf("[Anchor] ⚠️ 窗口 %s %d 次 × %v 未取到边界那一秒的 open"+
					"（缓存 %d 条, 最新一条评估偏移 %+dms, 缺时间戳 %d 条）, 本窗不产出观测",
					conditionID, anchorExactAttempts, anchorExactInterval, n, newestOff, noTs)
			}
		}()

		log.Printf("[Cycle] event=%s 窗口开始（锚待精确命中, 不设过渡锚; σ %d 窗就绪）",
			conditionID, hist.Count())

		ticker := time.NewTicker(time.Second)
		lastTick := flip.Tick{}
		// 上一条 tick 的**采样真实时刻**（wall clock）。必须与 lastTick.Ts 分开:
		// Ts 是 ticker 的**计划**时刻（Go 的 sendTime 发的是 Now().Add(-delta)）,
		// 循环被卡住时它会明显落后于真实时刻, 而窗末 close 的迟到判据恰恰要靠
		// 真实时刻（见窗口结束后的 σ 段）。
		var lastSampleAt time.Time

	collectLoop:
		for {
			select {
			case <-ctx.Done():
				ticker.Stop()
				return
			case tickTime := <-ticker.C:
				rem := int(endTime.Sub(tickTime).Seconds())
				rem = max(rem, 0)
				// 采样真实时刻（tickTime 是计划时刻, 卡顿时二者可差数秒——σ 迟到判据用这个）
				lastSampleAt = time.Now()
				lastTick = sampleTick(tickTime, rem, runtime, cfg.Feed.MaxSpotAgeMs)

				// 引擎驱动（首个触底 tick → 观测判定 → 执行/落盘/注册结算）
				// 结算只注册确定持仓（paper 行 ExecStatus 空恒成交; live filled/partial;
				// unfilled/rejected/风控停单不注册）。GTC 的 resting 行仓位未定
				// （挂单在簿）——交 FillTracker 定稿, 由它的回调注册结算。
				if o := engine.ProcessTick(lastTick); o != nil {
					if rec := runtime.Exec.HandleObservation(o, conditionID, slug, nextStart.Unix()); rec != nil && rec.OK {
						switch {
						case rec.ExecStatus == flip.ExecStatusResting:
							fillTracker.Register(rec, endTime, false)
						case rec.IsFilled():
							resolutionPoller.Register(conditionID, slug)
						}
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
					nextSlug := fmt.Sprintf("%s-%d", cfg.Runtime.SlugPrefix, nextStart.Add(windowSec*time.Second).Unix())
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

		// 取锚通道收尾: 取消 + join（通道通常早在 +2s 就已命中并关闭, join 立即返回）。
		// channel close 建立 happens-before——此后读 rec / 引擎锚无数据竞争。
		cancelAnch()
		<-anchorDone
		// 本窗最终锚/σ: 精确命中过 = 边界那一秒的评估值; 始终未取到 = (0, 0)
		// → 本窗不产出观测, 也不计入 σ。
		anchor, histBps := engine.WindowAnchor()

		// 步骤 6: 窗口结束 → σ 滚动窗追加本窗振幅（严格只用已结束窗口）。
		// close 采自边界瞬间的 TWAP 流值（与官方收盘价口径差异已在文档量化）。
		// 无触底的窗口无记录（回测 extract 同款语义），本窗结算注册已在触发时完成。
		// 新鲜度守卫: 断流期陈旧流值会把本窗振幅放大成假 σ 污染其后 18 窗，
		// 缺一窗可接受（阈值依据见 feed.max_twap_age_ms 的注释）。
		// 精确取锚命中时 anchor > 0 → 本窗照常计入 σ（锚就是边界那一秒的评估值,
		// 最贴回测的 twap_open 口径）; 始终未命中才落 anchor≤0 分支（本窗判定的 tick
		// 全部被闸, 也不会产出观测, σ 少一窗由 RecentBlock 的缺口容差吸收）。
		switch {
		case anchor <= 0 || lastTick.TwapPrice <= 0:
			log.Printf("[Cycle] ⚠️ 窗口结束 %s 但 close/anchor 缺失，本窗不计入 σ", conditionID)
		case lastTick.TwapAgeMs > cfg.Feed.MaxTwapAgeMs:
			log.Printf("[Cycle] ⚠️ 窗口结束 %s TWAP 陈旧（龄 %dms），本窗不计入 σ",
				conditionID, lastTick.TwapAgeMs)
		case lastSampleAt.Sub(endTime) > time.Second:
			// 采样迟到（2026-09-19 review）: tick 循环被卡住跨过边界（预取 HTTP、GC、
			// 调度），恢复后第一条 tick 的**计划时刻**仍落在边界之前——于是 rem 算出 0、
			// 本窗就此结束，但那条 tick 的数据是**此刻**读的, close 实际采到了**下一窗**
			// 的 TWAP。TwapAgeMs 判据拦不住（推送每秒一条, 龄恒 ~1s）; 判据只能用采样
			// 真实时刻（lastSampleAt, 不能用 lastTick.Ts——它正是被卡顿骗过的那个值）。
			// 容差 1s 而非 0: 正常结束 tick 落在边界前 0~1s（ticker 网格相位）, 且迟到
			// <1s 时 close 多数仍是同一条已到达的推送（推送每秒一条、到达延迟 ~2s,
			// 口径变化在既有 ~1.5s open/close 不对称之内）。丢窗代价不对称——σ 长期变薄
			// 会滑向 no_sigma 整窗跳过（零信号）, 比一窗 close 偏 1s 严重得多。
			// 本窗不计入 σ: 假振幅会污染其后 18 窗的浅洞尺子, 且随 windows_*.jsonl 落盘
			// 持久化（重启会重新 seed）。缺一窗由 RecentBlock 的 600s 缺口容差吸收,
			// 与停机窗/no_sigma 窗同效。
			log.Printf("[Cycle] ⚠️ 窗口结束 %s close 采样迟到（边界后 +%v, 循环卡顿跨窗），本窗不计入 σ",
				conditionID, lastSampleAt.Sub(endTime).Round(time.Millisecond))
		default:
			amp := math.Abs(lastTick.TwapPrice - anchor)
			hist.Push(amp)
			// 落盘一行供下次重启 σ 本地预热（与 push 同分支同条件;
			// 失败只告警——σ 内存窗不受影响，仅预热缺此窗）
			if err := recorder.LogWindowAmplitude(conditionID, slug, nextStart.Unix(),
				time.Now(), anchor, lastTick.TwapPrice, amp, rec.src); err != nil {
				log.Printf("[Cycle] ⚠️ 窗口振幅落盘失败: %v（重启本地预热将缺此窗）", err)
			}
			log.Printf("[Cycle] 窗口结束 %s: |close−anchor|=%.2f, σ 现 %d 窗",
				conditionID, amp, hist.Count())
		}

		// 本窗 tick 健康度无条件落盘（含锚缺失/σ 未计的窗口——可见性优先于整洁:
		// 「本窗为什么没信号」必须留下可查的痕迹, 见 logWinstats 注释）
		logWinstats(conditionID, slug, nextStart.Unix(), anchor, histBps, engine.WindowStats(), "", rec)

		// live 首窗禁单解除: 首个完整跑完的窗口结束后置 false。窗口被跳过
		// （continue）则顺延——保守多禁一窗, 防重启残留窗双单的缝隙优先于
		// 交易频率; 解除后 HandleObservation 的 FirstWindow 闸恒放行。
		if runtime.Exec.FirstWindow {
			runtime.Exec.FirstWindow = false
			log.Println("[Trading] 重启后首窗结束, 禁单解除")
		}
	}
}

// runtimeState 是主循环/Dashboard 共用的窗口现场快照载体——只背「当前窗口长
// 什么样」（引擎/适配器/窗口元/盘口闭包）; 成交编排（执行器/闸/两阶段落盘/风控）
// 收敛在 Exec（flip.ExecState, 见 internal/flip exec_state.go——paper/live 同形态, 唯一差异 =
// 是否真实 POST, 不散落在本类型）。mu 保护每窗口换装的字段（Engine/ConditionID/
// Slug/EventStart）: 主循环写（窗口起点 setWindow 换装, 跳窗路径 clearWindow 清空——
// 清空后 Dashboard 显示「等待下一窗口…」而非上一窗陈旧状态），Dashboard goroutine
// 经 Snapshot 读。
type runtimeState struct {
	mu sync.RWMutex

	Engine      *flip.Engine                            // 当前窗口引擎（首个窗口边界前 nil, Snapshot 判空）
	TwapAdapter *feed.TwapAdapter                       // 锚/σ 数据源
	Binance     *feed.BinanceAdapter                    // 浅洞腿 spot 数据源
	ConditionID string                                  // 当前窗口 conditionId（窗口起点换装）
	Slug        string                                  // 当前窗口 slug
	EventStart  int64                                   // 当前窗口起点（unix 秒）
	Mode        string                                  // 成交模式: paper/live（构造后不变, live 缺凭证降级为 paper）
	StartedAt   time.Time                               // 进程启动时刻（构造后不变）
	Exec        *flip.ExecState                         // 信号执行编排（HandleObservation 方法 + LiveSummary; 构造后不变）
	books       func() (*sdk.OrderBook, *sdk.OrderBook) // 当前窗口 UP/DOWN 盘口闭包
}

// Snapshot 实现 dashboard.Snapshotter（Dashboard 每 5s 轮询取快照）。
// Engine 指针在锁内读取；指针自身稳定（主循环只换不释放），
// Engine.State() 内部另有锁，读后调用安全。
// ⚠️ Engine 为 nil 的场景: 启动空窗（Dashboard 先于窗口循环开服, 最长等 ~5 分钟
// 才 setWindow）与跳窗路径（clearWindow: 迟到/市场获取失败/token 缺失/防重入跳过
// 整个窗口）——判空, nil 时状态留空（前端此时显示「等待下一个窗口…」, 口径一致）。
func (rt *runtimeState) Snapshot() flip.LiveSnapshot {
	yb, nb := rt.books()
	pm := feed.NewPMTick(yb, nb)
	twapPrice, twAge := rt.TwapAdapter.Latest()
	bin := rt.Binance.LatestData()

	// spot 显示口径: 未推送显示 0/−1（前端判灰）；有推送则显示最近价与
	// 本地接收龄（前端按 >2s 标红——与引擎判 stale 的阈值一致）
	spotPrice, spotAgeMs := 0.0, int64(-1)
	if bin.RxAtMs > 0 {
		spotPrice = bin.Price
		spotAgeMs = time.Now().UnixMilli() - bin.RxAtMs
	}

	rt.mu.RLock()
	engineState := ""
	anchor := 0.0    // 本窗开盘 TWAP（= 引擎锚; 锚缺失/未就绪为 0, 前端显示「—」）
	eng := rt.Engine // 引擎引用（窗口换装时替换; 计数器读取放到锁外）
	if eng != nil {
		engineState = eng.State().String()
		anchor, _ = eng.WindowAnchor()
	}
	snap := flip.LiveSnapshot{
		Mode:        rt.Mode,
		StartedAt:   rt.StartedAt,
		ConditionID: rt.ConditionID,
		Slug:        rt.Slug,
		EventStart:  rt.EventStart,
		EngineState: engineState,
		UpBid:       pm.UpBid,
		UpAsk:       pm.UpAsk,
		DownBid:     pm.DownBid,
		DownAsk:     pm.DownAsk,
		BookLatMs:   pm.BookLatMs,
		TwapAgeMs:   twAge,
		TwapPrice:   twapPrice,
		TwapOpen:    anchor,
		SpotPrice:   spotPrice,
		SpotAgeMs:   spotAgeMs,
	}
	rt.mu.RUnlock()

	// 本窗 tick 健康度放锁外: 引擎自锁（诊断计数, 与本窗盘口快照同一窗口上下文）。
	// 窗口间（clearWindow 后 Engine=nil）为 nil——前端隐藏本窗统计块。
	if eng != nil {
		st := eng.WindowStats()
		snap.Stats = &st
	}

	// live 摘要放锁外: Exec 构造后不变且方法内部自锁（Recorder 域, 与窗口快照无关）
	// ——全量观测遍历不阻塞 setWindow/clearWindow 的窗口换装写锁（paper 恒 nil,
	// 逻辑见 flip.ExecState.LiveSummary）
	snap.Live = rt.Exec.LiveSummary()
	snap.Risk = rt.Exec.RiskSummary() // 日亏熔断摘要（两模式都填, 与闸判据同源）
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

// clearWindow 清空当前窗口快照（语义 = 无窗口进行中），跳窗 continue 路径调用:
// 否则 Dashboard 在最长一个完整窗口周期内停留在上一窗的陈旧引擎/conditionID
// （上一窗早已结束, 引擎已不再被 tick——清空零副作用）。Snapshot 判 nil Engine,
// 前端显示「等待下一窗口…」, 与启动空窗同口径。
func (rt *runtimeState) clearWindow() {
	rt.mu.Lock()
	rt.Engine = nil
	rt.ConditionID = ""
	rt.Slug = ""
	rt.EventStart = 0
	rt.mu.Unlock()
}

// sampleTick 读取当前盘口/现货/TWAP 构造一条引擎 tick（1s 粒度）。
// 盘口缺失时 bid/ask 为 0（引擎判无效 tick 不检）；spot 新鲜度超阈值置 0
// （= missing_spot）；TWAP 现值随身携带（dist_t 观察腿 + 窗口 close 采样）。
// spot 接收龄随行落盘（spotAgeMs，诊断——不参与判定，见 flip.Observation）。
func sampleTick(t time.Time, rem int, rt *runtimeState, maxSpotAgeMs int64) flip.Tick {
	yb, nb := rt.books()
	pm := feed.NewPMTick(yb, nb)

	// Binance spot（浅洞腿输入）: 本地接收新鲜度 ≤maxSpotAgeMs 才有效——
	// 断流后保留的最后价必须判 stale（交易所时间戳不可作新鲜度判据）。
	// 龄与价格同源于这一次 LatestData 读取（避免「价格是新的、年龄是旧的」自相矛盾）。
	bin := rt.Binance.LatestData()
	spot, spotAge := 0.0, int64(-1)
	if bin.RxAtMs > 0 {
		spotAge = t.UnixMilli() - bin.RxAtMs
		if spotAge <= maxSpotAgeMs {
			spot = bin.Price
		}
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
		SpotAgeMs: spotAge,
		TwapPrice: twapPrice,
		TwapAgeMs: twAge,
	}
}

// resolveLiveMode 决定成交模式与执行器（默认纸面; -mode live 且凭证齐 → 真实下单）。
// 两种实现同一 flip.Executor 契约——差异只在是否真实 POST（见 internal/flip exec_state.go）;
// PaperExecutor 恒为兜底（NewExecutor 内部防线的 "live" 分支不会被走到, main 只
// 在这里分流, 不散落第二处模式判断）。
func resolveLiveMode(mode string, cfgSDK sdk.Config, readOnly bool, client *sdk.PolymarketClient) (string, flip.Executor) {
	if mode != "live" {
		return "paper", flip.NewExecutor("paper")
	}
	// live 依赖三段凭证（全部来自配置文件, 2026-09-16 起不再读环境变量）:
	//   1) sdk.polymarket.owner_key: 订单签名 EOA;
	//   2) sdk.polymarket.clob_creds.{key,secret,passphrase}: CLOB L2 headers;
	//   3) sdk.polymarket.funder_address: Safe 签名(POLY_GNOSIS_SAFE=2)下的 maker
	//      地址——非可选: 缺失则 maker=裸 EOA, 以余额/授权不符被 CLOB 拒单。
	creds := cfgSDK.Polymarket.CLOBCreds
	var missing []string
	if readOnly {
		missing = append(missing, "sdk.polymarket.owner_key")
	}
	if creds == nil || creds.Key == "" || creds.Secret == "" || creds.Passphrase == "" {
		missing = append(missing, "sdk.polymarket.clob_creds.key/secret/passphrase")
	}
	if cfgSDK.Polymarket.FunderAddress == "" {
		missing = append(missing, "sdk.polymarket.funder_address")
	}
	if len(missing) > 0 {
		log.Printf("[Trading] ⚠️ -mode live 但凭证缺失（%s）—— 降级纸面执行", strings.Join(missing, ", "))
		return "paper", flip.NewExecutor("paper")
	}
	addr := cfgSDK.Polymarket.FunderAddress
	if len(addr) > 12 {
		addr = addr[:6] + "…" + addr[len(addr)-4:]
	}
	// 撤单点由 FillTracker 自己的启动日志打印（它拿得到配置里的时间腿）
	log.Printf("[Trading] 🔒 live 就绪: maker=%s（GTC 限价挂单 @ 触发 ask, 绝不超价, 到策略时间腿撤未成交余量; 首窗禁单）", addr)
	return "live", trading.NewLiveExecutor(&trading.SdkClient{Client: client})
}

// pendingResting 挑出磁盘上未决的 GTC 挂单行（resting = 订单仍在 CLOB 簿上、成交量
// 未定稿）。只可能来自 live（paper 即时定稿）; live 模式下由 FillTracker 接管, 非 live
// 模式下必须拒绝启动（见启动处的 🔴 段）。
func pendingResting(recs []*flip.Record) []*flip.Record {
	var out []*flip.Record
	for _, rec := range recs {
		if rec.ExecStatus == flip.ExecStatusResting {
			out = append(out, rec)
		}
	}
	return out
}

// warnLiveStartup 打印 live 启动告警（载入期逐条 ⚠️ 已打, 这里给总量与目录提示;
// paper 无此语义, 调用点已按 effMode 门控）。
func warnLiveStartup(r *flip.Recorder) {
	if n := r.NeedsReconcile(); n > 0 {
		log.Printf("[Trading] ⚠️ %d 条执行中断记录待人工核对（submitting/未知结果, 见上方逐条告警）—— 勿自动补单, 按 maker+时间窗去 data-api 核对", n)
	}
	// 混合目录提示: 当日已有 paper 行（ExecStatus 空）混入会污染信号频率口径与
	// 日亏现算线——live 建议独立 runtime.output_dir 目录（如 data/v4live）。同日
	// live 行（崩溃重启续跑）不算混合。
	// ⚠️ 这里只能提配置键: 目录自 2026-09-16 配置重构起不再有 CLI flag（原 -output
	// 已删除, main 只剩 -config/-dashboard/-mode/-stake）, 照旧文案照做会以
	// "flag provided but not defined" 启动失败。
	today := time.Now().UTC().Format("2006-01-02")
	mixed := false
	for _, rec := range r.Observations() {
		if rec.Date == today && rec.ExecStatus == "" {
			mixed = true
			break
		}
	}
	if mixed {
		log.Printf("[Trading] ⚠️ 输出目录今日已含 paper 行（touches_%s.jsonl）—— live 建议独立 runtime.output_dir 目录（如 data/v4live）, 否则当日 paper/live 混行会污染信号口径与日亏现算线", today)
	}
}

// warmupSigma 做 σ 启动预热: 优先本地 windows_*.jsonl（recorder 每窗落盘的
// |close−anchor|）——「马上重启」场景毫秒级恢复，且与 live push 同源同口径、
// 零上游 API 压力。
//
// 本地可用条件（2026-09-06 review 收紧，防陈旧条目混入）:
//  1. 最近 ≤flip.HistWindows 窗截到最新一段连续块（flip.RecentBlock）——窗口
//     结束时刻对齐 5min 边界，相邻条目正常差 300s；σ 新鲜度守卫缺一窗恰差
//     600s（仍连续）；停机/断档的缺口 ≥3 窗。断档前的条目属更早的波动率
//     regime（如停机数小时后再跑几窗即崩溃重启），混入会把 σ 尺度拉偏——
//     只 seed 连续块，其余丢弃;
//  2. 连续块 ≥ flip.HistMin 窗（不足时本地意义小，走网络拿停机期窗口更接近回测）;
//  3. 块内最新窗结束距今 ≤ localFreshMax（引擎最近在跑）。
//
// 任一不满足即回退官方历史范围网络预热（≤18 窗逐窗间隔 1s ≈ 19s, 接口可能
// 429 限流丢窗——crypto-price 上游限速, 见 FetchTwapRanges 注释）。
func warmupSigma(hist *flip.HistState, r *flip.Recorder, client *sdk.PolymarketClient) {
	seeded := 0
	if wins := r.RecentWindows(flip.HistWindows); len(wins) > 0 {
		if block := flip.RecentBlock(wins, int64(2*windowSec*1000)); len(block) >= flip.HistMin &&
			time.Since(time.UnixMilli(block[len(block)-1].Ts)) <= localFreshMax {
			amps := make([]float64, len(block))
			for i := range block {
				amps[i] = block[i].Amp
			}
			hist.Seed(amps)
			seeded = len(block)
			log.Printf("[Cycle] σ 本地预热: %d 窗（%s ~ %s）",
				len(block),
				time.UnixMilli(block[0].Ts).UTC().Format("15:04:05"),
				time.UnixMilli(block[len(block)-1].Ts).UTC().Format("15:04:05"))
		}
	}
	if seeded == 0 {
		go func() {
			log.Printf("[Cycle] σ 网络预热: 本地窗口不足/断档/陈旧，拉取官方 TWAP 历史范围（≤%d 窗）...", flip.HistWindows)
			vals := feed.FetchTwapRanges(client, flip.HistWindows, windowSec, twapLookbackSeconds)
			hist.Seed(vals)
			log.Printf("[Cycle] σ 预热完成: %d 窗可用", len(vals))
		}()
	}
}
