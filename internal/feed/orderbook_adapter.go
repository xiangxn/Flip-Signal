package feed

import (
	"context"
	"log"

	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// OrderBookAdapter wraps MarketMonitor to provide order book streaming for specific tokens.
type OrderBookAdapter struct {
	monitor     *sdk.MarketMonitor
	orderBookCh chan *sdk.OrderBook // kept for backward compat; prefer GetLatestBook()
}

// NewOrderBookAdapter creates a new order book adapter.
// wsBaseURL: e.g. "wss://ws-subscriptions-clob.polymarket.com"
// customFeatureEnabled: set to true to receive resolution events via WebSocket
//
// NOTE: isStore parameter is deprecated and ignored — the adapter always enables
// the SDK's internal atomic storage (isStore=true) so GetLatestBook works.
// Streaming via OrderBook() still works for backward compatibility.
func NewOrderBookAdapter(wsBaseURL string, client *sdk.PolymarketClient, isStore bool) *OrderBookAdapter {
	return &OrderBookAdapter{
		monitor:     sdk.NewMarketMonitor(wsBaseURL, true, client, false),
		orderBookCh: make(chan *sdk.OrderBook, 4096),
	}
}

// NewOrderBookAdapterWithResolve is like NewOrderBookAdapter but with
// customFeatureEnabled=true, which enables market_resolved events on the WebSocket.
func NewOrderBookAdapterWithResolve(wsBaseURL string, client *sdk.PolymarketClient, isStore bool) *OrderBookAdapter {
	return &OrderBookAdapter{
		monitor:     sdk.NewMarketMonitor(wsBaseURL, true, client, true),
		orderBookCh: make(chan *sdk.OrderBook, 4096),
	}
}

// SubscribeTokens subscribes to order book updates for the given token IDs.
func (o *OrderBookAdapter) SubscribeTokens(tokens ...string) {
	o.monitor.SubscribeTokens(tokens...)
}

// UnsubscribeTokens unsubscribes from order book updates for the given token IDs.
func (o *OrderBookAdapter) UnsubscribeTokens(tokens ...string) {
	o.monitor.UnsubscribeTokens(tokens...)
}

// SubscribeResolved returns a channel that receives market resolution events.
func (o *OrderBookAdapter) SubscribeResolved() <-chan *sdk.ResolvedInfo {
	return o.monitor.SubscribeResolved()
}

// GetLatestBook returns the most recent order book for a token ID from the
// SDK's internal atomic store. This is O(1) and avoids the streaming channel
// pipeline entirely — use this instead of the OrderBook() channel when you
// only need the latest state (e.g. periodic snapshots).
//
// Returns nil if no book has been received for this token yet.
func (o *OrderBookAdapter) GetLatestBook(tokenID string) *sdk.OrderBook {
	book, err := o.monitor.GetTokenOrderBook(tokenID)
	if err != nil {
		return nil
	}
	return book
}

// Start begins streaming order book data.
func (o *OrderBookAdapter) Start(ctx context.Context) {
	// Drain goroutine: pull from the monitor's channel to prevent backpressure
	// in the SDK's emitOrderBook → orderBookCh path. The latest book is stored
	// atomically via isStore=true and accessible via GetLatestBook().
	// We also forward to our own channel for backward compatibility.
	go func() {
		ch := o.monitor.SubscribeOrderBook()
		for {
			select {
			case <-ctx.Done():
				return
			case book, ok := <-ch:
				if !ok {
					return
				}
				// Forward to our channel (backward compat, non-blocking)
				select {
				case o.orderBookCh <- book:
				default:
					select {
					case <-o.orderBookCh:
					default:
					}
					select {
					case o.orderBookCh <- book:
					default:
					}
				}
			}
		}
	}()

	go func() {
		if err := o.monitor.Run(ctx); err != nil {
			log.Printf("[OrderBookAdapter] monitor stopped: %v", err)
		}
	}()
}

// OrderBook returns the channel for order book updates.
// Prefer GetLatestBook() for periodic reads — it avoids channel overhead.
func (o *OrderBookAdapter) OrderBook() <-chan *sdk.OrderBook {
	return o.orderBookCh
}

// LatestOrderBook returns the most recent order book without blocking.
// Prefer GetLatestBook(tokenID) for token-specific atomic reads.
func (o *OrderBookAdapter) LatestOrderBook() *sdk.OrderBook {
	select {
	case book := <-o.orderBookCh:
		// Put it back
		select {
		case o.orderBookCh <- book:
		default:
		}
		return book
	default:
		return nil
	}
}
