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

	"github.com/tidwall/gjson"
	"github.com/xiangxn/go-polymarket-sdk/model"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"

	"github.com/necklace/lasttrading/internal/dashboard"
	"github.com/necklace/lasttrading/internal/feed"
	"github.com/necklace/lasttrading/internal/flip"
	"github.com/necklace/lasttrading/internal/lab"
)

func init() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
}

func main() {
	outputPath := flag.String("output", "data/flip_signals.jsonl", "Output JSONL path for flip signals")
	labOutputDir := flag.String("lab-output", "", "Optional lab event output directory (JSONL, same format as cmd/lab)")
	dashboardAddr := flag.String("dashboard", "", "HTTP dashboard address (e.g. :8090)")
	symbol := flag.String("symbol", "BTCUSDT", "Binance trading pair")
	slugPrefix := flag.String("slug", "btc-updown-5m", "Polymarket slug prefix")
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// ================================================================
	// Polymarket client (read-only if no owner key configured)
	// ================================================================
	cfg := loadConfig()
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
	binanceCfg := feed.BinanceConfig{
		Symbol:        *symbol,
		StreamBaseURL: "wss://data-stream.binance.vision",
		RestBaseURL:   "https://data-api.binance.vision",
	}
	binance := feed.NewBinanceAdapterWithConfig(binanceCfg)
	go func() {
		if err := binance.Start(ctx); err != nil {
			log.Printf("[Flip] Binance start: %v", err)
		}
	}()

	// ================================================================
	// Polymarket order book adapter
	// ================================================================
	bookAdapter := feed.NewOrderBookAdapterWithResolve(
		cfg.SDK.Polymarket.ClobWSBaseURL, client, false,
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
	flipCfg := flip.DefaultConfig()
	histTracker := flip.NewHistRangeTracker(flipCfg.HistWindowN) // 18

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
		histErr <- histTracker.Warmup(binanceCfg.RestBaseURL, *symbol)
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

	flipRecorder, err := flip.NewFlipRecorder(*outputPath)
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
	if *labOutputDir != "" {
		labWriter, err = lab.NewWriter(*labOutputDir)
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
	if *dashboardAddr != "" {
		dash := dashboard.New(collector, flipEngine, histTracker, flipRecorder, binance, *symbol)
		go dash.ListenAndServe(*dashboardAddr)
		log.Printf("[Flip] Dashboard: http://localhost%s", *dashboardAddr)
	}

	log.Println("========================================")
	log.Println(" Flip Signal Detection — Paper Trading")
	log.Printf(" Symbol: %s  |  Slug: %s  |  Output: %s", *symbol, *slugPrefix, *outputPath)
	if *labOutputDir != "" {
		log.Printf(" Lab data: %s (events JSONL)", *labOutputDir)
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
		marketSlug := fmt.Sprintf("%s-%d", *slugPrefix, nextStart.Unix())

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

				collector.UpdatePolymarket(bestBid(yb), bestBid(nb))

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

// bestBid returns the best (highest) bid price from a Polymarket order book.
// Polymarket CLOB bids are sorted ascending; the last bid is the highest.
func bestBid(book *sdk.OrderBook) float64 {
	if book == nil || len(book.Bids) == 0 {
		return 0
	}
	return book.Bids[len(book.Bids)-1].Price
}

// ── Config ──

type appConfig struct {
	SDK sdk.Config `mapstructure:"sdk"`
}

func loadConfig() *appConfig {
	cfg := &appConfig{
		SDK: sdk.Config{
			Polymarket: sdk.PolymarketConfig{
				ChainID:        137,
				ClobBaseURL:    "https://clob.polymarket.com",
				ClobWSBaseURL:  "wss://ws-subscriptions-clob.polymarket.com",
				GammaBaseURL:   "https://gamma-api.polymarket.com",
				DataAPIBaseURL: "https://data-api.polymarket.com",
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
