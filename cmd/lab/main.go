// Command lab runs the Feature Research Lab data collector.
//
// It connects to Binance WebSocket (aggTrade + depth20) and Polymarket
// CLOB WebSocket (order books), generates 1-second pure-fact
// ResearchSnapshots, groups them into 5-minute Events identified by
// Polymarket conditionId, and writes events as JSONL to disk.
//
// Usage:
//
//	go run ./cmd/lab -output data/lab -symbol BTCUSDT
//
// Read-only by default — generates a temporary wallet key for
// Polymarket API access (data reading only, no trading).
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
	"github.com/xiangxn/go-polymarket-sdk/model"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"

	"github.com/necklace/flip-signal/internal/feed"
	"github.com/necklace/flip-signal/internal/lab"
)

func init() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
}

func main() {
	outputDir := flag.String("output", "data/lab", "Output directory for event JSONL files")
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
		log.Println("[Lab] ⚠️  No POLYMARKET_OWNER_KEY — running READ-ONLY")
	}

	// ================================================================
	// Binance adapter
	// ================================================================
	binanceCfg := feed.BinanceConfig{
		Symbol: *symbol,
		// StreamBaseURL: "wss://stream.binance.com:9443",
		StreamBaseURL: "wss://data-stream.binance.vision",
		RestBaseURL:   "https://data-api.binance.vision",
	}
	binance := feed.NewBinanceAdapterWithConfig(binanceCfg)
	go func() {
		if err := binance.Start(ctx); err != nil {
			log.Printf("[Lab] Binance start: %v", err)
		}
	}()

	// ================================================================
	// Polymarket order book adapter
	// ================================================================
	bookAdapter := feed.NewOrderBookAdapter(
		cfg.SDK.Polymarket.ClobWSBaseURL, client,
	)
	bookAdapter.Start(ctx)

	// Book tracking: goroutine updates, tick loop reads under RLock.
	var (
		bookMu  sync.RWMutex
		yesBook *sdk.OrderBook
		noBook  *sdk.OrderBook
		yesTok  string
		noTok   string
	)

	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case book := <-bookAdapter.OrderBook():
				if book == nil || len(book.Bids) == 0 || len(book.Asks) == 0 {
					continue
				}
				bookMu.Lock()
				switch book.AssetId {
				case yesTok:
					yesBook = book
				case noTok:
					noBook = book
				default:
					log.Printf("[Book] UNMATCHED token=%s (want YES=%s NO=%s)",
						book.AssetId, yesTok, noTok)
				}
				bookMu.Unlock()
			}
		}
	}()

	// ================================================================
	// Collector & Writer
	// ================================================================
	collector := lab.NewCollector(binance)
	writer, err := lab.NewWriter(*outputDir)
	if err != nil {
		log.Fatalf("[Lab] writer: %v", err)
	}
	defer func() {
		if err := writer.Close(); err != nil {
			log.Printf("[Lab] writer close: %v", err)
		}
	}()

	log.Println("========================================")
	log.Println(" Feature Research Lab — BTC 5-min data")
	log.Printf(" Symbol: %s  |  Slug: %s  |  Output: %s", *symbol, *slugPrefix, *outputDir)
	log.Println(" Sources: [Binance aggTrade+depth20] + [Polymarket CLOB books]")
	log.Println("========================================")

	// Wait for initial data
	log.Println("[Lab] Waiting for initial data...")
	time.Sleep(3 * time.Second)

	// ================================================================
	// Market Cycle Loop
	// ================================================================
	for {
		select {
		case <-ctx.Done():
			log.Println("[Lab] Shutting down...")
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
		marketData, err := client.FetchMarketBySlug(marketSlug)
		if err != nil {
			log.Printf("[Cycle] ERROR fetching market: %v — retrying in 5s", err)
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

		// Step 5: Subscribe to new tokens (unsub old ones first)
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

		bookMu.Lock()
		yesTok = yesTokenID
		noTok = noTokenID
		yesBook = nil
		noBook = nil
		bookMu.Unlock()

		// Step 6: Start event collection
		collector.StartEvent(conditionID, nextStart.Unix(), openPrice)
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
				// Read latest Polymarket book prices
				bookMu.RLock()
				yb := yesBook
				nb := noBook
				bookMu.RUnlock()

				collector.UpdatePolymarket(bestBid(yb), bestBid(nb))

				snap := collector.Tick(tickTime)
				if snap == nil {
					continue
				}
				snapCount++

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

		// Step 7: Finalize and persist
		event := collector.FinalizeEvent()
		outcomeLabel := "DOWN/Flat"
		if event.Outcome == 0 {
			outcomeLabel = "UP"
		}
		log.Printf("[Event] %s done — open=%.2f close=%.2f outcome=%s snapshots=%d",
			conditionID, event.OpenPrice, event.ClosePrice, outcomeLabel, len(event.Snapshots))

		if err := writer.Write(event); err != nil {
			log.Printf("[Writer] ERROR: %v", err)
		}
	}
}

// ---- helpers ----

func bestBid(book *sdk.OrderBook) float64 {
	if book == nil || len(book.Bids) == 0 {
		return 0
	}
	// Polymarket CLOB: bids sorted ascending, best (highest) is last
	return book.Bids[len(book.Bids)-1].Price
}

// ---- config ----

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
	// Environment variable overrides (same as cmd/mqs)
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
