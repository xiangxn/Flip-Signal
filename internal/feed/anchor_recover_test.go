// 2026-09-18 锚升级通道测试（docs/dog020_anchor_upgrade_2026-09-18.md）。
// 全部注入假 fetcher / 假缓存取数（零网络）；Schedule 与轮询节拍都压到毫秒级
// （opts.PickInterval），用「请求时刻相对边界」核验节奏，不真实等待 +2/+5/+40s。
package feed

import (
	"context"
	"sync"
	"testing"
	"time"
)

// seqFetch 返回按调用次序取值的假 fetcher（越界复用最后一个）与调用时刻/入参记录。
func seqFetch(prices ...float64) (OpenPriceFetcher, *[]time.Time) {
	var mu sync.Mutex
	var calls []time.Time
	i := 0
	return func(_ context.Context, _, _ time.Time) float64 {
		mu.Lock()
		defer mu.Unlock()
		calls = append(calls, time.Now())
		p := prices[min(i, len(prices)-1)]
		i++
		return p
	}, &calls
}

// blockingFetch 每次调用阻塞 block 后返回 price。
func blockingFetch(price float64, block time.Duration) (OpenPriceFetcher, *[]time.Time) {
	var mu sync.Mutex
	var calls []time.Time
	return func(ctx context.Context, _, _ time.Time) float64 {
		mu.Lock()
		calls = append(calls, time.Now())
		mu.Unlock()
		select {
		case <-ctx.Done():
			return 0
		case <-time.After(block):
			return price
		}
	}, &calls
}

// pickStep 是脚本化取数的一条: 价格 + 评估偏移。
type pickStep struct {
	price  float64
	pickMs int64
}

// scriptPick 按调用次序循环给出 (price, pickMs)，并记录收到的容差。
func scriptPick(seq []pickStep) (PushPicker, *[]time.Duration) {
	var mu sync.Mutex
	var tols []time.Duration
	i := 0
	return func(_ time.Time, tol time.Duration) (float64, int64, bool) {
		mu.Lock()
		defer mu.Unlock()
		tols = append(tols, tol)
		s := seq[i%len(seq)]
		i++
		return s.price, s.pickMs, true
	}, &tols
}

// nopPick 恒不命中（缓存空/超容差）。
func nopPick(time.Time, time.Duration) (float64, int64, bool) { return 0, 0, false }

// drain 收完通道（关闭即返回），超时即失败。
func drain(t *testing.T, ch <-chan AnchorRecovery, timeout time.Duration) []AnchorRecovery {
	t.Helper()
	var got []AnchorRecovery
	deadline := time.After(timeout)
	for {
		select {
		case r, ok := <-ch:
			if !ok {
				return got
			}
			got = append(got, r)
		case <-deadline:
			t.Fatalf("通道未在 %v 内关闭（已收 %d 条）", timeout, len(got))
		}
	}
}

// opts 造一份毫秒级节拍的测试参数。
func opts(sched ...time.Duration) AnchorUpgradeOpts {
	return AnchorUpgradeOpts{
		Schedule:     sched,
		PickTol:      50 * time.Millisecond,
		FetchTimeout: 200 * time.Millisecond,
		PickInterval: 2 * time.Millisecond,
	}
}

// TestRecoverAnchorOfficialLastWins 官方**每次成功都采纳**（last-wins）: 不再以首次
// 成功为终局——官方值本身在头十几秒仍在收敛（docs §9），最后一次采样即最终锚。
func TestRecoverAnchorOfficialLastWins(t *testing.T) {
	boundary := time.Now()
	// 三个采样点各返回不同价（模拟官方收敛: 99_000 → 99_200 → 99_300）
	// SettleAfter=0 = 无收敛门（生产由 main 置 +20s）
	fetch, calls := seqFetch(99_000, 99_200, 99_300)
	got := drain(t, RecoverAnchor(context.Background(), fetch, nopPick, boundary,
		boundary.Add(5*time.Minute),
		opts(10*time.Millisecond, 30*time.Millisecond, 60*time.Millisecond)), 2*time.Second)

	if len(got) != 3 {
		t.Fatalf("三个采样点都应采纳（last-wins）, 得到 %d 条: %+v", len(got), got)
	}
	for i, want := range []float64{99_000, 99_200, 99_300} {
		if got[i].Source != AnchorSourceOfficial || got[i].Price != want {
			t.Fatalf("第 %d 条 = %+v, 期望 official/%.0f", i, got[i], want)
		}
	}
	if n := len(*calls); n != 3 {
		t.Fatalf("官方请求应 3 次（每点一次）, 得到 %d", n)
	}
	if d := (*calls)[0].Sub(boundary); d < 8*time.Millisecond || d > 100*time.Millisecond {
		t.Fatalf("首点应在边界 +10ms 附近发出, 实际 %v", d)
	}
	if got[0].AtMs < boundary.UnixMilli() {
		t.Fatalf("AtMs = %d, 早于边界 %d", got[0].AtMs, boundary.UnixMilli())
	}
}

// TestRecoverAnchorSettleGuard 收敛门: 已有流值锚时, 收敛点（+20s）之前的官方成功值
// 只作兜底、不覆盖流值（否则会把已知精确的边界那一秒推送换成收敛中的临时值）。
func TestRecoverAnchorSettleGuard(t *testing.T) {
	boundary := time.Now()
	sched := []time.Duration{10 * time.Millisecond, 30 * time.Millisecond, 60 * time.Millisecond}

	// ① 有 t=0 流值种子 → +10ms/+30ms 两点丢弃, +60ms（≥ 收敛点 50ms）采纳
	o := opts(sched...)
	o.SettleAfter = 50 * time.Millisecond
	o.SeedPickMs, o.SeedOK = -1000, true
	fetch, _ := seqFetch(99_000, 99_200, 99_300)
	got := drain(t, RecoverAnchor(context.Background(), fetch, nopPick, boundary,
		boundary.Add(5*time.Minute), o), 2*time.Second)
	if len(got) != 1 || got[0].Source != AnchorSourceOfficial || got[0].Price != 99_300 {
		t.Fatalf("收敛门内应只采纳最后一个采样点（99300）, 得到 %+v", got)
	}

	// ② 无流值（SeedOK=false 且缓存不命中）→ 早期点照常采纳（兜底, 好过整窗无锚）
	// 注意必须换新 boundary: ① 已跑掉 ~60ms, 复用会把三个采样点压成「一次性」。
	boundary2 := time.Now()
	o2 := opts(sched...)
	o2.SettleAfter = 50 * time.Millisecond
	fetch2, _ := seqFetch(99_000, 99_200, 99_300)
	got2 := drain(t, RecoverAnchor(context.Background(), fetch2, nopPick, boundary2,
		boundary2.Add(5*time.Minute), o2), 2*time.Second)
	if len(got2) != 3 {
		t.Fatalf("无流值时早期官方值应作兜底全部采纳, 得到 %+v", got2)
	}
}

// TestRecoverAnchorSettleGuardStreamHit 收敛门同样认可**重选命中**（不只是 t=0 种子）:
// 升级通道自己送出过 stream 后, 早期官方点即不再覆盖。
func TestRecoverAnchorSettleGuardStreamHit(t *testing.T) {
	boundary := time.Now()
	pick, _ := scriptPick([]pickStep{{98_500, 0}}) // 首拍即命中「边界那一秒」
	o := opts(10*time.Millisecond, 40*time.Millisecond)
	o.SettleAfter = 100 * time.Millisecond // 两点都在收敛点之前
	fetch, _ := seqFetch(99_000, 99_200)
	got := drain(t, RecoverAnchor(context.Background(), fetch, pick, boundary,
		boundary.Add(5*time.Minute), o), 2*time.Second)

	if len(got) != 1 || got[0].Source != AnchorSourceStream {
		t.Fatalf("有重选命中时早期官方点不应覆盖流值, 得到 %+v", got)
	}
}

// TestRecoverAnchorAllFail 官方全失败: 每个 Schedule 点各试一次（±40ms 节奏），
// 一条都不送出、通道关闭（调用方保留流值锚）。
func TestRecoverAnchorAllFail(t *testing.T) {
	boundary := time.Now()
	sched := []time.Duration{10 * time.Millisecond, 30 * time.Millisecond, 60 * time.Millisecond}
	fetch, calls := seqFetch(0)
	var got []AnchorRecovery
	el := waitMs(t, 60*time.Millisecond, 2*time.Second, func() {
		got = drain(t, RecoverAnchor(context.Background(), fetch, nopPick, boundary,
			boundary.Add(5*time.Minute), opts(sched...)), 2*time.Second)
	})

	if len(got) != 0 {
		t.Fatalf("官方全失败不应送出任何结果, 得到 %+v", got)
	}
	if n := len(*calls); n != len(sched) {
		t.Fatalf("官方应尝试 %d 次, 得到 %d", len(sched), n)
	}
	for i := range *calls {
		want := boundary.Add(sched[i])
		if d := (*calls)[i].Sub(want); d < -40*time.Millisecond || d > 40*time.Millisecond {
			t.Fatalf("第 %d 次请求距边界 %v, 期望 %v（±40ms）", i, d, sched[i])
		}
	}
	if el > 500*time.Millisecond {
		t.Fatalf("最后一个点失败后应立即结束, 实际 %v", el)
	}
}

// TestRecoverAnchorFetchTimeoutSkipsPassedPoints 单次请求卡顿（跨过后续点）时
// 跳过已流过的 Schedule 点、不补发: 被跨过的点不会各领一次请求（4 点 → 3 次请求）。
func TestRecoverAnchorFetchTimeoutSkipsPassedPoints(t *testing.T) {
	boundary := time.Now()
	// 第 1 点在 +5ms 发出、阻塞 30ms（跨过 +10ms/+25ms），第 3 次对齐 +60ms
	fetch, calls := blockingFetch(0, 30*time.Millisecond)
	drain(t, RecoverAnchor(context.Background(), fetch, nopPick, boundary,
		boundary.Add(5*time.Minute),
		opts(5*time.Millisecond, 10*time.Millisecond, 25*time.Millisecond, 60*time.Millisecond),
	), 2*time.Second)

	if n := len(*calls); n != 3 {
		t.Fatalf("被跨过的点应合并成一次请求（不补发）, 期望 3 次, 得到 %d（%v）", n, *calls)
	}
	// 第二次请求只能在首次阻塞结束后发出（若 +10ms 点被单独补发, 间隔会 < 30ms）
	if gap := (*calls)[1].Sub((*calls)[0]); gap < 28*time.Millisecond {
		t.Fatalf("被跨过的 Schedule 点不应补发, 与首次请求间隔仅 %v", gap)
	}
	// 第三次对齐 +60ms 点（±40ms）
	if d := (*calls)[2].Sub(boundary.Add(60 * time.Millisecond)); d < -40*time.Millisecond || d > 40*time.Millisecond {
		t.Fatalf("第三次请求应落在 +60ms 点附近, 实际 %v", d)
	}
}

// TestRecoverAnchorPickMonotone 缓存重选的单调门: |评估偏移| 严格变小才送，
// 并列或变差一律不送（防重连补发/乱序把锚带偏）。
func TestRecoverAnchorPickMonotone(t *testing.T) {
	boundary := time.Now()
	// 循环脚本: −1000 → −500 → 0 → −900（变差）→ 0（并列, 不送）
	pickFn, _ := scriptPick([]pickStep{
		{98_000, -1000}, {98_001, -500}, {98_002, 0}, {98_003, -900}, {98_004, 0},
	})
	o := opts() // 空 Schedule: 只跑重选, 硬停 = 边界 + PickTol + 2×节拍
	o.SeedPickMs, o.SeedOK = -1200, true
	got := drain(t, RecoverAnchor(context.Background(), fetchNil(), pickFn, boundary,
		boundary.Add(5*time.Minute), o), 2*time.Second)

	want := []int64{-1000, -500, 0}
	if len(got) != len(want) {
		t.Fatalf("应送出 %d 条升级, 得到 %d（%+v）", len(want), len(got), got)
	}
	for i, w := range want {
		if got[i].Source != AnchorSourceStream || got[i].PickMs != w {
			t.Fatalf("第 %d 条 = %+v, 期望 stream/pick=%d", i, got[i], w)
		}
	}
	if got[0].Price != 98_000 || got[2].Price != 98_002 {
		t.Fatalf("价格未随 pick 传递: %+v", got)
	}
}

// TestRecoverAnchorPickSeedGate 单调门基准 = t=0 初值: 无种子（SeedOK=false）时
// 首个命中即胜出; 有种子时并列不升级。
func TestRecoverAnchorPickSeedGate(t *testing.T) {
	boundary := time.Now()
	pick, _ := scriptPick([]pickStep{{98_000, -1000}})

	// 无种子: 首条命中直接送出
	got := drain(t, RecoverAnchor(context.Background(), fetchNil(), pick, boundary,
		boundary.Add(5*time.Minute), opts()), 2*time.Second)
	if len(got) != 1 || got[0].PickMs != -1000 {
		t.Fatalf("无种子时应采纳首条命中: %+v", got)
	}

	// 种子 = −1000（与 pick 并列）: 不升级
	o := opts()
	o.SeedPickMs, o.SeedOK = -1000, true
	if got := drain(t, RecoverAnchor(context.Background(), fetchNil(), pick, boundary,
		boundary.Add(5*time.Minute), o), 2*time.Second); len(got) != 0 {
		t.Fatalf("并列不应升级: %+v", got)
	}

	// 种子 = −1000 但符号相反（+1000）: |偏移| 并列, 同样不升级
	o.SeedPickMs = 1000
	if got := drain(t, RecoverAnchor(context.Background(), fetchNil(), pick, boundary,
		boundary.Add(5*time.Minute), o), 2*time.Second); len(got) != 0 {
		t.Fatalf("并列（异号）不应升级: %+v", got)
	}
}

// TestRecoverAnchorPickTolPassedThrough 容差原样透传给取数器（容差判定在
// TwapAdapter.PushNearest 内, 此处只保证不被改写）。
func TestRecoverAnchorPickTolPassedThrough(t *testing.T) {
	boundary := time.Now()
	pick, tols := scriptPick([]pickStep{{98_000, -1000}})
	o := opts()
	o.PickTol = 150 * time.Millisecond // 取一个与默认/节拍都不重合的值
	drain(t, RecoverAnchor(context.Background(), fetchNil(), pick, boundary,
		boundary.Add(5*time.Minute), o), 2*time.Second)

	if len(*tols) == 0 || (*tols)[0] != 150*time.Millisecond {
		t.Fatalf("容差未透传: %v", *tols)
	}
}

// TestRecoverAnchorContextCancel 窗口收尾取消 ctx → 立即关通道、不 panic、
// 不再发官方请求（首个点已失败, 后点未到）。
func TestRecoverAnchorContextCancel(t *testing.T) {
	boundary := time.Now()
	fetch, calls := seqFetch(0)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		time.Sleep(20 * time.Millisecond)
		cancel()
	}()
	var got []AnchorRecovery
	waitMs(t, 0, 500*time.Millisecond, func() {
		got = drain(t, RecoverAnchor(ctx, fetch, nopPick, boundary,
			boundary.Add(5*time.Minute), opts(2*time.Millisecond, time.Hour)), time.Second)
	})
	if len(got) != 0 {
		t.Fatalf("取消前不应有结果: %+v", got)
	}
	if n := len(*calls); n != 1 {
		t.Fatalf("取消前只应发出首个点的请求, 得到 %d", n)
	}
}

// TestRecoverAnchorNilFetch 未配 fetcher（nil）不 panic: 按失败处理，通道照样关闭。
func TestRecoverAnchorNilFetch(t *testing.T) {
	boundary := time.Now()
	waitMs(t, 0, 500*time.Millisecond, func() {
		drain(t, RecoverAnchor(context.Background(), nil, nopPick, boundary,
			boundary.Add(5*time.Minute), opts(5*time.Millisecond)), time.Second)
	})
}

// TestRecoverAnchorHardStop 空 Schedule 也必须终止（硬停 = 边界 + 容差 + 2×节拍）:
// 防「无官方点 → 循环永不退出」的 goroutine 泄漏。
func TestRecoverAnchorHardStop(t *testing.T) {
	boundary := time.Now()
	o := opts() // Schedule 为空
	o.PickTol = 20 * time.Millisecond
	el := waitMs(t, 15*time.Millisecond, time.Second, func() {
		drain(t, RecoverAnchor(context.Background(), fetchNil(), nopPick, boundary,
			boundary.Add(5*time.Minute), o), time.Second)
	})
	if el > 200*time.Millisecond {
		t.Fatalf("空 Schedule 应立即硬停, 实际 %v", el)
	}
}

// fetchNil 返回 nil fetcher（配合空 Schedule 用: 有 Schedule 才会调它）。
func fetchNil() OpenPriceFetcher { return nil }

// msAgo 返回 d 之前的 unix 毫秒（保留给时间戳类断言用）。
func msAgo(d time.Duration) int64 { return time.Now().Add(-d).UnixMilli() }

// waitMs 断言 f 的耗时落在 [lo, hi]（执行越界即节奏错）。
func waitMs(t *testing.T, lo, hi time.Duration, f func()) time.Duration {
	t.Helper()
	start := time.Now()
	f()
	el := time.Since(start)
	if el < lo || el > hi {
		t.Fatalf("耗时 %v 不在 [%v, %v]", el, lo, hi)
	}
	return el
}
