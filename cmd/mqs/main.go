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
	//   1. Determine next 5-min aligned window
	//   2. Sleep until window start + 2s (kline delay)
	//   3. Fetch Binance 5m kline open price
	//   4. Fetch Polymarket market by slug
	//   5. Subscribe to market token order books
	//   6. Reset snapshot collector (new RingBuffer, new endTime)
	//   7. Drain stale snapshots from channel
	//   8. Collect snapshots + compute MQS until market ends
	//   9. Unsubscribe tokens, increment generation, goto 1
	// ================================================================
	log.Println("========================================")
	log.Println("MQS Live — BTC 5-minute tail-trading")
	log.Println("Data: [Binance BTC/USDT] + [Polymarket order books]")
	log.Println("========================================")

	var generation int64 // incremented each cycle, used to filter stale snapshots

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
		// The current window is [alignedTs, alignedTs+300). We want the window
		// that starts at alignedTs (which may already be in progress).
		nextStart := time.Unix(alignedTs, 0)
		marketSlug := fmt.Sprintf("%s-%d", *slugPrefix, nextStart.Unix())
		marketEndTime := nextStart.Unix() + windowSec

		// --- Step 2: Sleep until window start + 2s (kline hasn't been generated yet at t=0) ---
		waitUntil := nextStart.Add(2 * time.Second)
		waitDur := time.Until(waitUntil)
		if waitDur > 0 {
			log.Printf("[Cycle] next window at %s, waiting %v (slug=%s)",
				nextStart.UTC().Format(time.RFC3339), waitDur.Round(time.Second), marketSlug)
			select {
			case <-ctx.Done():
				return
			case <-time.After(waitDur):
			}
		}

		// --- Step 3: Fetch kline open price ---
		log.Printf("[Cycle] fetching %s 5m kline...", *slugPrefix)
		binanceAdapter.FetchKlineOpenPrice()

		// --- Step 4: Fetch Polymarket market ---
		log.Printf("[Cycle] fetching market: %s", marketSlug)
		marketData, err := client.FetchMarketBySlug(marketSlug)
		if err != nil {
			log.Printf("[Cycle] ERROR: %v — retrying in 5s", err)
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

		log.Printf("[Cycle] market=%s tokens=%v ends=%s",
			marketID, tokenIDs,
			time.Unix(marketEndTime, 0).UTC().Format(time.RFC3339))

		// --- Step 5: Subscribe & reset ---
		bookAdapter.SubscribeTokens(tokenIDs...)
		generation++
		snapFeed.Reset(marketID, marketEndTime, generation)

		// --- Step 6: Drain stale snapshots from previous cycle ---
		drained := 0
	drainLoop:
		for {
			select {
			case snap := <-snapFeed.SnapshotCh:
				if snap.Generation >= generation {
					// Current-cycle data reached — stop draining
					break drainLoop
				}
				drained++
			default:
				break drainLoop
			}
		}
		if drained > 0 {
			log.Printf("[Cycle] drained %d stale snapshots", drained)
		}

		remaining := marketEndTime - time.Now().Unix()
		log.Printf("[Cycle] running — remaining=%ds", remaining)

		// --- Step 7: Collect snapshots until market ends ---
		var lastDecision decision.Decision
		var lastLogRemaining int
		for {
			select {
			case <-ctx.Done():
				return

			case snap := <-snapFeed.SnapshotCh:
				// Skip snapshots from previous generations
				if snap.Generation < generation {
					continue
				}
				if snap.RemainingSec <= 0 {
					log.Printf("[Cycle] market ended, transitioning...")
					goto nextCycle
				}

				// Compute MQS
				mq := engine.ComputeMQS(snapFeed.Collector().Buffer().Window(300))

				// Pre-tail: only periodic logs
				if snap.RemainingSec > 60 {
					if snap.RemainingSec%15 == 0 {
						log.Printf("[pre-tail] MQS=%.0f Rem=%ds", mq.Total, snap.RemainingSec)
					}
					continue
				}

				// Tail window — evaluate trading rules
				result := decision.Decide(mq, snap.RemainingSec)

				// Only log/record when decision CHANGES or every 10s
				changed := result.Decision != lastDecision || snap.RemainingSec-lastLogRemaining >= 10
				if changed {
					lastDecision = result.Decision
					lastLogRemaining = snap.RemainingSec

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
