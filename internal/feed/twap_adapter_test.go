package feed

import (
	"context"
	"testing"
	"time"

	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

func TestTwapAdapter_NoData(t *testing.T) {
	ch := make(chan sdk.ExternalPrice)
	a := NewTwapAdapterWithChannel(ch, "BTC", 60)

	price, age := a.Latest()
	if price != 0 || age != 0 {
		t.Errorf("expected (0, 0) before any push, got (%.2f, %d)", price, age)
	}
}

func TestTwapAdapter_FilterWindowAndSymbol(t *testing.T) {
	ch := make(chan sdk.ExternalPrice, 8)
	a := NewTwapAdapterWithChannel(ch, "BTC", 60)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a.Start(ctx)

	// 30s 窗口与其它 symbol 必须被丢弃，只保留 BTC-60
	ch <- sdk.ExternalPrice{Symbol: "BTC", Price: 50100, Source: "ChainlinkTWAP60", WindowSeconds: 60}
	ch <- sdk.ExternalPrice{Symbol: "BTC", Price: 50200, Source: "ChainlinkTWAP30", WindowSeconds: 30}
	ch <- sdk.ExternalPrice{Symbol: "ETH", Price: 3000, Source: "ChainlinkTWAP60", WindowSeconds: 60}
	ch <- sdk.ExternalPrice{Symbol: "btc", Price: 50150, Source: "ChainlinkTWAP60", WindowSeconds: 60}

	// 等待异步消费完成
	deadline := time.Now().Add(2 * time.Second)
	for {
		price, age := a.Latest()
		if price == 50150 {
			if age < 0 || age > 5000 {
				t.Errorf("age out of range: %dms", age)
			}
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("timeout: last price=%.2f (expected 50150)", price)
		}
		time.Sleep(5 * time.Millisecond)
	}
}
