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

// ── 推送缓存 / PushNearest（2026-09-18 锚升级通道）──

// pushEval 发一条带**评估时刻**的 TWAP-60 推送（tsMs = payload.timestamp）。
func pushEval(ch chan<- sdk.ExternalPrice, price float64, tsMs int64) {
	ch <- sdk.ExternalPrice{Symbol: "BTC", Price: price, Source: "ChainlinkTWAP60",
		WindowSeconds: 60, Timestamp: tsMs}
}

// TestPushNearest_EmptyCache 空缓存不命中。
func TestPushNearest_EmptyCache(t *testing.T) {
	a := NewTwapAdapterWithChannel(make(chan sdk.ExternalPrice), "BTC", 60)
	if price, pickMs, ok := a.PushNearest(time.Now(), 10*time.Second); ok || price != 0 || pickMs != 0 {
		t.Fatalf("空缓存应未命中, 得到 (%.2f, %d, %v)", price, pickMs, ok)
	}
}

// TestPushNearest_ExactHitAndUnit 单位钉死（毫秒）: 评估时刻恰好等于边界的条目
// 命中且 pickMs=0——若 Timestamp 被当成秒处理, 这条永远命不中。
func TestPushNearest_ExactHitAndUnit(t *testing.T) {
	ch := make(chan sdk.ExternalPrice, 8)
	a := NewTwapAdapterWithChannel(ch, "BTC", 60)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a.Start(ctx)

	boundary := time.Now().Truncate(time.Second)
	pushEval(ch, 50_000, boundary.Add(-2*time.Second).UnixMilli()) // 边界前 2s（t=0 采样能看到的那条）
	pushEval(ch, 50_010, boundary.UnixMilli())                     // 边界那一秒 = 官方 open 口径
	pushEval(ch, 50_020, boundary.Add(time.Second).UnixMilli())
	waitPrice(t, a, 50_020)

	price, pickMs, ok := a.PushNearest(boundary, 10*time.Second)
	if !ok || price != 50_010 || pickMs != 0 {
		t.Fatalf("应命中边界那一秒, 得到 (%.2f, %d, %v)", price, pickMs, ok)
	}
}

// TestPushNearest_SelectsByEvalNotArrival 选条按**评估时刻**而非到达时刻:
// 刚到达但评估时刻在边界前 5s 的推送, 在 2s 容差下未命中（按到达口径会命中）。
func TestPushNearest_SelectsByEvalNotArrival(t *testing.T) {
	ch := make(chan sdk.ExternalPrice, 4)
	a := NewTwapAdapterWithChannel(ch, "BTC", 60)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a.Start(ctx)

	boundary := time.Now().Truncate(time.Second)
	pushEval(ch, 50_000, boundary.Add(-5*time.Second).UnixMilli()) // 刚到达, 但评估在边界前 5s
	waitPrice(t, a, 50_000)

	if _, pickMs, ok := a.PushNearest(boundary, 2*time.Second); ok {
		t.Fatalf("超容差不得命中, 得到 pickMs=%d", pickMs)
	}
	price, pickMs, ok := a.PushNearest(boundary, 6*time.Second)
	if !ok || price != 50_000 || pickMs != -5000 {
		t.Fatalf("放宽容差应命中并给出负偏移, 得到 (%.2f, %d, %v)", price, pickMs, ok)
	}
}

// TestPushNearest_TieBreakLaterArrival |评估偏移| 并列时取后到者（重连补发的确定化）。
func TestPushNearest_TieBreakLaterArrival(t *testing.T) {
	ch := make(chan sdk.ExternalPrice, 4)
	a := NewTwapAdapterWithChannel(ch, "BTC", 60)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a.Start(ctx)

	boundary := time.Now().Truncate(time.Second)
	pushEval(ch, 50_000, boundary.Add(-time.Second).UnixMilli())
	pushEval(ch, 50_100, boundary.Add(time.Second).UnixMilli()) // 与上一条 |偏移| 并列, 后到
	waitPrice(t, a, 50_100)

	price, pickMs, ok := a.PushNearest(boundary, 10*time.Second)
	if !ok || price != 50_100 || pickMs != 1000 {
		t.Fatalf("并列应取后到者, 得到 (%.2f, %d, %v)", price, pickMs, ok)
	}
}

// TestPushNearest_NonPositiveNotCached 非正价格不入缓存（防 ok=true 送回 0 锚）。
func TestPushNearest_NonPositiveNotCached(t *testing.T) {
	ch := make(chan sdk.ExternalPrice, 4)
	a := NewTwapAdapterWithChannel(ch, "BTC", 60)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a.Start(ctx)

	boundary := time.Now().Truncate(time.Second)
	pushEval(ch, 50_000, boundary.Add(-3*time.Second).UnixMilli())
	pushEval(ch, 0, boundary.UnixMilli()) // 应被丢弃
	pushEval(ch, -1, boundary.Add(time.Second).UnixMilli())
	time.Sleep(100 * time.Millisecond) // 等消费（Latest 不更新, 无可轮询的目标）

	if price, pickMs, ok := a.PushNearest(boundary, 10*time.Second); !ok || price != 50_000 || pickMs != -3000 {
		t.Fatalf("非正价格应被丢弃, 得到 (%.2f, %d, %v)", price, pickMs, ok)
	}
	if p, _ := a.Latest(); p != 50_000 {
		t.Fatalf("非正价格不应刷新 Latest: %.2f", p)
	}
}

// TestPushNearest_MissingTimestampFallback 缺 payload.timestamp 时按本地到达兜底
// （唯一静默失败模式, 必须有兜底而非丢条）。
func TestPushNearest_MissingTimestampFallback(t *testing.T) {
	ch := make(chan sdk.ExternalPrice, 4)
	a := NewTwapAdapterWithChannel(ch, "BTC", 60)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a.Start(ctx)

	boundary := time.Now()
	pushEval(ch, 50_000, 0) // 无评估时刻
	waitPrice(t, a, 50_000)

	price, pickMs, ok := a.PushNearest(boundary, 5*time.Second)
	if !ok || price != 50_000 {
		t.Fatalf("缺时间戳应兜底命中, 得到 (%.2f, %d, %v)", price, pickMs, ok)
	}
	if pickMs < -1000 || pickMs > 5000 {
		t.Fatalf("兜底偏移应贴近 0（本地到达）, 得到 %d", pickMs)
	}
}

// TestPushNearest_RingCap 缓存按容量淘汰最老条目（twapPushCap 条之外不可见）。
func TestPushNearest_RingCap(t *testing.T) {
	ch := make(chan sdk.ExternalPrice, twapPushCap+8)
	a := NewTwapAdapterWithChannel(ch, "BTC", 60)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a.Start(ctx)

	boundary := time.Now().Truncate(time.Second)
	// 第一条评估时刻最贴边界（最该被选中）, 随后灌满容量把它挤出去
	pushEval(ch, 11_111, boundary.UnixMilli())
	for i := 1; i <= twapPushCap; i++ {
		pushEval(ch, 50_000+float64(i), boundary.Add(time.Duration(i)*time.Second).UnixMilli())
	}
	waitPrice(t, a, 50_000+float64(twapPushCap))

	price, _, ok := a.PushNearest(boundary, time.Hour)
	if !ok || price == 11_111 {
		t.Fatalf("最老条目应被淘汰, 得到 (%.2f, %v)", price, ok)
	}
	// 环内最老者 = 第 2 条（评估 +1s）
	if price != 50_001 {
		t.Fatalf("应选中环内最贴边界的存活条目, 得到 %.2f", price)
	}
}

// TestPushNearest_SwapIsolation 热替换后旧通道的推送不入环（重建订阅的数据面切换）。
func TestPushNearest_SwapIsolation(t *testing.T) {
	ch1 := make(chan sdk.ExternalPrice, 4)
	ch2 := make(chan sdk.ExternalPrice, 4)
	a := NewTwapAdapterWithChannel(ch1, "BTC", 60)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a.Start(ctx)

	boundary := time.Now().Truncate(time.Second)
	pushEval(ch1, 50_000, boundary.Add(-2*time.Second).UnixMilli())
	waitPrice(t, a, 50_000)

	a.Swap(ch2)
	pushEval(ch1, 99_999, boundary.UnixMilli()) // 旧通道: 应被忽略
	pushEval(ch2, 50_100, boundary.Add(time.Second).UnixMilli())
	waitPrice(t, a, 50_100)

	if price, _, ok := a.PushNearest(boundary, time.Hour); !ok || price == 99_999 {
		t.Fatalf("旧通道推送不应入环, 得到 (%.2f, %v)", price, ok)
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
