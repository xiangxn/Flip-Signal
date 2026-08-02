package feed

import (
	"context"
	"log"

	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// OrderBookAdapter wraps MarketMonitor to provide order book streaming for specific tokens.
type OrderBookAdapter struct {
	monitor     *sdk.MarketMonitor
	orderBookCh chan *sdk.OrderBook
}

// NewOrderBookAdapter creates a new order book adapter.
// wsBaseURL: e.g. "wss://ws-subscriptions-clob.polymarket.com"
// customFeatureEnabled: set to true to receive resolution events via WebSocket
func NewOrderBookAdapter(wsBaseURL string, client *sdk.PolymarketClient, isStore bool) *OrderBookAdapter {
	return &OrderBookAdapter{
		monitor:     sdk.NewMarketMonitor(wsBaseURL, isStore, client, false),
		orderBookCh: make(chan *sdk.OrderBook, 4096),
	}
}

// NewOrderBookAdapterWithResolve is like NewOrderBookAdapter but with
// customFeatureEnabled=true, which enables market_resolved events on the WebSocket.
func NewOrderBookAdapterWithResolve(wsBaseURL string, client *sdk.PolymarketClient, isStore bool) *OrderBookAdapter {
	return &OrderBookAdapter{
		monitor:     sdk.NewMarketMonitor(wsBaseURL, isStore, client, true),
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

// Start begins streaming order book data.
func (o *OrderBookAdapter) Start(ctx context.Context) {
	// Relay the monitor's channel to our channel
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
				select {
				case o.orderBookCh <- book:
				default:
					// Drop oldest
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

// OrderBook returns the channel for latest order book updates.
func (o *OrderBookAdapter) OrderBook() <-chan *sdk.OrderBook {
	return o.orderBookCh
}

// LatestOrderBook returns the most recent order book without blocking.
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
