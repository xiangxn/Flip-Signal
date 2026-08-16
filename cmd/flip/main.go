// Command flip 是 Flip Signal Detection 交易引擎的入口。
//
// 连接 Chainlink TWAP-60 推送（btc-updown-5m 结算口径）、Polymarket CLOB
// WebSocket（订单簿）与 Binance WebSocket（aggTrade + depth20，研究对照），
// 通过 lab.Collector 每 5 秒生成 ResearchSnapshot，使用 flip.Engine 状态机
// 检测 >0.7 穿越信号，并将信号及盈亏记录到 JSONL 文件。
//
// 可选通过 -lab-output 输出完整事件快照（与 cmd/lab 格式一致），
// 无需单独运行 cmd/lab。
//
// 默认只读运行 —— 自动生成临时钱包密钥用于 Polymarket API 访问
// （仅数据读取，不执行交易）。
//
// 用法：
//
//	go run ./cmd/flip -output data/flip_signals.jsonl
//	go run ./cmd/flip -output data/flip_signals.jsonl -lab-output data/lab
//	go run ./cmd/flip -trading                  # 启用实盘交易
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
	"syscall"
	"time"

	"github.com/tidwall/gjson"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"

	"github.com/necklace/flip-signal/internal/config"
	"github.com/necklace/flip-signal/internal/dashboard"
	"github.com/necklace/flip-signal/internal/feed"
	"github.com/necklace/flip-signal/internal/flip"
	"github.com/necklace/flip-signal/internal/lab"
	"github.com/necklace/flip-signal/internal/trading"
)

func init() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
}

func main() {
	// ── CLI 参数：优先级最高，可覆盖所有配置来源 ──
	configPath := flag.String("config", "config.yaml", "配置文件路径（YAML）")
	outputPath := flag.String("output", "", "Flip Signal JSONL 输出路径（覆盖配置文件）")
	labOutputDir := flag.String("lab-output", "", "Lab 数据输出目录（覆盖配置文件）")
	dashboardAddr := flag.String("dashboard", "", "Dashboard 监听地址（覆盖配置文件）")
	symbol := flag.String("symbol", "", "Binance 交易对（覆盖配置文件）")
	slugPrefix := flag.String("slug", "", "Polymarket slug 前缀（覆盖配置文件）")
	tradingEnabled := flag.Bool("trading", false, "启动时启用实盘交易（覆盖配置文件）")
	stakeOverride := flag.Float64("stake", 0, "每信号投入 USDC（0=用配置文件值）")
	maxLossOverride := flag.Float64("max-loss", 0, "日亏上限 USDC（0=用配置文件值）")
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// 优雅退出：在任何 return 路径（含全部 ctx.Done 分支）均执行，
	// 为进行中的操作留出收尾时间后再清理资源。
	defer func() {
		log.Println("[Flip] 正在优雅退出 —— 等待进行中的操作完成（10s）...")
		time.Sleep(10 * time.Second)
		log.Println("[Flip] 退出完成。")
	}()

	// ================================================================
	// 配置加载: 代码默认值 ← 文件配置 ← 环境变量 (优先级从低到高)
	// ================================================================
	cfg, err := config.Load(*configPath)
	if err != nil {
		log.Fatalf("[Config] 加载配置失败: %v", err)
	}

	// ── CLI 覆盖（最高优先级）──
	if *outputPath != "" {
		cfg.Runtime.OutputPath = *outputPath
	}
	if *labOutputDir != "" {
		cfg.Runtime.LabOutputDir = *labOutputDir
	}
	if *dashboardAddr != "" {
		cfg.Runtime.DashboardAddr = *dashboardAddr
	}
	if *symbol != "" {
		cfg.Runtime.Symbol = *symbol
		cfg.Binance.Symbol = *symbol
	}
	if *slugPrefix != "" {
		cfg.Runtime.SlugPrefix = *slugPrefix
	}
	if *tradingEnabled {
		cfg.Trading.Enabled = true
	}
	if *stakeOverride > 0 {
		cfg.Trading.StakePerSignal = *stakeOverride
	}
	if *maxLossOverride > 0 {
		cfg.Trading.MaxDailyLoss = *maxLossOverride
	}

	// ================================================================
	// Polymarket 客户端（未配置 owner key 时只读运行）
	// ================================================================
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
		log.Println("[Flip] ⚠️  未配置 POLYMARKET_OWNER_KEY — 只读运行（纸面交易）")
	} else {
		log.Println("[Flip] 🔑 已配置私钥 — 实盘交易可用")
	}

	// ================================================================
	// Binance 行情适配器
	// ================================================================
	binance := feed.NewBinanceAdapterWithConfig(cfg.Binance)
	go func() {
		if err := binance.Start(ctx); err != nil {
			log.Printf("[Flip] Binance 启动失败: %v", err)
		}
	}()

	// ================================================================
	// Polymarket 订单簿适配器
	// ================================================================
	bookAdapter := feed.NewOrderBookAdapter(
		cfg.SDK.Polymarket.ClobWSBaseURL, client,
	)
	bookAdapter.Start(ctx)

	// ================================================================
	// Chainlink TWAP-60 适配器（btc-updown-5m 结算口径基准价格）
	// ================================================================
	twapMonitor := sdk.NewCryptoPriceMonitor(client, sdk.MonitorChainlinkTwap, "BTC")
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

	// ================================================================
	// 实盘交易执行器（仅在有凭证时构造）
	// ================================================================
	var trader *trading.Trader
	if !readOnly {
		// 创建 TradeMonitor（实时追踪订单成交/状态变更）
		var tradeMon *sdk.TradeMonitor
		if cfg.SDK.Polymarket.CLOBCreds != nil {
			tradeMon = sdk.NewTradeMonitor(
				cfg.SDK.Polymarket.ClobWSBaseURL,
				cfg.SDK.Polymarket.CLOBCreds,
			)
			go func() {
				if err := tradeMon.Run(ctx); err != nil {
					log.Printf("[Trading] TradeMonitor 异常: %v", err)
				}
			}()
			log.Println("[Trading] 📡 TradeMonitor 已启动")
		}

		tradeClient := &trading.SdkClient{Client: client}
		trader = trading.NewTrader(cfg.Trading, tradeClient)
		if err := trader.Start(ctx, tradeMon); err != nil {
			log.Printf("[Trading] ⚠️ 交易记录器启动失败: %v", err)
		}
		if cfg.Trading.Enabled {
			if err := trader.Enable(); err != nil {
				log.Printf("[Trading] ⚠️ 实盘启用失败: %v（保持禁用）", err)
			}
		}
	}
	if trader != nil {
		defer trader.Close()
	}

	// 订单簿 token 追踪
	var (
		yesTok string
		noTok  string
	)

	// ================================================================
	// Lab 采集器（每 5 秒生成 ResearchSnapshot）
	// ================================================================
	collector := lab.NewCollector(binance, 5) // trading engine always uses 5s

	// ================================================================
	// Flip 引擎与信号记录器
	// ================================================================
	flipCfg := cfg.Flip
	histTracker := flip.NewHistRangeTracker(flipCfg.HistWindowN)

	// 等待 Binance 初始数据就绪后再预热历史振幅。
	// 每秒轮询 LatestData()，而非盲等固定时长。
	log.Println("[Flip] 等待 Binance 初始数据...")
	for i := 0; i < 30; i++ {
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Second):
		}
		if btc := binance.LatestData(); btc.Price != 0 {
			log.Printf("[Flip] Binance 数据就绪，耗时 %ds（price=%.2f）", i+1, btc.Price)
			break
		}
	}

	// 等待 Chainlink TWAP 初始数据（首条推送通常在数秒内到达）
	log.Println("[Flip] 等待 Chainlink TWAP 初始数据...")
	for i := 0; i < 60; i++ {
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Second):
		}
		if twapPrice, _ := twapAdapter.Latest(); twapPrice != 0 {
			log.Printf("[Flip] TWAP 数据就绪，耗时 %ds（twap=%.2f）", i+1, twapPrice)
			break
		}
	}

	// 历史振幅基准：默认冷启动 —— 仅由主循环每周期 AddRange(TWAP 官方
	// open, close) 积累，≥3 个窗口后 IsReady，期间 B1/B2 不产生信号。
	// 保证基准口径来自单一采集管线（Phase 3 标定期）。
	// 可选预热：hist_warmup_enabled=true 时用官方 crypto-price 接口拉取
	// 最近 HistWindowN 个窗口振幅，消除 15 分钟冷启动（接口有数据延迟，
	// 口径验证稳定后再启用）。
	if flipCfg.HistWarmupEnabled {
		twapRanges := feed.FetchTwapRanges(client, flipCfg.HistWindowN,
			int64(lab.WindowSec), sdk.ChainlinkTwapWindowSixty)
		for _, r := range twapRanges {
			histTracker.AddRange(0, r) // 注入振幅：AddRange(open,close) = |close-open|
		}
		if histTracker.IsReady() {
			log.Printf("[Flip] TWAP 历史振幅预热完成: %d/%d 个窗口, avg=%.2f",
				len(twapRanges), flipCfg.HistWindowN, histTracker.AvgRange())
		} else {
			log.Printf("[Flip] ⚠️ TWAP 历史振幅预热不足: %d 个窗口（需 ≥3），B1/B2 转入冷启动积累", len(twapRanges))
		}
	} else {
		log.Printf("[Flip] 历史振幅冷启动: 积累 %d 个窗口（~15 分钟）后 B1/B2 可用",
			flipCfg.HistWindowN)
	}

	flipEngine := flip.NewEngine(flipCfg, histTracker)

	flipRecorder, err := flip.NewFlipRecorder(cfg.Runtime.OutputPath)
	if err != nil {
		log.Fatalf("[Flip] 信号记录器创建失败: %v", err)
	}
	defer func() {
		if err := flipRecorder.Close(); err != nil {
			log.Printf("[Flip] 信号记录器关闭异常: %v", err)
		}
	}()

	// ================================================================
	// 结算轮询器：异步轮询 Polymarket gamma API，确认市场结算后再 Resolve
	// 替代原有的 BTC 价格模拟结算（fallbackSettlementTimer）
	// ================================================================
	resolutionPoller := trading.NewResolutionPoller(
		client.FetchMarketBySlug,
		10*time.Second, // 每 10s 轮询一次
		func(conditionID string, outcome int) {
			// 结算 Trader 持仓（ResolveByOutcome 内置去重，重复调用安全）
			if trader != nil {
				trader.ResolveByOutcome(conditionID, outcome)
			}
			// 结算 Flip Signal
			if err := flipRecorder.Resolve(conditionID, outcome); err != nil {
				log.Printf("[Flip] 结算失败: %v", err)
			}
		},
	)
	go resolutionPoller.Run(ctx)

	// 可选：Lab 数据写入器（格式与 cmd/lab 一致）
	var labWriter *lab.Writer
	if cfg.Runtime.LabOutputDir != "" {
		labWriter, err = lab.NewWriter(cfg.Runtime.LabOutputDir)
		if err != nil {
			log.Fatalf("[Flip] Lab 写入器创建失败: %v", err)
		}
		defer func() {
			if err := labWriter.Close(); err != nil {
				log.Printf("[Flip] Lab 写入器关闭异常: %v", err)
			}
		}()
	}

	// 设置成交实时回调：WS 收到成交数据 → 即时同步 FlipRecorder → Dashboard
	if trader != nil {
		trader.SetExecUpdateCallback(func(conditionID, status string, filledShares, avgFillPrice float64) {
			flipRecorder.UpdateExecution(conditionID, status, filledShares, avgFillPrice)
		})
	}

	// 可选：HTTP Dashboard
	mode := "paper"
	if trader != nil && trader.Enabled() {
		mode = "live"
	}
	if cfg.Runtime.DashboardAddr != "" {
		dash := dashboard.New(collector, flipEngine, histTracker, flipRecorder, binance, trader, cfg.Runtime.Symbol, mode, cfg.Trading)
		go dash.ListenAndServe(cfg.Runtime.DashboardAddr)
		log.Printf("[Flip] Dashboard: http://0.0.0.0%s（可通过任意网卡访问）", cfg.Runtime.DashboardAddr)
	}

	modeLabel := "纸面交易"
	if mode == "live" {
		modeLabel = "实盘交易"
	}
	log.Println("========================================")
	log.Printf(" Flip Signal Detection — %s", modeLabel)
	log.Printf(" 交易对: %s  |  Slug: %s  |  输出: %s",
		cfg.Runtime.Symbol, cfg.Runtime.SlugPrefix, cfg.Runtime.OutputPath)
	if cfg.Runtime.LabOutputDir != "" {
		log.Printf(" Lab 数据: %s（events JSONL）", cfg.Runtime.LabOutputDir)
	}
	log.Printf(" 运行参数: trigger>%.1f confirm_delay=%dtick div≥%.2f (floor=%.1f) score_entry≥%d score_add≥%d twap_max_age=%dms",
		flipCfg.TriggerThreshold, flipCfg.ConfirmDelayTicks, flipCfg.MinDivergence, flipCfg.DivergenceFloor, flipCfg.ScoreEntry, flipCfg.ScoreAdd, flipCfg.MaxTwapAgeMs)
	log.Println(" 数据源: [Chainlink TWAP-60(结算基准)] + [Polymarket CLOB books] + [Binance(研究对照)]")
	log.Println("========================================")

	// ================================================================
	// 市场周期主循环
	// ================================================================
	var generation int64

	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		// 步骤 1：计算下一个 5 分钟对齐边界
		now := time.Now()
		alignedTs := now.Unix() / lab.WindowSec * lab.WindowSec
		nextStart := time.Unix(alignedTs, 0)
		// 进程启动后首个周期或时钟漂移：若当前窗口已开始超过 10 秒，
		// 跳过残窗，对齐到下一个完整窗口
		if now.Sub(nextStart) > 10*time.Second {
			nextStart = nextStart.Add(time.Duration(lab.WindowSec) * time.Second)
		}
		marketSlug := fmt.Sprintf("%s-%d", cfg.Runtime.SlugPrefix, nextStart.Unix())

		// 步骤 2：等待至窗口起点（5 分整）。官方 TWAP 开盘价接口有数据
		// 延迟，需从边界起轮询才能及时拿到 openPrice。
		if wait := time.Until(nextStart); wait > 0 {
			log.Printf("[Cycle] next window %s, waiting %v (slug=%s)",
				nextStart.UTC().Format(time.RFC3339), wait.Round(time.Second), marketSlug)
			select {
			case <-ctx.Done():
				return
			case <-time.After(wait):
			}
		}

		// 步骤 2b：后台轮询官方 TWAP 开盘价（10s 间隔，最长 30s，即最多 3 次失败），
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
				log.Printf("[Cycle] ⚠️ TWAP 开盘价不可用（官方超时且流无数据），本周期 B1/B2 特征停用")
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

		// 步骤 3：获取 Binance K 线开盘价（带超时）
		klineDone := make(chan struct{}, 1)
		go func() {
			binance.FetchKlineOpenPrice()
			klineDone <- struct{}{}
		}()
		select {
		case <-klineDone:
		case <-time.After(5 * time.Second):
			log.Printf("[Cycle] ⚠️ K 线获取超时，使用当前价格代替")
		}

		btc := binance.LatestData()
		openPrice := btc.OpenPrice
		if openPrice == 0 {
			openPrice = btc.Price
			log.Printf("[Cycle] ⚠️ K 线开盘价不可用，使用当前价格 %.2f 代替", openPrice)
		}

		// 步骤 4：获取 Polymarket 市场信息 → conditionId + token IDs
		log.Printf("[Cycle] 获取市场信息: %s", marketSlug)
		type marketResult struct {
			data *gjson.Result
			err  error
		}
		marketCh := make(chan marketResult, 1)
		go func() {
			d, err := client.FetchMarketBySlug(marketSlug)
			marketCh <- marketResult{d, err}
		}()
		var marketData *gjson.Result
		var fetchErr error
		select {
		case <-ctx.Done():
			log.Println("[Cycle] 市场信息获取被中断")
			return
		case <-time.After(15 * time.Second):
			fetchErr = fmt.Errorf("市场信息获取超时 (slug=%s)", marketSlug)
		case mr := <-marketCh:
			marketData, fetchErr = mr.data, mr.err
		}
		if fetchErr != nil {
			log.Printf("[Cycle] ⚠️ 获取市场信息失败: %v —— 5 秒后重试", fetchErr)
			// 网络故障时本窗口会被跳过（step 1 的 >10s 漂移保护直接对齐下一窗口）。
			// 必须立即退订上一事件的 token：否则 MarketMonitor 重连时会把缓存的
			// 旧 token 重新订阅回去，已结算资产会触发服务端 close 1000
			// （all subscribed assets resolved）→ 重连 → 再订阅 → 死循环。
			if yesTok != "" || noTok != "" {
				var staleTokens []string
				if yesTok != "" {
					staleTokens = append(staleTokens, yesTok)
				}
				if noTok != "" {
					staleTokens = append(staleTokens, noTok)
				}
				bookAdapter.UnsubscribeTokens(staleTokens...)
				log.Printf("[Cycle] 🔒 已退订上一事件 token（YES=%s NO=%s）", yesTok, noTok)
				yesTok, noTok = "", ""
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(5 * time.Second):
			}
			continue
		}

		conditionID := marketData.Get("conditionId").String()

		// 解析 token ID 和 outcome
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

		// 预热 SDK 缓存：从 gamma API 响应中提取 tickSize / negRisk / feeRate，
		// 注入 SDK 内部缓存，避免 CreateOrder 时额外网络请求（下单延迟敏感）。
		trading.PrefetchTokenInfo(client, marketData, tokenIDs)

		log.Printf("[Cycle] conditionId=%s YES=%s NO=%s negRisk=%v tickSize=%s",
			conditionID, yesTokenID, noTokenID,
			marketData.Get("negRisk").Bool(),
			marketData.Get("orderPriceMinTickSize").String())

		// 步骤 5：订阅新 token 并通知实盘新周期
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

		yesTok = yesTokenID
		noTok = noTokenID

		// 实盘：通知新周期
		if trader != nil {
			trader.NewCycle(conditionID, yesTokenID, noTokenID)
		}

		// 步骤 6：启动事件采集与翻转检测
		collector.StartEvent(conditionID, nextStart.Unix(), openPrice)
		collector.SetTwapOpen(twapOpen)
		generation++
		flipEngine.Reset(generation)
		log.Printf("[Cycle] event=%s binance_open=%.2f twap_open=%.2f 开始采集...",
			conditionID, openPrice, twapOpen)

		ticker := time.NewTicker(5 * time.Second)
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
				// 读取 Polymarket 最新订单簿价格（原子操作，无 channel 开销）
				yb := bookAdapter.GetLatestBook(yesTok)
				nb := bookAdapter.GetLatestBook(noTok)

				collector.UpdatePolymarket(bestBid(yb), bestBid(nb), maxLatency(yb, nb))

				// 读取最新 TWAP-60（B1/B2 特征基准，结算口径）
				twPrice, twAge := twapAdapter.Latest()
				collector.UpdateTwap(twPrice, twAge)

				snap := collector.Tick(tickTime)
				if snap == nil {
					// Binance 数据不可用，仍检查窗口是否结束以防空转
					if tickTime.Unix() >= nextStart.Unix()+lab.WindowSec {
						ticker.Stop()
						break collectLoop
					}
					continue
				}
				snapCount++

				// ── 翻转信号检测 ──
				if sig := flipEngine.ProcessSnapshot(snap, generation); sig != nil {
					sig.ConditionID = conditionID
					// 根据 stake_per_signal 计算目标股数（纸面/实盘统一）
					// 用 FillPrice（确认时刻对侧 ask）而非 EntryPrice（穿越时对侧 bid），
					// 与回测 ask 成交口径一致
					sig.Shares = trading.ComputeShares(cfg.Trading.StakePerSignal, sig.FillPrice)

					if err := flipRecorder.RecordSignal(sig); err != nil {
						log.Printf("[Flip] 信号记录失败: %v", err)
					}
					log.Printf("[Flip] 🎯 SIGNAL: %s>0.7 score=%d entry=%.3f fill=%.3f shares=%.0f | "+
						"div=%+.2f range_exp=%.1f btc_pos=%+.2f other_d=%+.3f rem=%ds twap_age=%dms",
						sig.Side, sig.Score, sig.EntryPrice, sig.FillPrice, sig.Shares,
						sig.BtcDivergence, sig.RangeExpansion, sig.BTCPosition, sig.OtherDelta,
						sig.RemainingSec, sig.TwapAgeMs)

					// ── 交易执行（纸面/实盘统一路径）──
					if trader != nil {
						yesBook := trading.OrderBookToSummary(yb)
						noBook := trading.OrderBookToSummary(nb)
						execInfo, err := trader.OnSignal(sig, yesTok, noTok, yesBook, noBook)
						if err != nil {
							log.Printf("[Trading] ⚠️ 信号未执行: %v", err)
						}
						flipRecorder.UpdateExecution(conditionID, execInfo.Status, execInfo.FilledShares, execInfo.AvgFillPrice)
					} else {
						// 无 Trader：纯纸面模拟成交（成交价 = 确认时刻对侧 ask，回测同口径）
						flipRecorder.UpdateExecution(conditionID, "filled", sig.Shares, sig.FillPrice)
					}
				}

				if snap.RemainingSec <= 0 {
					ticker.Stop()
					break collectLoop
				}

				if snap.RemainingSec%30 == 0 && snap.RemainingSec != lastLogRemaining {
					lastLogRemaining = snap.RemainingSec
					log.Printf("[Event] %s remaining=%ds snaps=%d binance=%.2f twap=%.2f yes=%.4f no=%.4f",
						conditionID, snap.RemainingSec, snapCount,
						snap.CurrentPrice, snap.TwapPrice, snap.YesPrice, snap.NoPrice)
				}
			}
		}

		// 步骤 7：结束事件、持久化 Lab 数据、更新历史振幅、结算信号
		event := collector.FinalizeEvent()

		// 官方 TWAP 结算价修正 —— 异步获取，不阻塞主循环进入下一窗口
		// （同步轮询会延迟下一窗口的穿越检测 ~10s，错过窗口前段信号）。
		// 官方收盘价到达后（或超时回退流采样）在后台完成历史振幅积累与
		// Lab 快照持久化；histTracker 自带锁，跨 goroutine 安全。
		go func(ev *lab.Event, start time.Time) {
			// 收盘轮询 5s 间隔 × 最长 60s（异步执行，不阻塞主循环）
			if officialOpen, officialClose, ok := feed.PollOfficialClosePrice(ctx, client, start,
				int64(lab.WindowSec), sdk.ChainlinkTwapWindowSixty, 60*time.Second); ok {
				ev.TwapOpenPrice = officialOpen
				ev.TwapClosePrice = officialClose
				ev.Outcome = 1 // Down
				if officialClose >= officialOpen {
					ev.Outcome = 0 // Up
				}
			} else if ctx.Err() == nil {
				log.Printf("[Event] ⚠️ %s 官方结算价未就绪，沿用流采样 close", conditionID)
			}

			// 历史振幅按 TWAP 结算口径积累；TWAP 缺失的周期跳过，
			// 避免 0 振幅污染均值（冷启动窗口不参与基准）
			if ev.TwapOpenPrice > 0 && ev.TwapClosePrice > 0 {
				histTracker.AddRange(ev.TwapOpenPrice, ev.TwapClosePrice)
			} else {
				log.Printf("[Event] ⚠️ %s TWAP 数据缺失，跳过历史振幅积累", conditionID)
			}

			// 若启用 Lab 输出，持久化完整事件快照
			if labWriter != nil {
				if err := labWriter.Write(ev); err != nil {
					log.Printf("[Flip] Lab 写入失败: %v", err)
				}
			}

			outcomeLabel := "DOWN/FLAT"
			if ev.Outcome == 0 {
				outcomeLabel = "UP"
			}
			log.Printf("[Event] %s 完成 —— twap open=%.2f close=%.2f outcome=%s | binance open=%.2f close=%.2f snapshots=%d",
				conditionID, ev.TwapOpenPrice, ev.TwapClosePrice, outcomeLabel,
				ev.OpenPrice, ev.ClosePrice, len(ev.Snapshots))
		}(event, nextStart)

		// 实盘交易对账（顺序关键：先对账 GTC 挂单，确保成交数据已回填至 FlipRecorder）
		if trader != nil {
			if ei := trader.OnCycleEnd(event.ConditionID); ei.Status != "" {
				flipRecorder.UpdateExecution(event.ConditionID, ei.Status, ei.FilledShares, ei.AvgFillPrice)
			}
		}

		// 注册异步结算：由 ResolutionPoller 轮询 Polymarket gamma API，
		// 待 umaResolutionStatus=="resolved" 且 outcomePrices 包含 "1" 时自动触发 Resolve
		resolutionPoller.Register(event.ConditionID, marketSlug)

	}
}

// ── 辅助函数 ──

// bestBid 从 Polymarket CLOB 订单簿中提取最优买价。
//
// Polymarket CLOB 的 bids 按价格升序排列：最后一个元素即最高（最优）
// 买价。订单簿为 nil 或为空时返回 0。
func bestBid(book *sdk.OrderBook) float64 {
	if book == nil || len(book.Bids) == 0 {
		return 0
	}
	return book.Bids[len(book.Bids)-1].Price
}

// maxLatency 返回两个订单簿中延迟较大的值（毫秒）。
// 用于将订单簿数据质量传递到 Flip 引擎进行风控。
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

