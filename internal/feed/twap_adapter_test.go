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

// ── 推送缓存 / PushNearest（2026-09-19 精确取锚）──

// pushEval 发一条带**评估时刻**的 TWAP-60 推送（tsMs = payload.timestamp）。
func pushEval(ch chan<- sdk.ExternalPrice, price float64, tsMs int64) {
	ch <- sdk.ExternalPrice{Symbol: "BTC", Price: price, Source: "ChainlinkTWAP60",
		WindowSeconds: 60, Timestamp: tsMs}
}

// TestPushNearest_EmptyCache 空缓存不命中。
func TestPushNearest_EmptyCache(t *testing.T) {
	a := NewTwapAdapterWithChannel(make(chan sdk.ExternalPrice), "BTC", 60)
	if price, arrivedMs, ok := a.PushNearest(time.Now()); ok || price != 0 || arrivedMs != 0 {
		t.Fatalf("空缓存应未命中, 得到 (%.2f, %d, %v)", price, arrivedMs, ok)
	}
}

// TestPushNearest_ExactHitAndUnit 精确匹配 + 单位钉死（毫秒）: 只有评估时刻**恰好
// 等于边界**的那条命中（±1s 的两条在缓存里但不得命中）——若 Timestamp 被当成秒处理,
// 精确条永远命不中, 而 ±1s 会被误命中。
func TestPushNearest_ExactHitAndUnit(t *testing.T) {
	ch := make(chan sdk.ExternalPrice, 8)
	a := NewTwapAdapterWithChannel(ch, "BTC", 60)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a.Start(ctx)

	boundary := time.Now().Truncate(time.Second)
	t0 := time.Now().UnixMilli()
	pushEval(ch, 50_000, boundary.Add(-time.Second).UnixMilli()) // 边界前一秒（t=0 口径能拿到的那条）
	pushEval(ch, 50_010, boundary.UnixMilli())                   // 边界那一秒 = 官方 open 口径
	pushEval(ch, 50_020, boundary.Add(time.Second).UnixMilli())
	waitPrice(t, a, 50_020)

	price, arrivedMs, ok := a.PushNearest(boundary)
	if !ok || price != 50_010 {
		t.Fatalf("应精确命中边界那一秒, 得到 (%.2f, %v)", price, ok)
	}
	if arrivedMs < t0 || arrivedMs > time.Now().UnixMilli() {
		t.Fatalf("arrivedMs 应为本地到达时刻（%d 不在 [%d, now]）", arrivedMs, t0)
	}
	// ±1s 的两条各自精确匹配得到自己的值（证明不是「取最近」）
	if p, _, ok := a.PushNearest(boundary.Add(-time.Second)); !ok || p != 50_000 {
		t.Fatalf("边界前一秒应命中 50000, 得到 (%.2f, %v)", p, ok)
	}
	if p, _, ok := a.PushNearest(boundary.Add(time.Second)); !ok || p != 50_020 {
		t.Fatalf("边界后一秒应命中 50020, 得到 (%.2f, %v)", p, ok)
	}
}

// TestPushNearest_NoApproximation 缓存里没有边界那一秒的条目时**不命中**（哪怕
// 边界前后都有条目）: 精确取锚宁可让调用方继续重试, 也不拿近似值充数。
func TestPushNearest_NoApproximation(t *testing.T) {
	ch := make(chan sdk.ExternalPrice, 4)
	a := NewTwapAdapterWithChannel(ch, "BTC", 60)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a.Start(ctx)

	boundary := time.Now().Truncate(time.Second)
	pushEval(ch, 50_000, boundary.Add(-time.Second).UnixMilli())
	pushEval(ch, 50_020, boundary.Add(time.Second).UnixMilli())
	waitPrice(t, a, 50_020)

	if price, _, ok := a.PushNearest(boundary); ok {
		t.Fatalf("不得用近似条目充数, 得到 (%.2f, %v)", price, ok)
	}
}

// TestPushNearest_TieBreakLaterArrival 同一评估时刻出现多条（重连补发/乱序重放）
// 时取**后到者**: 结果钉死为确定值。
func TestPushNearest_TieBreakLaterArrival(t *testing.T) {
	ch := make(chan sdk.ExternalPrice, 4)
	a := NewTwapAdapterWithChannel(ch, "BTC", 60)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a.Start(ctx)

	boundary := time.Now().Truncate(time.Second)
	ts := boundary.UnixMilli()
	pushEval(ch, 50_000, ts)
	pushEval(ch, 50_100, ts) // 同一评估时刻, 后到 → 应胜出
	waitPrice(t, a, 50_100)

	price, _, ok := a.PushNearest(boundary)
	if !ok || price != 50_100 {
		t.Fatalf("重复评估时刻应取后到者, 得到 (%.2f, %v)", price, ok)
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

	if price, _, ok := a.PushNearest(boundary); ok || price != 0 {
		t.Fatalf("非正价格应被丢弃, 得到 (%.2f, %v)", price, ok)
	}
	if p, _ := a.Latest(); p != 50_000 {
		t.Fatalf("非正价格不应刷新 Latest: %.2f", p)
	}
}

// TestPushNearest_MissingTimestampNotCached 缺 payload.timestamp 的条目不进缓存
// （按到达时刻兜底的条目在精确口径下永远命不中, 留着只会污染 CacheStat 诊断）,
// 但 Latest 照常刷新——它是看门狗/dashboard 的输入, 与锚无关。
func TestPushNearest_MissingTimestampNotCached(t *testing.T) {
	ch := make(chan sdk.ExternalPrice, 4)
	a := NewTwapAdapterWithChannel(ch, "BTC", 60)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a.Start(ctx)

	boundary := time.Now().Truncate(time.Second)
	pushEval(ch, 50_000, 0) // 无评估时刻
	waitPrice(t, a, 50_000)

	if price, _, ok := a.PushNearest(boundary); ok {
		t.Fatalf("缺时间戳的条目不应入缓存, 得到 (%.2f, %v)", price, ok)
	}
	if n, _, noTs := a.CacheStat(); n != 0 || noTs != 1 {
		t.Fatalf("CacheStat 应为 (0 条, 1 条缺时间戳), 得到 (%d, %d)", n, noTs)
	}
}

// TestCacheStat 诊断快照: 条目数 + 最新一条的评估偏移（负值 = 评估在过去）。
func TestCacheStat(t *testing.T) {
	ch := make(chan sdk.ExternalPrice, 4)
	a := NewTwapAdapterWithChannel(ch, "BTC", 60)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a.Start(ctx)

	now := time.Now().Truncate(time.Second)
	pushEval(ch, 50_000, now.Add(-3*time.Second).UnixMilli())
	pushEval(ch, 50_010, now.Add(-time.Second).UnixMilli())
	waitPrice(t, a, 50_010)

	n, off, noTs := a.CacheStat()
	if n != 2 || noTs != 0 {
		t.Fatalf("CacheStat 条目数/缺时间戳数错: (%d, %d)", n, noTs)
	}
	// 最新一条评估于边界 −1s，now 距它应落在 [−1000, +4000]ms（机器慢时放宽）
	if off > -500 || off < -4000 {
		t.Fatalf("最新评估偏移应在 −1s 附近, 得到 %+dms", off)
	}
}

// TestPushNearest_RingCap 缓存按容量淘汰最老条目: 被挤出的边界条目再也取不回
// （故 twapPushCap 必须覆盖取锚重试预算, 见其注释）。
func TestPushNearest_RingCap(t *testing.T) {
	ch := make(chan sdk.ExternalPrice, twapPushCap+8)
	a := NewTwapAdapterWithChannel(ch, "BTC", 60)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a.Start(ctx)

	boundary := time.Now().Truncate(time.Second)
	pushEval(ch, 11_111, boundary.UnixMilli()) // 边界那一秒, 随后灌满容量把它挤出去
	for i := 1; i <= twapPushCap; i++ {
		pushEval(ch, 50_000+float64(i), boundary.Add(time.Duration(i)*time.Second).UnixMilli())
	}
	waitPrice(t, a, 50_000+float64(twapPushCap))

	if price, _, ok := a.PushNearest(boundary); ok {
		t.Fatalf("被挤出的边界条目不应再命中, 得到 (%.2f, %v)", price, ok)
	}
	if n, _, _ := a.CacheStat(); n != twapPushCap {
		t.Fatalf("缓存应恰为容量 %d 条, 得到 %d", twapPushCap, n)
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
	pushEval(ch2, 50_100, boundary.UnixMilli())
	waitPrice(t, a, 50_100)

	if price, _, ok := a.PushNearest(boundary); !ok || price != 50_100 {
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
