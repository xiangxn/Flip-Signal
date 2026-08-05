package feed

import (
	"context"
	"log"
	"sync"
	"time"

	"github.com/necklace/lasttrading/internal/mqs"
	"github.com/necklace/lasttrading/internal/snapshot"

	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// SnapshotFeed combines Binance BTC data + Polymarket YES/NO data
// into 1-second snapshots, computes MQS, and publishes results.
type SnapshotFeed struct {
	mu        sync.Mutex
	collector *snapshot.Collector
	binance   *BinanceAdapter
	bookAdapter *OrderBookAdapter
	engine    *mqs.Engine

	// Token tracking: which token ID maps to which outcome
	yesTokenID string
	noTokenID  string

	SnapshotCh chan *snapshot.Snapshot
	MQSCh      chan mqs.MarketQuality
}

// NewSnapshotFeed creates a new snapshot feed.
func NewSnapshotFeed(
	marketID string,
	binance *BinanceAdapter,
	bookAdapter *OrderBookAdapter,
) *SnapshotFeed {
	return &SnapshotFeed{
		collector:   snapshot.NewCollector(marketID, 0, 300),
		binance:     binance,
		bookAdapter: bookAdapter,
		engine:      mqs.NewEngine(),
		SnapshotCh:  make(chan *snapshot.Snapshot, 1024),
		MQSCh:       make(chan mqs.MarketQuality, 1024),
	}
}

// SetTokens configures which token IDs map to YES/NO outcomes.
func (f *SnapshotFeed) SetTokens(yesTokenID, noTokenID string) {
	f.yesTokenID = yesTokenID
	f.noTokenID = noTokenID
}

// Reset re-initializes the feed for a new market cycle.
func (f *SnapshotFeed) Reset(marketID string, endTime int64, generation int64) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.collector = snapshot.NewCollector(marketID, 0, 300)
	f.collector.SetMarketEndTime(endTime)
	f.collector.SetGeneration(generation)
}

// Collector returns the internal snapshot collector.
func (f *SnapshotFeed) Collector() *snapshot.Collector {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.collector
}

// Start begins the 1-second snapshot collection loop.
func (f *SnapshotFeed) Start(ctx context.Context) {
	go f.binance.Start(ctx)
	f.bookAdapter.Start(ctx)

	ticker := time.NewTicker(1 * time.Second)
	defer ticker.Stop()

	log.Printf("[SnapshotFeed] started")

	for {
		select {
		case <-ctx.Done():
			return

		case now := <-ticker.C:
			btc := f.binance.LatestData()
			if btc.Price == 0 {
				continue
			}

			f.mu.Lock()
			collector := f.collector

			if btc.OpenPrice > 0 {
				collector.SetOpenPrice(btc.OpenPrice)
			}

			collector.UpdateBTC(btc.Price,
				btc.BidDepth5, btc.AskDepth5,
				btc.BidDepth10, btc.AskDepth10)

			buy1s, sell1s, buy10s, sell10s := f.binance.ConsumeVolume()
			collector.UpdateVolume(buy1s, sell1s, buy10s, sell10s)

			// Read latest Polymarket book prices (atomic, no channel overhead)
			yesBook := f.bookAdapter.GetLatestBook(f.yesTokenID)
			noBook := f.bookAdapter.GetLatestBook(f.noTokenID)

			var yesPrice, noPrice float64
			if yesBook != nil {
				yesPrice = midPrice(yesBook)
			}
			if noBook != nil {
				noPrice = midPrice(noBook)
			}
			collector.UpdatePolymarket(yesPrice, noPrice)

			snap := collector.Tick(now)
			mq := f.engine.ComputeMQS(collector.Buffer().Window(300))
			f.mu.Unlock()

			select {
			case f.SnapshotCh <- snap:
			default:
			}
			select {
			case f.MQSCh <- mq:
			default:
			}
		}
	}
}

// midPrice returns the mid price from an order book's best bid/ask.
func midPrice(book *sdk.OrderBook) float64 {
	if book == nil || len(book.Bids) == 0 || len(book.Asks) == 0 {
		return 0
	}
	return (book.Bids[0].Price + book.Asks[0].Price) / 2
}
