package feed

import (
	"context"
	"log"
	"sync"
	"time"

	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// OrderBookAdapter wraps MarketMonitor to provide order book streaming for specific tokens.
type OrderBookAdapter struct {
	monitor     *sdk.MarketMonitor
	orderBookCh chan *sdk.OrderBook // kept for backward compat; prefer GetLatestBook()

	// 当前订阅 token 的本地副本：SDK 的 MarketMonitor 在 Run 退出时会把内部
	// subsTokens 清空（Disconnect），保留副本用于 monitor 重启后恢复订阅。
	mu     sync.RWMutex
	tokens []string
}

// NewOrderBookAdapter creates a new order book adapter.
// wsBaseURL: e.g. "wss://ws-subscriptions-clob.polymarket.com"
//
// Always enables the SDK's internal atomic storage so GetLatestBook works.
func NewOrderBookAdapter(wsBaseURL string, client *sdk.PolymarketClient) *OrderBookAdapter {
	return &OrderBookAdapter{
		monitor:     sdk.NewMarketMonitor(wsBaseURL, true, client, false),
		orderBookCh: make(chan *sdk.OrderBook, 4096),
	}
}

// SubscribeTokens subscribes to order book updates for the given token IDs.
func (o *OrderBookAdapter) SubscribeTokens(tokens ...string) {
	o.mu.Lock()
	o.tokens = addTokens(o.tokens, tokens...)
	o.mu.Unlock()
	o.monitor.SubscribeTokens(tokens...)
}

// UnsubscribeTokens unsubscribes from order book updates for the given token IDs.
func (o *OrderBookAdapter) UnsubscribeTokens(tokens ...string) {
	o.mu.Lock()
	o.tokens = removeTokens(o.tokens, tokens...)
	o.mu.Unlock()
	o.monitor.UnsubscribeTokens(tokens...)
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
		for {
			err := o.monitor.Run(ctx)
			// ⚠️ ctx 未取消时无条件重启（含 Run 干净退出 err==nil 的场景），
			// 否则数据流会永久死亡 —— 与 cmd/collect 的重启模式一致
			// （2026-08-18 review 在 collect 修复的同款问题）。
			if ctx.Err() != nil {
				return
			}
			if err != nil {
				// Run 退出意味着 WS 连续连接失败耗尽重试次数（或内部异常）：
				// SDK 已 Disconnect 并清空 subsTokens，不重启的话后续窗口
				// 将永远收不到盘口数据。
				log.Printf("[OrderBookAdapter] ⚠️ monitor 异常退出: %v —— 5 秒后重启", err)
			} else {
				log.Printf("[OrderBookAdapter] ⚠️ monitor 干净退出 —— 5 秒后重启")
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(5 * time.Second):
			}
			// 先恢复本地订阅副本（此时 ws 未连接，仅写入 SDK 内部状态），
			// Run 连接成功后由 OnOpen → subscribeMarket 自动重发订阅。
			o.mu.RLock()
			tokens := append([]string(nil), o.tokens...)
			o.mu.RUnlock()
			o.monitor.SubscribeTokens(tokens...)
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

// addTokens 向订阅列表追加 token 并去重，返回新切片。
func addTokens(list []string, tokens ...string) []string {
	set := make(map[string]struct{}, len(list)+len(tokens))
	for _, t := range list {
		set[t] = struct{}{}
	}
	for _, t := range tokens {
		set[t] = struct{}{}
	}
	dst := make([]string, 0, len(set))
	for t := range set {
		dst = append(dst, t)
	}
	return dst
}

// removeTokens 从订阅列表中移除指定 token，返回新切片。
func removeTokens(list []string, tokens ...string) []string {
	rm := make(map[string]struct{}, len(tokens))
	for _, t := range tokens {
		rm[t] = struct{}{}
	}
	dst := list[:0]
	for _, t := range list {
		if _, ok := rm[t]; !ok {
			dst = append(dst, t)
		}
	}
	return dst
}
