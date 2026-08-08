// Command flip runs the Flip Signal Detection paper trading engine.
//
// It connects to Binance WebSocket (aggTrade + depth20) and Polymarket
// CLOB WebSocket (order books), generates 5-second ResearchSnapshots via
// lab.Collector, detects >0.7 crossing signals using the flip.Engine
// state machine, and records signals + P&L to a JSONL file.
//
// Optionally also writes full event snapshots as lab data (-lab-output flag),
// so you don't need to run cmd/lab separately.
//
// Read-only by default — generates a temporary wallet key for
// Polymarket API access (data reading only, no trading).
//
// Usage:
//
//	go run ./cmd/flip -output data/flip_signals.jsonl
//	go run ./cmd/flip -output data/flip_signals.jsonl -lab-output data/lab
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

	"github.com/spf13/viper"
	"github.com/tidwall/gjson"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"

	"github.com/necklace/flip-signal/internal/dashboard"
	"github.com/necklace/flip-signal/internal/feed"
	"github.com/necklace/flip-signal/internal/flip"
	"github.com/necklace/flip-signal/internal/lab"
)

func init() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
}

func main() {
	// ── CLI flags: 优先级最高，可覆盖所有配置来源 ──
	configPath := flag.String("config", "config.yaml", "配置文件路径（YAML）")
	outputPath := flag.String("output", "", "Flip Signal JSONL 输出路径（覆盖配置文件）")
	labOutputDir := flag.String("lab-output", "", "Lab 数据输出目录（覆盖配置文件）")
	dashboardAddr := flag.String("dashboard", "", "Dashboard 监听地址（覆盖配置文件）")
	symbol := flag.String("symbol", "", "Binance 交易对（覆盖配置文件）")
	slugPrefix := flag.String("slug", "", "Polymarket slug 前缀（覆盖配置文件）")
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// ================================================================
	// 配置加载: 代码默认值 ← 文件配置 ← 环境变量 (优先级从低到高)
	// ================================================================
	cfg, err := loadConfig(*configPath)
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

	// ================================================================
	// Polymarket client (read-only if no owner key configured)
	// ================================================================
	readOnly := false
	if cfg.SDK.Polymarket.OwnerKey == "" {
		key := make([]byte, 32)
		if _, err := rand.Read(key); err != nil {
			log.Fatalf("Failed to generate temporary key: %v", err)
		}
		cfg.SDK.Polymarket.OwnerKey = hex.EncodeToString(key)
		readOnly = true
	}

	client := sdk.NewClient(&cfg.SDK)
	if readOnly {
		log.Println("[Flip] ⚠️  No POLYMARKET_OWNER_KEY — running READ-ONLY (paper trading)")
	}

	// ================================================================
	// Binance adapter
	// ================================================================
	binance := feed.NewBinanceAdapterWithConfig(cfg.Binance)
	go func() {
		if err := binance.Start(ctx); err != nil {
			log.Printf("[Flip] Binance start: %v", err)
		}
	}()

	// ================================================================
	// Polymarket order book adapter
	// ================================================================
	bookAdapter := feed.NewOrderBookAdapterWithResolve(
		cfg.SDK.Polymarket.ClobWSBaseURL, client,
	)
	bookAdapter.Start(ctx)

	// Book tracking
	var (
		yesTok string
		noTok  string
	)

	// ================================================================
	// Lab Collector (5s ResearchSnapshots)
	// ================================================================
	collector := lab.NewCollector(binance)

	// ================================================================
	// Flip Engine & Recorder
	// ================================================================
	flipCfg := cfg.Flip
	histTracker := flip.NewHistRangeTracker(flipCfg.HistWindowN)

	// Wait for initial Binance data before warming up hist range
	log.Println("[Flip] Waiting for initial Binance data...")
	select {
	case <-ctx.Done():
		return
	case <-time.After(3 * time.Second):
	}

	// Warmup: fetch historical 5m klines for instant hist_avg_range
	histErr := make(chan error, 1)
	go func() {
		histErr <- histTracker.Warmup(cfg.Binance.RestBaseURL, cfg.Binance.Symbol)
	}()
	select {
	case <-ctx.Done():
		log.Println("[Flip] hist warmup interrupted")
		return
	case err := <-histErr:
		if err != nil {
			log.Printf("[Flip] hist warmup failed: %v (F6/F7 will be degraded until enough cycles)", err)
		}
	}

	flipEngine := flip.NewEngine(flipCfg, histTracker)

	flipRecorder, err := flip.NewFlipRecorder(cfg.Runtime.OutputPath)
	if err != nil {
		log.Fatalf("[Flip] recorder: %v", err)
	}
	defer func() {
		if err := flipRecorder.Close(); err != nil {
			log.Printf("[Flip] recorder close: %v", err)
		}
	}()

	// Optional lab data writer (same format as cmd/lab)
	var labWriter *lab.Writer
	if cfg.Runtime.LabOutputDir != "" {
		labWriter, err = lab.NewWriter(cfg.Runtime.LabOutputDir)
		if err != nil {
			log.Fatalf("[Flip] lab writer: %v", err)
		}
		defer func() {
			if err := labWriter.Close(); err != nil {
				log.Printf("[Flip] lab writer close: %v", err)
			}
		}()
	}

	// Optional HTTP dashboard
	if cfg.Runtime.DashboardAddr != "" {
		mode := "live"
		if readOnly {
			mode = "paper"
		}
		dash := dashboard.New(collector, flipEngine, histTracker, flipRecorder, binance, cfg.Runtime.Symbol, mode)
		go dash.ListenAndServe(cfg.Runtime.DashboardAddr)
		log.Printf("[Flip] Dashboard: http://0.0.0.0%s (accessible from any network interface)", cfg.Runtime.DashboardAddr)
	}

	log.Println("========================================")
	log.Println(" Flip Signal Detection — Paper Trading")
	log.Printf(" Symbol: %s  |  Slug: %s  |  Output: %s",
		cfg.Runtime.Symbol, cfg.Runtime.SlugPrefix, cfg.Runtime.OutputPath)
	if cfg.Runtime.LabOutputDir != "" {
		log.Printf(" Lab data: %s (events JSONL)", cfg.Runtime.LabOutputDir)
	}
	log.Printf(" Config: trigger>%.1f confirm_delay=%dtick score_entry≥%d score_add≥%d",
		flipCfg.TriggerThreshold, flipCfg.ConfirmDelayTicks, flipCfg.ScoreEntry, flipCfg.ScoreAdd)
	log.Println(" Sources: [Binance aggTrade+depth20] + [Polymarket CLOB books]")
	log.Println("========================================")

	// ================================================================
	// Market Cycle Loop
	// ================================================================
	var generation int64

	for {
		select {
		case <-ctx.Done():
			log.Println("[Flip] Shutting down...")
			return
		default:
		}

		// Step 1: Calculate next 5-min aligned boundary
		now := time.Now()
		alignedTs := now.Unix() / lab.WindowSec * lab.WindowSec
		nextStart := time.Unix(alignedTs, 0)
		marketSlug := fmt.Sprintf("%s-%d", cfg.Runtime.SlugPrefix, nextStart.Unix())

		// Step 2: Wait until window start + 2s
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

		// Step 3: Fetch Binance kline open price
		binance.FetchKlineOpenPrice()

		btc := binance.LatestData()
		openPrice := btc.OpenPrice
		if openPrice == 0 {
			openPrice = btc.Price
			log.Printf("[Cycle] WARNING: kline open not available, using current price %.2f", openPrice)
		}

		// Step 4: Fetch Polymarket market → conditionId + token IDs
		log.Printf("[Cycle] fetching market: %s", marketSlug)
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
			log.Println("[Cycle] market fetch interrupted")
			return
		case mr := <-marketCh:
			marketData, fetchErr = mr.data, mr.err
		}
		if fetchErr != nil {
			log.Printf("[Cycle] ERROR fetching market: %v — retrying in 5s", fetchErr)
			select {
			case <-ctx.Done():
				return
			case <-time.After(5 * time.Second):
			}
			continue
		}

		conditionID := marketData.Get("conditionId").String()

		// Parse token IDs and outcomes
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

		// Step 5: Subscribe to new tokens (unsubscribe old ones first)
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

		// Step 6: Start event collection + flip engine
		collector.StartEvent(conditionID, nextStart.Unix(), openPrice)
		generation++
		flipEngine.Reset(generation)
		log.Printf("[Cycle] event=%s open=%.2f collecting...", conditionID, openPrice)

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
				// Read latest Polymarket book prices (atomic, no channel overhead)
				yb := bookAdapter.GetLatestBook(yesTok)
				nb := bookAdapter.GetLatestBook(noTok)

				collector.UpdatePolymarket(pmMidPrice(yb), pmMidPrice(nb))

				snap := collector.Tick(tickTime)
				if snap == nil {
					continue
				}
				snapCount++

				// ── Flip Signal Detection ──
				if sig := flipEngine.ProcessSnapshot(snap, generation); sig != nil {
					sig.ConditionID = conditionID
					if err := flipRecorder.RecordSignal(sig); err != nil {
						log.Printf("[Flip] record error: %v", err)
					}
					log.Printf("[Flip] 🎯 SIGNAL: %s>0.7 score=%d entry=%.3f shares=%d | "+
						"osc=%v path_eff=%.2f noise=%.1f flips=%d range_exp=%.1f btc_pos=%.2f other_d=%+.3f rem=%ds",
						sig.Side, sig.Score, sig.EntryPrice, sig.Shares,
						sig.IsOscillating, sig.PathEff, sig.NoiseRatio, sig.Flips,
						sig.RangeExpansion, sig.BTCPosition, sig.OtherDelta,
						sig.RemainingSec)
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

		// Step 7: Finalize event, persist lab data, update hist range, resolve
		event := collector.FinalizeEvent()
		histTracker.AddRange(event.OpenPrice, event.ClosePrice)

		// Persist full event snapshots if lab output is enabled
		if labWriter != nil {
			if err := labWriter.Write(event); err != nil {
				log.Printf("[Flip] lab write error: %v", err)
			}
		}

		outcomeLabel := "DOWN/FLAT"
		if event.Outcome == 0 {
			outcomeLabel = "UP"
		}
		log.Printf("[Event] %s done — open=%.2f close=%.2f outcome=%s snapshots=%d",
			conditionID, event.OpenPrice, event.ClosePrice, outcomeLabel, len(event.Snapshots))

		// Resolve: outcome 0=Up, 1=Down
		if err := flipRecorder.Resolve(event.ConditionID, event.Outcome); err != nil {
			log.Printf("[Flip] resolve error: %v", err)
		}

		// Unsubscribe old tokens
		bookAdapter.UnsubscribeTokens(tokenIDs...)
	}
}

// ── Helpers ──

// pmMidPrice returns the mid price (average of best bid and best ask)
// from a Polymarket CLOB order book.
//
// Both Bids and Asks are sorted by the API so that the LAST element is the
// best price: Bids[len-1] = highest bid, Asks[len-1] = lowest ask.
// Falls back to best bid only when asks are not yet available.
func pmMidPrice(book *sdk.OrderBook) float64 {
	if book == nil || len(book.Bids) == 0 {
		return 0
	}
	bestBid := book.Bids[len(book.Bids)-1].Price
	if len(book.Asks) == 0 {
		return bestBid // ask side not yet populated, fallback to bid
	}
	bestAsk := book.Asks[len(book.Asks)-1].Price
	return (bestBid + bestAsk) / 2
}

// ── Config ──

// RuntimeConfig holds runtime parameters that can be overridden by CLI flags.
type RuntimeConfig struct {
	Symbol        string `mapstructure:"symbol"`         // Binance trading pair
	SlugPrefix    string `mapstructure:"slug_prefix"`    // Polymarket slug prefix
	OutputPath    string `mapstructure:"output_path"`    // Flip signal JSONL output path
	LabOutputDir  string `mapstructure:"lab_output_dir"` // Optional lab event output directory
	DashboardAddr string `mapstructure:"dashboard_addr"` // HTTP dashboard listen address
}

// AppConfig is the top-level configuration structure matching config.yaml.
type AppConfig struct {
	Runtime RuntimeConfig      `mapstructure:"runtime"`
	SDK     sdk.Config         `mapstructure:"sdk"`
	Binance feed.BinanceConfig `mapstructure:"binance"`
	Flip    flip.FlipConfig    `mapstructure:"flip"`
}


// loadConfig loads configuration with viper precedence:
//
//	代码默认值 (最低) ← config.yaml ← 环境变量 POLYMARKET_* (最高)
//
// CLI flags are applied separately in main() after loadConfig returns.
func loadConfig(configPath string) (*AppConfig, error) {
	v := viper.New()

	// ── 环境变量绑定（在 ReadInConfig 之前，确保优先级：env > file > default）──

	// POLYMARKET_* → sdk.polymarket.* / sdk.*
	v.BindEnv("sdk.polymarket.owner_key", "POLYMARKET_OWNER_KEY")
	v.BindEnv("sdk.polymarket.clob_creds.key", "POLYMARKET_CLOB_KEY")
	v.BindEnv("sdk.polymarket.clob_creds.secret", "POLYMARKET_CLOB_SECRET")
	v.BindEnv("sdk.polymarket.clob_creds.passphrase", "POLYMARKET_CLOB_PASSPHRASE")
	v.BindEnv("sdk.polymarket.funder_address", "POLYMARKET_FUNDER")
	v.BindEnv("sdk.socks_proxy", "POLYMARKET_PROXY")

	// FLIP_* → runtime.*
	v.BindEnv("runtime.symbol", "FLIP_SYMBOL")
	v.BindEnv("runtime.slug_prefix", "FLIP_SLUG_PREFIX")
	v.BindEnv("runtime.output_path", "FLIP_OUTPUT")
	v.BindEnv("runtime.lab_output_dir", "FLIP_LAB_OUTPUT")
	v.BindEnv("runtime.dashboard_addr", "FLIP_DASHBOARD")
	v.BindEnv("binance.symbol", "FLIP_SYMBOL") // 同步 binance 交易对

	// ── Step 1: 代码默认值（最低优先级）──
	cfg := &AppConfig{
		Runtime: RuntimeConfig{
			Symbol:        "BTCUSDT",
			SlugPrefix:    "btc-updown-5m",
			OutputPath:    "data/flip_signals.jsonl",
			LabOutputDir:  "",
			DashboardAddr: "",
		},
		SDK:     *sdk.DefaultConfig(),
		Binance: feed.DefaultBinanceConfig(),
		Flip:    flip.DefaultConfig(),
	}
	// SDK DefaultConfig 设了 dummy OwnerKey，置空以触发 read-only 模式
	// （env POLYMARKET_OWNER_KEY 或配置文件可覆盖）
	cfg.SDK.Polymarket.OwnerKey = ""

	// ── Step 2: 文件配置（覆盖默认值，env 绑定的变量自动优先）──
	if configPath != "" {
		v.SetConfigFile(configPath)
	} else {
		v.SetConfigName("config")
		v.SetConfigType("yaml")
		v.AddConfigPath(".")
	}

	if err := v.ReadInConfig(); err != nil {
		if _, ok := err.(viper.ConfigFileNotFoundError); !ok {
			return nil, fmt.Errorf("read config file: %w", err)
		}
		log.Printf("[Config] 未找到配置文件 (%s)，使用代码默认值", configPath)
	} else {
		log.Printf("[Config] 已加载配置文件: %s", v.ConfigFileUsed())
	}

	// Unmarshal 自动处理优先级: BindEnv > config file > struct default
	// （只覆盖 viper 中存在对应值的字段）
	if err := v.Unmarshal(cfg); err != nil {
		return nil, fmt.Errorf("unmarshal config: %w", err)
	}

	return cfg, nil
}
