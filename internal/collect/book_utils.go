package collect

import (
	"github.com/xiangxn/go-polymarket-sdk/orders"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// BestBid 返回订单簿最优买价（Polymarket CLOB: bids 升序，最优在最后）。
// 空盘口返回 0。
func BestBid(book *sdk.OrderBook) float64 {
	if book == nil || len(book.Bids) == 0 {
		return 0
	}
	return book.Bids[len(book.Bids)-1].Price
}

// BestAsk 返回订单簿最优卖价。CLOB WS 的 asks 是降序（0.99 填充单在前，
// 真实最优卖价在最后），与 bids 升序对称，最优都在末尾 —— 与 SDK 自身
// GetTokenOrderBook 取 Asks[len-1] 的约定一致。空盘口返回 0。
// ⚠️ 曾误取 Asks[0]（恒为 0.99/1.0 填充价），2026-08-18 数据审计修正。
func BestAsk(book *sdk.OrderBook) float64 {
	if book == nil || len(book.Asks) == 0 {
		return 0
	}
	return book.Asks[len(book.Asks)-1].Price
}

// topNQuantity 返回靠市场最优侧的 n 档数量之和（承接结构指标）。
// CLOB WS 的 bids 升序（0.01 在前）、asks 降序（0.99 在前），最优价
// 都在切片末尾 —— 因此取末尾 n 档而非开头（开头是远离市场的填充档）。
// 不足 n 档时返回现有档位之和。
func topNQuantity(levels []orders.Book, n int) float64 {
	if len(levels) > n {
		levels = levels[len(levels)-n:]
	}
	var sum float64
	for _, l := range levels {
		sum += l.Size
	}
	return sum
}

// top5 返回前 5 档数量之和（盘口承接结构指标）。
func top5(levels []orders.Book) float64 {
	return topNQuantity(levels, 5)
}

// MakePMTick 从 YES/NO 订单簿构造 1s 盘口快照行。
// top5 数量取双方簿前 5 档之和；时戳/延迟取两簿较大者。
func MakePMTick(yesBook, noBook *sdk.OrderBook) PMTick {
	t := PMTick{
		YesBid: BestBid(yesBook),
		YesAsk: BestAsk(yesBook),
		NoBid:  BestBid(noBook),
		NoAsk:  BestAsk(noBook),
	}
	if yesBook != nil {
		t.YesBidTop5 = top5(yesBook.Bids)
		t.YesAskTop5 = top5(yesBook.Asks)
		if yesBook.Timestamp > t.BookTs {
			t.BookTs = yesBook.Timestamp
		}
		if yesBook.Latency > t.BookLatMs {
			t.BookLatMs = yesBook.Latency
		}
	}
	if noBook != nil {
		t.NoBidTop5 = top5(noBook.Bids)
		t.NoAskTop5 = top5(noBook.Asks)
		if noBook.Timestamp > t.BookTs {
			t.BookTs = noBook.Timestamp
		}
		if noBook.Latency > t.BookLatMs {
			t.BookLatMs = noBook.Latency
		}
	}
	return t
}
