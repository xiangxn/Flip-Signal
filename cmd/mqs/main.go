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

	"github.com/necklace/lasttrading/internal/decision"
	"github.com/necklace/lasttrading/internal/feed"
	"github.com/necklace/lasttrading/internal/mqs"

	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
	"github.com/xiangxn/go-polymarket-sdk/model"
)

const windowSec = 5 * 60 // 5 minutes

func init() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
}

func main() {
	configPath := flag.String("config", "config.yaml", "Path to config file")
	slugPrefix := flag.String("slug", "btc-updown-5m", "Polymarket slug prefix")
	flag.Parse()

	cfg, err := LoadConfig(*configPath)
	if err != nil {
		log.Fatalf("Failed to load config: %v", err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// ---- Polymarket client ----
	readOnly := false
	if cfg.SDK.Polymarket.OwnerKey == "" {
		key := make([]byte, 32)
		if _, err := rand.Read(key); err != nil {
			log.Fatalf("Failed to generate temporary key: %v", err)
		}
		cfg.SDK.Polymarket.OwnerKey = hex.EncodeToString(key)
		readOnly = true
		log.Println("⚠️  No POLYMARKET_OWNER_KEY — running READ-ONLY (monitoring only)")
	}

	client := sdk.NewClient(&cfg.SDK)
	if readOnly {
		log.Println("🔒 Read-only mode: scoring + logging, no order placement")
	}

	// ---- Binance adapter (runs continuously) ----
	log.Println("Connecting to Binance WS...")
	binanceAdapter := feed.NewBinanceAdapter()
	go func() {
		if err := binanceAdapter.Start(ctx); err != nil {
			log.Printf("[WARN] Binance: %v", err)
		}
	}()

	// ---- Polymarket order book adapter ----
	bookAdapter := feed.NewOrderBookAdapter(cfg.SDK.Polymarket.ClobWSBaseURL, client, false)

	// ---- Snapshot feed ----
	snapFeed := feed.NewSnapshotFeed("", binanceAdapter, bookAdapter)
	go snapFeed.Start(ctx)

	// ---- Recorder ----
	recorder, err := decision.NewRecorder(cfg.Logging.DecisionLogPath)
	if err != nil {
		log.Printf("Warning: recorder: %v", err)
	}

	engine := mqs.NewEngine()

	// ================================================================
	// Market Cycle Loop
	// Polymarket runs 5-minute markets continuously. Each cycle:
	//   1. Wait for next 5-min aligned window + 2s (kline delay)
	//   2. Fetch Binance 5m kline open price
	//   3. Fetch Polymarket market by slug
	//   4. Subscribe to market tokens
	//   5. Collect snapshots + compute MQS until market ends
	//   6. Unsubscribe old tokens, go to 1
	// ================================================================
	log.Println("========================================")
	log.Println("MQS Live — BTC 5-minute tail-trading")
	log.Println("Data: [Binance BTC/USDT] + [Polymarket order books]")
	log.Println("========================================")

	for {
		select {
		case <-ctx.Done():
			log.Println("Shutting down...")
			if recorder != nil {
				_ = recorder.Close()
			}
			return
		default:
		}

		// --- Step 1: Determine next 5-min aligned window ---
		now := time.Now()
		alignedTs := now.Unix() / windowSec * windowSec
		nextStart := time.Unix(alignedTs+windowSec, 0) // start of NEXT window

		// If we're within 2s of the current window starting, use current window
		if now.Unix()-alignedTs < 2 {
			nextStart = time.Unix(alignedTs, 0)
		}

		marketSlug := fmt.Sprintf("%s-%d", *slugPrefix, nextStart.Unix())
		marketEndTime := nextStart.Unix() + windowSec

		// --- Step 2: Wait until window start + 2s (ensure kline exists) ---
		waitUntil := nextStart.Add(2 * time.Second)
		waitDur := time.Until(waitUntil)
		if waitDur > 0 {
			log.Printf("[Cycle] waiting %v for next window (slug=%s)", waitDur.Round(time.Second), marketSlug)
			select {
			case <-ctx.Done():
				return
			case <-time.After(waitDur):
			}
		}

		// --- Step 3: Fetch kline open price ---
		log.Printf("[Cycle] fetching kline open price...")
		binanceAdapter.FetchKlineOpenPrice()

		// --- Step 4: Fetch Polymarket market ---
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

		marketID := marketData.Get("id").String()
		tokens := marketData.Get("tokens").Array()
		var tokenIDs []string
		for _, t := range tokens {
			tokenIDs = append(tokenIDs, t.Get("token_id").String())
		}

		log.Printf("[Cycle] market=%s tokens=%v ends=%s", marketID, tokenIDs,
			time.Unix(marketEndTime, 0).UTC().Format(time.RFC3339))

		// --- Step 5: Subscribe & reset ---
		bookAdapter.SubscribeTokens(tokenIDs...)
		snapFeed.Reset(marketID, marketEndTime)

		remaining := marketEndTime - time.Now().Unix()
		log.Printf("[Cycle] running — remaining=%ds (waiting for tail window at 60s)", remaining)

		// --- Step 6: Collect snapshots until market ends ---
		for {
			select {
			case <-ctx.Done():
				return

			case snap := <-snapFeed.SnapshotCh:
				// Only process snapshots for current market
				if snap.RemainingSec <= 0 {
					log.Printf("[Cycle] market ended, transitioning...")
					goto nextCycle
				}

				// Compute MQS
				mq := engine.ComputeMQS(snapFeed.Collector().Buffer().Window(300))

				// Only evaluate in tail window (last 60s)
				if snap.RemainingSec > 60 {
					// Pre-tail: log MQS periodically (every 15s)
					if snap.RemainingSec%15 == 0 {
						log.Printf("[pre-tail] MQS=%.0f Rem=%ds", mq.Total, snap.RemainingSec)
					}
					continue
				}

				// Tail window — evaluate trading rules
				result := decision.Decide(mq, snap.RemainingSec)

				status := "🟢"
				switch result.Decision {
				case decision.DecisionForbidden:
					status = "🔴"
				case decision.DecisionNoTrade:
					status = "🟡"
				case decision.DecisionStrong:
					status = "🔥"
				}

				log.Printf("[%s] MQS=%.0f | T=%.0f N=%.0f H=%.0f F=%.0f L=%.0f | Rem=%ds | %s",
					status, mq.Total, mq.TrendScore, mq.NoiseScore, mq.HealthScore,
					mq.FlowScore, mq.LiquidityScore,
					snap.RemainingSec, result.Decision.String())

				if recorder != nil && result.Decision != decision.DecisionNoTrade {
					_ = recorder.Record(mq, snap.RemainingSec, snap.ReturnFromOpen, result.Decision, "")
				}
			}
		}
	nextCycle:
		// Unsubscribe old tokens before next cycle
		bookAdapter.UnsubscribeTokens(tokenIDs...)
	}
}

// ---- Config ----

type Config struct {
	SDK     sdk.Config    `mapstructure:"sdk"`
	Logging LoggingConfig `mapstructure:"logging"`
}

type LoggingConfig struct {
	DecisionLogPath string `mapstructure:"decision_log_path"`
	Level           string `mapstructure:"level"`
}

func DefaultConfig() *Config {
	return &Config{
		SDK: sdk.Config{
			Polymarket: sdk.PolymarketConfig{
				ChainID:        137,
				ClobBaseURL:    "https://clob.polymarket.com",
				ClobWSBaseURL:  "wss://ws-subscriptions-clob.polymarket.com",
				GammaBaseURL:   "https://gamma-api.polymarket.com",
				DataAPIBaseURL: "https://data-api.polymarket.com",
			},
		},
		Logging: LoggingConfig{
			DecisionLogPath: "data/mqs_decisions.jsonl",
			Level:           "info",
		},
	}
}

func LoadConfig(path string) (*Config, error) {
	cfg := DefaultConfig()
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
	return cfg, nil
}
