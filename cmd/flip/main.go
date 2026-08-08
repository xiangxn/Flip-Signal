// Command flip 是 Flip Signal Detection 交易引擎的入口。
//
// 连接 Binance WebSocket（aggTrade + depth20）和 Polymarket CLOB WebSocket
// （订单簿），通过 lab.Collector 每 5 秒生成 ResearchSnapshot，使用 flip.Engine
// 状态机检测 >0.7 穿越信号，并将信号及盈亏记录到 JSONL 文件。
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
	bookAdapter := feed.NewOrderBookAdapterWithResolve(
		cfg.SDK.Polymarket.ClobWSBaseURL, client,
	)
	bookAdapter.Start(ctx)

	// ================================================================
	// 实盘交易执行器（仅在有凭证时构造）
	// ================================================================
	var trader *trading.Trader
	if !readOnly {
		tradeClient := &trading.SdkClient{Client: client}
		trader = trading.NewTrader(cfg.Trading, tradeClient, bookAdapter.SubscribeResolved())
		if err := trader.Start(ctx); err != nil {
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
	collector := lab.NewCollector(binance)

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

	// 预热：拉取历史 5m K 线以初始化 hist_avg_range
	histErr := make(chan error, 1)
	go func() {
		histErr <- histTracker.Warmup(cfg.Binance.RestBaseURL, cfg.Binance.Symbol)
	}()
	select {
	case <-ctx.Done():
		log.Println("[Flip] 历史振幅预热被中断")
		return
	case err := <-histErr:
		if err != nil {
			log.Printf("[Flip] 历史振幅预热失败: %v（F6/F7 将在积累足够周期后退化可用）", err)
		}
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

	// 可选：HTTP Dashboard
	mode := "paper"
	if trader != nil && trader.Enabled() {
		mode = "live"
	}
	if cfg.Runtime.DashboardAddr != "" {
		dash := dashboard.New(collector, flipEngine, histTracker, flipRecorder, binance, trader, cfg.Runtime.Symbol, mode)
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
	log.Printf(" 运行参数: trigger>%.1f confirm_delay=%dtick score_entry≥%d score_add≥%d multi_cross=%v",
		flipCfg.TriggerThreshold, flipCfg.ConfirmDelayTicks, flipCfg.ScoreEntry, flipCfg.ScoreAdd, flipCfg.AllowRetryCrossings)
	log.Println(" 数据源: [Binance aggTrade+depth20] + [Polymarket CLOB books]")
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
		marketSlug := fmt.Sprintf("%s-%d", cfg.Runtime.SlugPrefix, nextStart.Unix())

		// 步骤 2：等待至窗口起点 + 2 秒
		waitUntil := nextStart.Add(2 * time.Second)
		if wait := time.Until(waitUntil); wait > 0 {
			log.Printf("[Cycle] next window %s, waiting %v (slug=%s)",
				nextStart.UTC().Format(time.RFC3339), wait.Round(time.Second), marketSlug)
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

		log.Printf("[Cycle] conditionId=%s YES=%s NO=%s",
			conditionID, yesTokenID, noTokenID)

		// 步骤 5：订阅新 token（先取消旧订阅）
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
		generation++
		flipEngine.Reset(generation)
		log.Printf("[Cycle] event=%s open=%.2f 开始采集...", conditionID, openPrice)

		ticker := time.NewTicker(5 * time.Second)
		snapCount := 0
		lastLogRemaining := 0

	collectLoop:
		for {
			select {
			case <-ctx.Done():
				ticker.Stop()
				return

			case tickTime := <-ticker.C:
				// 读取 Polymarket 最新订单簿价格（原子操作，无 channel 开销）
				yb := bookAdapter.GetLatestBook(yesTok)
				nb := bookAdapter.GetLatestBook(noTok)

				collector.UpdatePolymarket(bestBid(yb), bestBid(nb))

				snap := collector.Tick(tickTime)
				if snap == nil {
					continue
				}
				snapCount++

				// ── 翻转信号检测 ──
				if sig := flipEngine.ProcessSnapshot(snap, generation); sig != nil {
					sig.ConditionID = conditionID
					if err := flipRecorder.RecordSignal(sig); err != nil {
						log.Printf("[Flip] 信号记录失败: %v", err)
					}
					log.Printf("[Flip] 🎯 SIGNAL: %s>0.7 score=%d entry=%.3f shares=%d | "+
						"osc=%v path_eff=%.2f noise=%.1f flips=%d range_exp=%.1f btc_pos=%.2f other_d=%+.3f rem=%ds",
						sig.Side, sig.Score, sig.EntryPrice, sig.Shares,
						sig.IsOscillating, sig.PathEff, sig.NoiseRatio, sig.Flips,
						sig.RangeExpansion, sig.BTCPosition, sig.OtherDelta,
						sig.RemainingSec)

					// ── 实盘执行 ──
					if trader != nil {
						if err := trader.OnSignal(sig, yesTok, noTok); err != nil {
							log.Printf("[Trading] ⚠️ 信号未执行: %v", err)
						}
					}
				}

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

		// 步骤 7：结束事件、持久化 Lab 数据、更新历史振幅、结算信号
		event := collector.FinalizeEvent()
		histTracker.AddRange(event.OpenPrice, event.ClosePrice)

		// 若启用 Lab 输出，持久化完整事件快照
		if labWriter != nil {
			if err := labWriter.Write(event); err != nil {
				log.Printf("[Flip] Lab 写入失败: %v", err)
			}
		}

		outcomeLabel := "DOWN/FLAT"
		if event.Outcome == 0 {
			outcomeLabel = "UP"
		}
		log.Printf("[Event] %s 完成 —— open=%.2f close=%.2f outcome=%s snapshots=%d",
			conditionID, event.OpenPrice, event.ClosePrice, outcomeLabel, len(event.Snapshots))

		// 结算：outcome 0=Up, 1=Down
		if err := flipRecorder.Resolve(event.ConditionID, event.Outcome); err != nil {
			log.Printf("[Flip] 结算失败: %v", err)
		}

		// 实盘结算
		if trader != nil {
			trader.OnCycleEnd(event.ConditionID, event.Outcome)
		}

		// 取消旧 token 订阅
		bookAdapter.UnsubscribeTokens(tokenIDs...)
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

