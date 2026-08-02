package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/tidwall/gjson"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

func main() {
	now := time.Now()
	alignedTs := now.Unix()/300*300 + 300
	slug := fmt.Sprintf("btc-updown-5m-%d", alignedTs)
	waitSec := alignedTs - now.Unix()

	log.Printf("Next market: %s (starts in %ds)", slug, waitSec)

	cfg := sdk.Config{Polymarket: sdk.PolymarketConfig{
		ChainID: 137, ClobBaseURL: "https://clob.polymarket.com",
		ClobWSBaseURL: "wss://ws-subscriptions-clob.polymarket.com",
		GammaBaseURL: "https://gamma-api.polymarket.com",
	}}
	if v := os.Getenv("https_proxy"); v != "" {
		cfg.SocksProxy = v
	}
	if v := os.Getenv("POLYMARKET_OWNER_KEY"); v != "" {
		cfg.Polymarket.OwnerKey = v
	} else {
		key := make([]byte, 32)
		rand.Read(key)
		cfg.Polymarket.OwnerKey = hex.EncodeToString(key)
		log.Println("Using temp key (read-only)")
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	client := sdk.NewClient(&cfg)

	if waitSec > 60 {
		until := time.Unix(alignedTs-30, 0)
		log.Printf("Sleeping until %s...", until.UTC().Format(time.RFC3339))
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Until(until)):
		}
	}

	log.Printf("Fetching market: %s", slug)
	data, err := client.FetchMarketBySlug(slug)
	if err != nil {
		log.Fatalf("Fetch error: %v", err)
	}

	marketID := data.Get("id").String()
	endDate := data.Get("endDate").String()
	endTime, _ := time.Parse(time.RFC3339, endDate)

	clobRaw := data.Get("clobTokenIds").String()
	var tokenIDs []string
	for _, v := range gjson.Parse(clobRaw).Array() {
		tokenIDs = append(tokenIDs, v.String())
	}
	outcomesRaw := data.Get("outcomes").String()
	var outcomes []string
	for _, v := range gjson.Parse(outcomesRaw).Array() {
		outcomes = append(outcomes, v.String())
	}

	log.Printf("Market: %s, ends: %s (in %v)", marketID, endDate, time.Until(endTime).Round(time.Second))
	for i, tid := range tokenIDs {
		label := ""
		if i < len(outcomes) {
			label = outcomes[i]
		}
		log.Printf("  Token %d: %s (%s)", i, tid, label)
	}

	monitor := sdk.NewMarketMonitor(cfg.Polymarket.ClobWSBaseURL, false, client, true)
	bookCh := monitor.SubscribeOrderBook()
	resolvedCh := monitor.SubscribeResolved()

	go func() {
		if err := monitor.Run(ctx); err != nil {
			log.Printf("Monitor stopped: %v", err)
		}
	}()

	time.Sleep(3 * time.Second)
	monitor.SubscribeTokens(tokenIDs...)
	log.Println("Subscribed — customFeatureEnabled=true")
	log.Println("=== Listening — Ctrl+C to stop ===")

	bookCount, resolvedCount := 0, 0
	pollTicker := time.NewTicker(15 * time.Second)
	statusTicker := time.NewTicker(30 * time.Second)
	defer pollTicker.Stop()
	defer statusTicker.Stop()

	for {
		select {
		case <-ctx.Done():
			log.Printf("Done. Books=%d WS-Resolutions=%d", bookCount, resolvedCount)
			return

		case book := <-bookCh:
			if bookCount == 0 {
				log.Printf("FIRST BOOK: token=%s bids=%d asks=%d latency=%dms",
					book.AssetId[:20]+"...", len(book.Bids), len(book.Asks), book.Latency)
			}
			bookCount++

		case info := <-resolvedCh:
			resolvedCount++
			log.Printf("🎯 WS RESOLUTION! market=%s winner=%s asset=%s time=%d",
				info.Market, info.WinningOutcome, info.WinningAssetId, info.Timestamp)

		case <-pollTicker.C:
			if time.Now().Before(endTime) {
				continue
			}
			data, err := client.FetchMarketBySlug(slug)
			if err != nil {
				log.Printf("REST error: %v", err)
				continue
			}
			log.Printf("REST: closed=%v prices=%s", data.Get("closed").Bool(), data.Get("outcomePrices").String())

		case <-statusTicker.C:
			rem := time.Until(endTime).Round(time.Second)
			suffix := ""
			if rem < 0 {
				suffix = fmt.Sprintf(" (ended %v ago)", -rem)
			}
			log.Printf("books=%d ws-res=%d rem=%v%s", bookCount, resolvedCount, rem, suffix)
		}
	}
}
