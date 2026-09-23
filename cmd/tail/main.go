// Command tail 是「扫尾盘」策略引擎入口（口径文档 docs/tail_sweep_2026-09-22.md）。
//
// 与 cmd/flip（狗@0.2）**方向相反、各自独立**：flip 买被砸到 ≤0.20 的冷门侧，本引擎
// 在窗口最后 60s 买**热门侧**（ask 高的一侧）——价格腿 ≥0.80，且现货相对锚的位移
// 满足「≥63 美元」或「1σ 折美元 ≥40 且位移 ≥ 1σ」。14 天回测 n=1536 WR 99.6%
// +36.9U（每笔 2U, 0 亏损日），但文档 §4 判定门槛不可辨识 ⇒ 只纸面登记、不调参。
//
// 数据源与 flip 完全相同：PM CLOB 订单簿（UP/DOWN 四档）+ Chainlink TWAP-60（锚/σ）
// + Binance BTCUSDT spot（位移腿）。锚走决策 #15 的精确取锚（边界那一秒的推送,
// 500ms × 40 = 20s 预算）；σ 走 flip.HistState（前 ≤18 已完窗 |close−anchor| 均值）。
//
// ⚠️ 本窗**至多两行**（2026-09-23 用户口径「两帧折中」，见 tail.Config.FrameRem）:
//   - `rem ≤ frame_rem(150)` 的首个有效 tick → **帧行**（kind=frame）: 只落盘原始快照,
//     不判定、绝不下单——离线可据此复算 T=150 及更早的规则形态;
//   - `rem ≤ rem_start(60)` 的首个有效 tick → **决策快照**（kind=snap）: 五格判定 +
//     执行/结算。**只有这一行可能下单**。
//
// 记录三族（前缀刻意与 flip 的 touches_/windows_/winstats_ 不重合）:
//
//	tail_YYYY-MM-DD.jsonl      每窗 ≤2 行（frame + snap），观测/成交/结算
//	tailwin_YYYY-MM-DD.jsonl   每完成窗 1 行（σ 重启本地预热的数据源）
//	tailstats_YYYY-MM-DD.jsonl **严格每窗 1 行**（tick 健康度 + skip 原因 + 锚状态）
//
// 成交（-mode paper|live，两模式共用同一判定与风控闸）: paper = 模拟全额成交
// （shares = stake/hot_ask，精确除）; live = 真实 CLOB **GTC 限价挂单** @ 热门侧 ask,
// **挂到闭市 rem ≤ 0 才撤**未成交余量（2026-09-23 用户口径；flip 的撤单点是策略时间腿
// rem≤180，本族没有那条时间腿——快照点已在 rem≈60，再提前撤会把手里的位置全撤空）。
// 挂单终态由 trading.FillTracker 撤单时查 size_matched 定稿（闭市 +60s 硬截止兜底）。
//
// Dashboard 不在本命令范围（internal/dashboard 硬绑 flip.Recorder）。
//
// 用法:
//
//	go run ./cmd/tail -config v4.config.yaml                   # 纸面（无需凭证, 不弹密码）
//	go run ./cmd/tail -config v4.config.yaml -mode live        # live 需配置文件里有密文凭证
//	go run ./cmd/tail -stake 5                                 # 单点覆盖（覆盖 tail.stake）
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
	"github.com/necklace/flip-signal/internal/feed"
	"github.com/necklace/flip-signal/internal/flip"
	"github.com/necklace/flip-signal/internal/tail"
	"github.com/necklace/flip-signal/internal/trading"
)

// windowSec 是 btc-updown-5m 窗口长度（秒）。
const windowSec = 300

// prefetchLead 是市场信息预取提前量：窗口边界前多少秒开始调 FetchMarketBySlug。
const prefetchLead = 20 * time.Second

// lateLimit 是订阅迟到阈值：已落后窗口边界超过该时长则跳过本窗口。
// 本族对迟到的容忍度远高于 flip——判定点在 rem≤60（边界后 240s），故迟到几十秒
// 也不影响「尾盘快照」本身；但窗口**起点**的锚采样与 σ 对账仍需完整窗口，且迟到
// 窗口的两帧闸值（150/60）在语义上不再对应干净的首帧。沿用 flip 的 15s 是保守选择
// （宁可丢窗，也不拿残缺窗口凑样本）。
const lateLimit = 15 * time.Second

// twapMaxStale 是 TWAP 推送新鲜度阈值: 超过该时长未收到推送则重建订阅
// （TwapAdapter 内建看门狗，与 cmd/flip 同值）。
const twapMaxStale = 2 * time.Minute

// twapLookbackSeconds 是结算口径 TWAP 回看窗口秒数（Chainlink TWAP-60）。
const twapLookbackSeconds = 60

// 精确取锚参数（决策 #15）: 锚 = **边界那一秒**的 TWAP-60 评估值（实测与官方
// openPrice 收敛值逐位相同）。该条推送 p50 +2.0s 到达，20s 预算 = p50 的 10 倍余量，
// 命中即终局；预算耗尽 = 本窗无锚（引擎 anchor ≤0 一行不产出，只落 tailstats）。
//
// 与 flip 的关键差别在**时序余量**: 本族最早的产出帧在 rem≤150（边界后 +150s），
// 而取锚通道 +20s 就结束了——**锚在产出任何行之前早已定局**，不存在「帧用了旧锚、
// 快照用了新锚」的不一致窗口（引擎侧另有 emitted 冻结做双保险）。
const (
	anchorExactAttempts = 40                     // 精确取锚尝试次数（× 间隔 = 20s 预算）
	anchorExactInterval = 500 * time.Millisecond // 精确取锚尝试间隔
)

// localFreshMax 是 σ 本地预热的新鲜度上限（同 cmd/flip）: 最新已落盘窗口结束距今
// ≤ 该值才可信（= 引擎最近在跑）；停机更久则回退官方网络预热。
const localFreshMax = time.Duration(flip.HistMin*windowSec) * time.Second

// anchorInfo 是本窗锚的可见性字段（tailstats 落盘用）。
// 只由取锚 goroutine 写、主循环在 cancel + join 之后读——channel close 建立的
// happens-before 保证无数据竞争。
type anchorInfo struct {
	src  string // feed.AnchorSourceStream（空 = 本窗未取到锚; 官方段休眠时恒 stream）
	atMs int64  // 该推送的**本地到达时刻**（unix 毫秒; 仅取到锚时非 0）
}

// marketCache 缓存下一窗口的市场信息（稳态预取: 本窗 tick 尾部预取，loop 顶部复用）。
type marketCache struct {
	slug string
	res  *gjson.Result
}

// sampler 是 tick 采样所需的运行时组件：盘口闭包（monitor goroutine 写）+ 两个数据源。
// 不持有窗口状态——引擎挂在主循环里（本命令没有 Dashboard，无跨 goroutine 读取需求）。
type sampler struct {
	Binance     *feed.BinanceAdapter
	TwapAdapter *feed.TwapAdapter
	books       func() (*sdk.OrderBook, *sdk.OrderBook) // 当前窗口 UP/DOWN 盘口
}

func init() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
}

func main() {
	// ── CLI 参数（与 cmd/flip 同形, 去掉 -dashboard 与 -encrypt）──
	configPath := flag.String("config", "", "配置文件路径（YAML; 空 = 只用代码默认值）")
	mode := flag.String("mode", "", "成交模式: paper|live（覆盖 runtime.mode）")
	stake := flag.Float64("stake", 0, "每信号投入 USDC（覆盖 tail.stake）")
	flag.Parse()

	// set 记录「哪些 flag 被显式给出」（用 flag.Visit 而非比零值）: -stake 0 会走
	// 校验报错（响亮）而不是静默回退。
	set := map[string]bool{}
	flag.Visit(func(f *flag.Flag) { set[f.Name] = true })

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// ── 配置加载（CLI flag > 配置文件 > 代码默认值; 不读环境变量）──
	cfg, err := config.Load(*configPath)
	if err != nil {
		log.Fatalf("[Config] %v", err)
	}
	if set["mode"] {
		cfg.Runtime.Mode = *mode
	}
	if set["stake"] {
		cfg.Tail.Stake = *stake
	}
	// 校验判的是最终生效值，必须在覆盖之后（config.Validate 同时校验 flip 节——
	// 一个结构体一份配置, 两节都在里面）
	warns, verr := config.Validate(cfg)
	if verr != nil {
		log.Fatalf("[Config] 配置校验失败: %v", verr)
	}
	for _, w := range warns {
		log.Printf("⚠️  [Config] %s", w)
	}

	// ── Polymarket 客户端（未写 owner_key 则生成临时密钥，只读运行）──
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
		log.Println("[Tail] ⚠️  配置文件未提供 sdk.polymarket.owner_key —— 只读运行（纸面交易）")
	}

	// ── 成交执行器 + live 分流 ──
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
				log.Printf("[Tail] ⚠️ MarketMonitor 异常退出: %v —— 5 秒后重启", err)
			} else {
				log.Printf("[Tail] ⚠️ MarketMonitor 干净退出 —— 5 秒后重启")
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

	// ── Binance BTCUSDT spot（位移腿现货参考价，不出单）──
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

	// ── 记录器（tail_ / tailwin_ / tailstats_ 三族，互不干扰）──
	recorder, err := tail.NewRecorder(cfg.Runtime.OutputDir)
	if err != nil {
		log.Fatalf("[Tail] 记录器创建失败: %v", err)
	}
	defer recorder.Close()

	// ── 🔴 未决 GTC 挂单 × 非 live 模式 = 拒绝启动（与 cmd/flip 同一条红线）──
	// resting 行只可能来自 live（paper 即时定稿, 永不落 resting）——那笔真单还在 CLOB
	// 簿上等对手方。paper 模式既不能撤单也不能定稿: 重启后它会一直吃到闭市，行悬在
	// 磁盘上、日亏熔断与 P&L 都看不见它。故宁可拒绝启动，逼运维带凭证以 live 重启接管。
	if effMode != "live" {
		if pend := pendingResting(recorder.Observations()); len(pend) > 0 {
			for _, rec := range pend {
				log.Printf("[Tail] 🔴 未决挂单遗留: order=%q status=%s slug=%s event_start=%d note=%q",
					rec.OrderID, rec.ExecStatus, rec.Slug, rec.EventStart, rec.ExecNote)
			}
			log.Fatalf("[Tail] 🔴 检测到 %d 条未决 GTC 挂单（磁盘 resting 行）, 而本次是 %s 模式——该模式既不能撤单也不能定稿。请带凭证以 -mode live 重启接管（首轮查询即定稿并撤余量）; 若该单早已了结, 按 order_id 去 data-api/UI 核对后人工结清该行再跑 paper",
				len(pend), effMode)
		}
	}

	resolutionPoller := trading.NewResolutionPoller(
		client.FetchMarketBySlug,
		10*time.Second,
		func(conditionID string, outcome int) error {
			// Resolve 返回 false = recorder.pending 中无此市场（重复回调/已结算摘除）。
			// ⚠️ 帧行不注册结算, 故本族只有 snap 行可能命中（一窗至多一行, 无歧义）。
			if !recorder.Resolve(conditionID, outcome, time.Now()) {
				log.Printf("[Tail] ⚠️ 结算回填未命中 %s outcome=%d（pending 中无此市场）",
					conditionID, outcome)
			}
			return nil
		},
	)
	// 重启恢复: 磁盘上未结算信号（崩溃遗留）重新注册结算轮询
	for _, sig := range recorder.PendingSignals() {
		resolutionPoller.Register(sig.ConditionID, sig.Slug)
		log.Printf("[Tail] 🔄 恢复未结算信号: %s slug=%s", sig.ConditionID, sig.Slug)
	}
	go resolutionPoller.Run(ctx)

	if effMode == "live" {
		warnLiveStartup(recorder)
	}

	// ── 信号执行编排（paper/live 同源, 差异仅在 Ex 是否真实 POST）──
	exec := &tail.ExecState{
		Rec:          recorder,
		Ex:           executor,
		Live:         effMode == "live",
		Stake:        cfg.Tail.Stake,
		MaxDailyLoss: cfg.Risk.MaxDailyLoss,
		FirstWindow:  effMode == "live", // live 首窗禁单（重启防双单缝隙）
		Tokens: func() (string, string) {
			tokMu.RLock()
			defer tokMu.RUnlock()
			return upTok, downTok
		},
	}

	// ── GTC 挂单跟踪 ──
	// 撤单点 = **闭市**（trading.CancelAtClose）: 快照在 rem≈60 产出，挂单只等 ~1 分钟
	// 就撤等于白挂（且会把「热门侧走弱」的那批位置全部让出）。挂到闭市由 CLOB 自动
	// 结清, 我们在 rem ≤ 0 时主动撤掉余量并查 size_matched 定稿——闭市后 +60s 硬截止兜底。
	// ⚠️ cancelLead 必须是**微小正数**: ≤0 会被 NewFillTracker 当成"未配置"回退 180s。
	fillTracker := trading.NewFillTracker(&trading.SdkClient{Client: client},
		trading.CancelAtClose, func(f flip.FillFinal) {
			rec := exec.ApplyFillFinal(f)
			if rec != nil && rec.IsFilled() {
				resolutionPoller.Register(rec.ConditionID, rec.Slug)
			}
		})
	go fillTracker.Run(ctx)
	// 重启接管: 进程死在挂单期间 → 磁盘上的 resting 行交回跟踪
	if effMode == "live" {
		for _, rec := range recorder.Observations() {
			if rec.ExecStatus == flip.ExecStatusResting {
				fillTracker.RegisterOrder(fillOrderOf(rec), time.Unix(rec.EventStart, 0).Add(windowSec*time.Second), true)
			}
		}
	}

	// σ 启动预热（本地 tailwin_*.jsonl 优先, 不足/陈旧回退官方网络预热）
	hist := flip.NewHistState()
	warmupSigma(hist, recorder, client)

	log.Println("========================================")
	if effMode == "live" {
		log.Printf(" 扫尾盘⑤ — 🔒 实盘交易（GTC 限价挂单 @ 热门侧 ask, 挂到闭市撤余量, 日亏熔断 ≤%.1fU）",
			cfg.Risk.MaxDailyLoss)
	} else {
		log.Printf(" 扫尾盘⑤ — 纸面交易（mode=%s, 日亏熔断 ≤%.1fU 影子: 只标记不拦单）",
			effMode, cfg.Risk.MaxDailyLoss)
	}
	log.Printf(" 输出: %s（tail_/tailwin_/tailstats_ 三族） |  Slug: %s",
		cfg.Runtime.OutputDir, cfg.Runtime.SlugPrefix)
	log.Printf(" 参数: 两帧 rem≤%ds(帧)/rem≤%ds(决策) 热门侧 ask≥%.2f 位移≥%.0f美元 或 (1σ≥%.0f美元 且 位移≥1σ) stake=%.0fUSDC",
		cfg.Tail.FrameRem, cfg.Tail.RemStart, cfg.Tail.PriceMin, cfg.Tail.DevMinUSD, cfg.Tail.SigmaMinUSD, cfg.Tail.Stake)
	log.Printf(" 新鲜度闸: book_lat≤%dms + spot_age≤%dms + twap_age≤%dms（tail.max_book_lat_ms / feed.*）",
		cfg.Tail.MaxBookLatMs, cfg.Feed.MaxSpotAgeMs, cfg.Feed.MaxTwapAgeMs)
	log.Println(" 数据源: [PM CLOB books 1s] + [Chainlink TWAP-60 锚/σ] + [Binance spot 位移]")
	log.Println("========================================")

	sam := &sampler{Binance: binance, TwapAdapter: twapAdapter}
	sam.books = func() (*sdk.OrderBook, *sdk.OrderBook) {
		bookMu.RLock()
		defer bookMu.RUnlock()
		return upBook, downBook
	}

	// logStats 落盘一行本窗 tick 健康度（tailstats_*.jsonl, **每窗无条件一行**）。
	// 含被跳过的窗口（skip 非空）: 逐日行数（≈288）本身即「主循环跑满」的证据。
	logStats := func(condID, slug string, eventStart int64, anchor, hb float64,
		st tail.WindowStats, skip string, ar anchorInfo) {
		e := tail.StatsRow{
			Ts: time.Now().UnixMilli(), ConditionID: condID, Slug: slug,
			EventStart: eventStart, Skip: skip, Anchor: anchor, HistBps: hb,
			WindowStats: st, AnchorExact: ar.src != "", AnchorSrc: ar.src,
		}
		if ar.atMs > 0 {
			e.AnchorArrivedMs = ar.atMs - eventStart*1000 // 该推送本地到达时刻距边界
		}
		if err := recorder.LogWindowStats(e); err != nil {
			log.Printf("[Cycle] ⚠️ 窗口健康度落盘失败: %v", err)
		}
	}

	// ── 市场周期主循环 ──
	var nextCache *marketCache
	// prevWindowStart 是上一轮迭代处理的窗口起点（unix 秒; 0 = 本进程还没处理过）。
	// 用途同 cmd/flip: 区分「刚跑完的窗口」与真·迟到——collectLoop 收尾总在边界前
	// 0~1s 回来，此刻 floor(now/300) 仍指回刚结束的那一窗（假 late 会污染逐日行数）。
	var prevWindowStart int64
	for {
		select {
		case <-ctx.Done():
			log.Println("[Tail] 正在关闭...")
			return
		default:
		}

		// 步骤 1: 对齐下一个 5 分钟窗口
		now := time.Now()
		alignedTs := now.Unix() / windowSec * windowSec
		nextStart := time.Unix(alignedTs, 0)
		if elapsed := time.Since(nextStart); elapsed > lateLimit {
			if nextStart.Unix() != prevWindowStart {
				log.Printf("[Cycle] ⚠️ 已落后窗口边界 %v（>%v），跳过本窗口 %s",
					elapsed.Round(time.Second), lateLimit,
					nextStart.UTC().Format(time.RFC3339))
				logStats("", "", nextStart.Unix(), 0, 0, tail.WindowStats{}, "late", anchorInfo{})
			}
			nextStart = nextStart.Add(windowSec * time.Second)
		}
		prevWindowStart = nextStart.Unix()
		slug := fmt.Sprintf("%s-%d", cfg.Runtime.SlugPrefix, nextStart.Unix())

		// 步骤 2: 市场信息（优先用本窗 tick 期间预取的缓存；未命中则边界前 20s 预取）
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
				logStats("", slug, nextStart.Unix(), 0, 0, tail.WindowStats{}, "no_market", anchorInfo{})
				waitTo(nextStart.Add(windowSec*time.Second), ctx)
				continue
			}
		}
		conditionID := marketData.Get("conditionId").String()
		upTokenID, downTokenID := feed.ParseMarketTokens(marketData)
		if upTokenID == "" || downTokenID == "" {
			log.Printf("[Cycle] ⚠️ 市场 %s token 解析为空，跳过本窗口", slug)
			logStats(conditionID, slug, nextStart.Unix(), 0, 0, tail.WindowStats{}, "no_token", anchorInfo{})
			waitTo(nextStart.Add(windowSec*time.Second), ctx)
			continue
		}
		// 防重入（一窗至多一组样本）: 已有**决策快照**即整窗跳过——该窗的判定（可能
		// 已下单）已经做过, 重跑会写下第二条 snap（Recorder.pending 以 conditionID
		// 为键, 后记覆盖先记 → 先记的一笔永不结算）且可能同窗二次下单。
		// ⚠️ 只认 KindSnap: 帧行存在**不足以**跳窗（帧在 rem≤150、快照在 rem≤60，
		// 中间有 90s 的崩溃窗口）——那种情形要续跑, 帧的重写由 HandleFrame 自己跳过。
		if recorder.HasKind(conditionID, tail.KindSnap) {
			log.Printf("[Cycle] ⚠️ 窗口 %s 已有决策快照（%s 残留，快速重启重入），跳过整窗防双记", slug, conditionID)
			logStats(conditionID, slug, nextStart.Unix(), 0, 0, tail.WindowStats{}, "dup_record", anchorInfo{})
			waitTo(nextStart.Add(windowSec*time.Second), ctx)
			continue
		}
		if recorder.HasKind(conditionID, tail.KindFrame) {
			log.Printf("[Cycle] ↩️ 窗口 %s 已有帧行但无决策快照（崩溃于两帧之间），续跑本窗（帧重写由 HandleFrame 跳过）", slug)
		}
		// σ 未就绪整窗跳过（同 cmd/flip 决策 #13）: hist.Bps 恒 0 时任何快照都被
		// no_hist 拒（且观测行 hist_bps=0 会踩对账硬检查）。与锚缺失同一条原则——
		// 数据源不可信 → 本窗不产出样本。下一窗 5min 后, 预热早已完成。
		if hist.Count() < flip.HistMin {
			log.Printf("[Cycle] ⚠️ 窗口 %s σ 未就绪（%d < %d 窗，预热中），跳过本窗口",
				slug, hist.Count(), flip.HistMin)
			logStats(conditionID, slug, nextStart.Unix(), 0, 0, tail.WindowStats{}, "no_sigma", anchorInfo{})
			waitTo(nextStart.Add(windowSec*time.Second), ctx)
			continue
		}
		if effMode == "live" {
			trading.PrefetchTokenInfo(client, marketData, []string{upTokenID, downTokenID})
		}
		log.Printf("[Cycle] conditionId=%s UP=%s DOWN=%s", conditionID, upTokenID, downTokenID)

		// 步骤 4: 订阅切换
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

		// 步骤 5: 1s tick 采集 + 两帧判定
		// **不设过渡锚**（决策 #15）: 锚留 0，由取锚通道精确命中后经 UpgradeAnchor 注入;
		// 锚未到手期间 tick 照常占槽与计数, 只是不推进闩锁（引擎 anchor≤0 路径）。
		engine := tail.NewEngine(cfg.Tail)
		engine.BeginWindow(0, 0)
		endTime := nextStart.Add(windowSec * time.Second)

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
					continue // 本窗已产出帧（锚已冻结）或窗口已结束
				}
				rec.src, rec.atMs = r.Source, r.AtMs
				log.Printf("[Anchor] ✅ 窗口 %s 锚 %.2f（%s, σ=%.2fbps; 边界后 +%dms）",
					conditionID, r.Price, r.Source, bps, r.AtMs-nextStart.UnixMilli())
			}
			if rec.src == "" {
				n, newestOff, noTs := twapAdapter.CacheStat()
				log.Printf("[Anchor] ⚠️ 窗口 %s %d 次 × %v 未取到边界那一秒的 open"+
					"（缓存 %d 条, 最新一条评估偏移 %+dms, 缺时间戳 %d 条）, 本窗不产出样本",
					conditionID, anchorExactAttempts, anchorExactInterval, n, newestOff, noTs)
			}
		}()

		log.Printf("[Cycle] event=%s 窗口开始（锚待精确命中, 不设过渡锚; σ %d 窗就绪; 帧闸 rem≤%d）",
			conditionID, hist.Count(), cfg.Tail.FrameRem)

		ticker := time.NewTicker(time.Second)
		lastTick := flip.Tick{}
		// 上一条 tick 的**采样真实时刻**（wall clock）——与 lastTick.Ts 分开: Ts 是
		// ticker 的计划时刻, 循环被卡住时它会明显落后于真实时刻, 而窗末 close 的
		// 迟到判据恰恰要靠真实时刻（见窗口结束后的 σ 段）。
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
				lastSampleAt = time.Now()
				lastTick = sam.tick(tickTime, rem, cfg.Feed.MaxSpotAgeMs)

				// 引擎驱动: 0~2 行（帧 + 决策快照）。**帧行只落盘**——它不带判定、
				// 更不带仓位, 走执行路径会让本窗在价格还没到 0.80 时就下单。
				for _, o := range engine.ProcessTick(lastTick) {
					if o.Kind == tail.KindFrame {
						if r := exec.HandleFrame(&o, conditionID, slug, nextStart.Unix()); r != nil {
							log.Printf("[Tail] 📸 帧已落盘 %s rem=%d hot=%s ask=%.3f（原始快照, 不判定）",
								conditionID, o.Rem, o.Side, o.HotAsk)
						}
						continue
					}
					rec := exec.HandleObservation(&o, conditionID, slug, nextStart.Unix())
					if rec == nil || !rec.OK {
						continue
					}
					// 结算只注册确定持仓（paper 行恒成交; live filled/partial）。
					// GTC 的 resting 行仓位未定——交 FillTracker 定稿, 由它的回调注册。
					switch {
					case rec.ExecStatus == flip.ExecStatusResting:
						fillTracker.RegisterOrder(fillOrderOf(rec), endTime, false)
					case rec.IsFilled():
						resolutionPoller.Register(conditionID, slug)
					}
				}
				if rem == 0 {
					ticker.Stop()
					break collectLoop
				}
				// 稳态预取: 本窗 rem≤20 预取下一窗市场信息, rem%5==0 提供失败重试点
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
				// 每 30s 打印一次窗口进度（帧闸在 rem≤150, 之后才谈判定）
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
		anchor, histBps := engine.WindowAnchor()

		// 步骤 6: 窗口结束 → σ 滚动窗追加本窗振幅（严格只用已结束窗口）。
		// 判据与 cmd/flip 逐条一致（缺锚/close 缺失/流值陈旧/采样迟到 —— 假振幅会
		// 污染其后 18 窗的 σ 尺子, 而本族的 σ 腿直接吃它）。
		switch {
		case anchor <= 0 || lastTick.TwapPrice <= 0:
			log.Printf("[Cycle] ⚠️ 窗口结束 %s 但 close/anchor 缺失，本窗不计入 σ", conditionID)
		case lastTick.TwapAgeMs > cfg.Feed.MaxTwapAgeMs:
			log.Printf("[Cycle] ⚠️ 窗口结束 %s TWAP 陈旧（龄 %dms），本窗不计入 σ",
				conditionID, lastTick.TwapAgeMs)
		case lastSampleAt.Sub(endTime) > time.Second:
			log.Printf("[Cycle] ⚠️ 窗口结束 %s close 采样迟到（边界后 +%v, 循环卡顿跨窗），本窗不计入 σ",
				conditionID, lastSampleAt.Sub(endTime).Round(time.Millisecond))
		default:
			amp := math.Abs(lastTick.TwapPrice - anchor)
			hist.Push(amp)
			// 落盘一行供下次重启 σ 本地预热（失败只告警——σ 内存窗不受影响）
			if err := recorder.LogWindowAmplitude(flip.WindowEntry{
				Ts: time.Now().UnixMilli(), ConditionID: conditionID, Slug: slug,
				EventStart: nextStart.Unix(),
				Anchor:     anchor, Close: lastTick.TwapPrice, Amp: amp, AnchorSrc: rec.src,
			}); err != nil {
				log.Printf("[Cycle] ⚠️ 窗口振幅落盘失败: %v（重启本地预热将缺此窗）", err)
			}
			log.Printf("[Cycle] 窗口结束 %s: |close−anchor|=%.2f, σ 现 %d 窗",
				conditionID, amp, hist.Count())
		}

		// 本窗 tick 健康度无条件落盘（含锚缺失/σ 未计的窗口——可见性优先于整洁）
		logStats(conditionID, slug, nextStart.Unix(), anchor, histBps, engine.WindowStats(), "", rec)

		// live 首窗禁单解除: 首个完整跑完的窗口结束后置 false。窗口被跳过（continue）
		// 则顺延——保守多禁一窗，防重启残留窗双单的缝隙优先于交易频率。
		if exec.FirstWindow {
			exec.FirstWindow = false
			log.Println("[Trading] 重启后首窗结束, 禁单解除")
		}
	}
}

// tick 读取当前盘口/现货/TWAP 构造一条引擎 tick（1s 粒度）。
// 盘口缺失时 bid/ask 为 0（引擎判无效 tick、不推进闩锁）；spot 新鲜度超阈值置 0
// （= missing_spot，快照行会据此整窗丢弃）。
func (s *sampler) tick(t time.Time, rem int, maxSpotAgeMs int64) flip.Tick {
	yb, nb := s.books()
	pm := feed.NewPMTick(yb, nb)

	bin := s.Binance.LatestData()
	spot, spotAge := 0.0, int64(-1)
	if bin.RxAtMs > 0 {
		spotAge = t.UnixMilli() - bin.RxAtMs
		if spotAge <= maxSpotAgeMs {
			spot = bin.Price
		}
	}

	twapPrice, twAge := s.TwapAdapter.Latest()
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

// waitTo 睡到指定时刻（期间可被 ctx 取消）。窗口被跳过时用它整窗等待。
func waitTo(t time.Time, ctx context.Context) {
	if wait := time.Until(t); wait > 0 {
		select {
		case <-ctx.Done():
		case <-time.After(wait):
		}
	}
}

// fillOrderOf 把一条 tail.Record 折成 FillTracker 的登记参数。
// 限价取 HotAsk（= 快照时的热门侧 ask，即挂单限价）; 投入取 Stake（目标股数
// = floor2(Stake/HotAsk), 由 FillTracker 自己算）。
func fillOrderOf(rec *tail.Record) trading.FillOrder {
	return trading.FillOrder{
		OrderID:     rec.OrderID,
		ConditionID: rec.ConditionID,
		Slug:        rec.Slug,
		Limit:       rec.HotAsk,
		Stake:       rec.Stake,
	}
}

// resolveLiveMode 决定成交模式与执行器（默认纸面; -mode live 且凭证齐 → 真实下单）。
// 与 cmd/flip 同源（同一批凭证判据），只是撤单点语义由 FillTracker 的 cancelLead 决定
// （本族传 trading.CancelAtClose = 挂到闭市）。
func resolveLiveMode(mode string, cfgSDK sdk.Config, readOnly bool, client *sdk.PolymarketClient) (string, flip.Executor) {
	if mode != "live" {
		return "paper", flip.NewExecutor("paper")
	}
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
	log.Printf("[Trading] 🔒 live 就绪: maker=%s（GTC 限价挂单 @ 热门侧 ask, 挂到闭市才撤余量; 首窗禁单）", addr)
	return "live", trading.NewLiveExecutor(&trading.SdkClient{Client: client})
}

// pendingResting 挑出磁盘上未决的 GTC 挂单行（resting = 订单仍在 CLOB 簿上、成交量
// 未定稿）。只可能来自 live; live 模式下由 FillTracker 接管，非 live 模式必须拒绝启动。
func pendingResting(recs []*tail.Record) []*tail.Record {
	var out []*tail.Record
	for _, rec := range recs {
		if rec.ExecStatus == flip.ExecStatusResting {
			out = append(out, rec)
		}
	}
	return out
}

// warnLiveStartup 打印 live 启动告警（载入期逐条 ⚠️ 已打, 这里给总量与目录提示）。
func warnLiveStartup(r *tail.Recorder) {
	if n := r.NeedsReconcile(); n > 0 {
		log.Printf("[Trading] ⚠️ %d 条执行中断记录待人工核对（submitting/未知结果, 见上方逐条告警）—— 勿自动补单, 按 maker+时间窗去 data-api 核对", n)
	}
	// 混合目录提示: 当日已有 paper 行（ExecStatus 空）混入会污染信号频率口径与日亏
	// 现算线——live 建议独立 runtime.output_dir（如 data/tail-live）。
	// ⚠️ 两族（flip/tail）也建议分目录: 同一个目录会各写各的前缀, 不会串读,
	// 但「当日 P&L」这类按目录现算的口径会把两族混在一起。
	today := time.Now().UTC().Format("2006-01-02")
	for _, rec := range r.Observations() {
		if rec.Date == today && rec.ExecStatus == "" {
			log.Printf("[Trading] ⚠️ 输出目录今日已含 paper 行（tail_%s.jsonl）—— live 建议独立 runtime.output_dir 目录, 否则当日 paper/live 混行会污染信号口径与日亏现算线", today)
			return
		}
	}
}

// warmupSigma 做 σ 启动预热: 优先本地 tailwin_*.jsonl（recorder 每窗落盘的
// |close−anchor|）——「马上重启」场景毫秒级恢复，且与 live push 同源同口径、
// 零上游 API 压力。
//
// 本地可用条件（与 cmd/flip 逐条同源，只是数据源换成 tailwin_*）:
//  1. 最近 ≤flip.HistWindows 窗截到最新一段连续块（flip.RecentBlock）——断档前的
//     条目属更早的波动率 regime，混入会把 σ 尺度拉偏，只 seed 连续块;
//  2. 连续块 ≥ flip.HistMin 窗（不足时本地意义小，走网络更接近回测）;
//  3. 块内最新窗结束距今 ≤ localFreshMax（引擎最近在跑）。
//
// ⚠️ 两族各自独立预热（flip 读 windows_*, tail 读 tailwin_*）: 若两族都要「回测
// 口径的历史 σ」，网络预热取的是官方 TWAP 历史（同源）; 本地预热则各读各的——
// 同一目录下互不串读（前缀不同），分别部署时也各自完整。
func warmupSigma(hist *flip.HistState, r *tail.Recorder, client *sdk.PolymarketClient) {
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
