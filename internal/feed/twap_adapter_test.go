package feed

import (
	"context"
	"testing"
	"time"

	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// waitPrice 轮询等待 Latest 达到期望价格（异步消费，限时 2s）。
func waitPrice(t *testing.T, a *TwapAdapter, want float64) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		price, _ := a.Latest()
		if price == want {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("timeout: last price=%.2f (want %.2f)", price, want)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// TestTwapAdapter_SwapHotSwap 验证 Swap 热替换推送通道后，新通道推送生效、
// 旧通道推送不再被消费（重建订阅后的数据面切换）。
func TestTwapAdapter_SwapHotSwap(t *testing.T) {
	ch1 := make(chan sdk.ExternalPrice, 8)
	ch2 := make(chan sdk.ExternalPrice, 8)
	a := NewTwapAdapterWithChannel(ch1, "BTC", 60)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a.Start(ctx)

	ch1 <- sdk.ExternalPrice{Symbol: "BTC", Price: 50100, WindowSeconds: 60}
	waitPrice(t, a, 50100)

	a.Swap(ch2)
	ch1 <- sdk.ExternalPrice{Symbol: "BTC", Price: 99999, WindowSeconds: 60} // 旧通道，应被忽略
	ch2 <- sdk.ExternalPrice{Symbol: "BTC", Price: 50200, WindowSeconds: 60}
	waitPrice(t, a, 50200)

	time.Sleep(100 * time.Millisecond)
	if price, _ := a.Latest(); price != 50200 {
		t.Fatalf("旧通道推送被消费: price=%.2f, want 50200", price)
	}
}

// TestTwapAdapter_StaleWatchdogWithoutClient 验证看门狗触发路径：
// 无 client（测试构造）时超过 maxStale 未收到推送 → rebuild 告警分支，
// 不 panic、价格保留、不重建。
func TestTwapAdapter_StaleWatchdogWithoutClient(t *testing.T) {
	ch := make(chan sdk.ExternalPrice, 8)
	a := NewTwapAdapterWithChannel(ch, "BTC", 60)
	a.maxStale = 150 * time.Millisecond
	a.checkInterval = 30 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a.StartWithMonitor(ctx) // client=nil → spawnRun 跳过，看门狗触发时仅告警

	ch <- sdk.ExternalPrice{Symbol: "BTC", Price: 50100, WindowSeconds: 60}
	waitPrice(t, a, 50100)

	// 停推超过 maxStale：看门狗应多次触发 rebuild（client=nil 告警路径）
	time.Sleep(400 * time.Millisecond)
	if price, _ := a.Latest(); price != 50100 {
		t.Fatalf("看门狗触发后价格被意外修改: %.2f", price)
	}
}

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
