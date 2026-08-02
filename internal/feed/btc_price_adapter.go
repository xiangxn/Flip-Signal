package feed

import (
	"context"
	"log"

	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// BTCPriceAdapter wraps CryptoPriceMonitor to provide a simple BTC price channel.
// Uses Binance as the data source (more stable than Chainlink).
type BTCPriceAdapter struct {
	monitor *sdk.CryptoPriceMonitor
	priceCh chan float64
}

// NewBTCPriceAdapter creates a new BTC price adapter using Binance.
func NewBTCPriceAdapter(client *sdk.PolymarketClient) *BTCPriceAdapter {
	return &BTCPriceAdapter{
		monitor: sdk.NewCryptoPriceMonitor(client, sdk.MonitorBinance, "BTC"),
		priceCh: make(chan float64, 1024),
	}
}

// Start begins streaming BTC prices.
func (b *BTCPriceAdapter) Start(ctx context.Context) {
	ch := b.monitor.Subscribe()

	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case price, ok := <-ch:
				if !ok {
					return
				}
				select {
				case b.priceCh <- price.Price:
				default:
					// Drop oldest if full — always keep latest
					select {
					case <-b.priceCh:
					default:
					}
					select {
					case b.priceCh <- price.Price:
					default:
					}
				}
			}
		}
	}()

	go func() {
		if err := b.monitor.Run(ctx); err != nil {
			log.Printf("[BTCPriceAdapter] monitor stopped: %v", err)
		}
	}()
}

// Price returns the channel for latest BTC price.
func (b *BTCPriceAdapter) Price() <-chan float64 {
	return b.priceCh
}

// LatestPrice returns the most recent BTC price without blocking.
// Returns 0 if no price available.
func (b *BTCPriceAdapter) LatestPrice() float64 {
	select {
	case p := <-b.priceCh:
		// Put it back — we just peeked
		select {
		case b.priceCh <- p:
		default:
		}
		return p
	default:
		return 0
	}
}
