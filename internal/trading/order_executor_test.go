package trading

import (
	"testing"

	"github.com/xiangxn/go-polymarket-sdk/orders"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

func TestFormatBook_BestAskFirst(t *testing.T) {
	// WS 约定：asks 降序（末尾为最优卖价），渲染时应倒序输出
	book := &sdk.OrderBookSummary{
		Bids: []orders.Book{
			{Price: 0.21, Size: 50},
			{Price: 0.22, Size: 100}, // 最优买价
		},
		Asks: []orders.Book{
			{Price: 0.26, Size: 40},
			{Price: 0.25, Size: 80},
			{Price: 0.24, Size: 120}, // 最优卖价（可买价）
		},
		Timestamp: 1000,
	}
	got := formatBook(book, 5, 5000)
	want := "asks=[0.2400x120.0, 0.2500x80.0, 0.2600x40.0] bid=0.2200x100.0 age=4000ms"
	if got != want {
		t.Errorf("formatBook = %s, want %s", got, want)
	}
}

func TestFormatBook_MaxLevels(t *testing.T) {
	book := &sdk.OrderBookSummary{
		Asks: []orders.Book{
			{Price: 0.30, Size: 1},
			{Price: 0.29, Size: 2},
			{Price: 0.28, Size: 3},
			{Price: 0.27, Size: 4},
		},
	}
	got := formatBook(book, 2, 0)
	want := "asks=[0.2700x4.0, 0.2800x3.0]"
	if got != want {
		t.Errorf("formatBook = %s, want %s", got, want)
	}
}

func TestFormatBook_NilAndEmpty(t *testing.T) {
	if got := formatBook(nil, 5, 0); got != "book=nil" {
		t.Errorf("formatBook(nil) = %s, want book=nil", got)
	}
	empty := &sdk.OrderBookSummary{}
	got := formatBook(empty, 5, 0)
	want := "asks=[]"
	if got != want {
		t.Errorf("formatBook(empty) = %s, want %s", got, want)
	}
}

func TestBestAsk(t *testing.T) {
	if got := bestAsk(nil); got != 0 {
		t.Errorf("bestAsk(nil) = %.4f, want 0", got)
	}
	empty := &sdk.OrderBookSummary{}
	if got := bestAsk(empty); got != 0 {
		t.Errorf("bestAsk(empty) = %.4f, want 0", got)
	}
	book := &sdk.OrderBookSummary{
		Asks: []orders.Book{
			{Price: 0.40, Size: 10},
			{Price: 0.38, Size: 20},
			{Price: 0.36, Size: 30}, // 最优卖价（WS: 降序，末尾最优）
		},
	}
	if got := bestAsk(book); got != 0.36 {
		t.Errorf("bestAsk = %.4f, want 0.36", got)
	}
}
