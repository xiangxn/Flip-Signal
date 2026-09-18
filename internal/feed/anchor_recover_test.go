// 2026-09-19 精确取锚通道测试（docs/dog020_anchor_exact_open_2026-09-19.md）。
// 全部注入假 fetcher / 假缓存取数（零网络）；节拍与预算都压到毫秒级（opts），
// 用「调用时刻相对边界」核验节奏，不真实等待 +2/+5/+40s。
package feed

import (
	"context"
	"sync"
	"testing"
	"time"
)

// seqFetch 返回按调用次序取值的假 fetcher（越界复用最后一个）与调用时刻记录。
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

// pickStep 是脚本化取锚的一条: 价格 + 该条的本地到达时刻（unix 毫秒）;
// miss=true 表示这一次没命中（边界那一秒的推送还没到）。
type pickStep struct {
	price     float64
	arrivedMs int64
	miss      bool
}

// scriptPick 按调用次序给出结果（每调用一次消耗一条, 越界后恒不命中）。
func scriptPick(seq []pickStep) (PushPicker, *[]time.Time) {
	var mu sync.Mutex
	var calls []time.Time
	i := 0
	return func(_ time.Time) (float64, int64, bool) {
		mu.Lock()
		defer mu.Unlock()
		calls = append(calls, time.Now())
		if i >= len(seq) {
			return 0, 0, false
		}
		s := seq[i]
		i++
		if s.miss {
			return 0, 0, false
		}
		return s.price, s.arrivedMs, true
	}, &calls
}

// nopPick 恒不命中（缓存里没有边界那一秒的条目）。
func nopPick(time.Time) (float64, int64, bool) { return 0, 0, false }

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

// opts 造一份毫秒级节拍的测试参数: 精确取锚默认只试 1 次（立即, 不等待）——
// 官方段用例要的是「没有流值锚」这一前提, 不想被重试预算拖时间。
func opts(sched ...time.Duration) AnchorUpgradeOpts {
	return AnchorUpgradeOpts{
		Attempts:     1,
		Interval:     2 * time.Millisecond,
		Schedule:     sched,
		FetchTimeout: 200 * time.Millisecond,
	}
}

// TestRecoverAnchorExactHitIsFinal 命中即终局: 只送一条（stream）, 通道立刻关闭,
// 不再消耗剩余预算（精确匹配下不可能有更好的取值）。
func TestRecoverAnchorExactHitIsFinal(t *testing.T) {
	boundary := time.Now()
	arrived := boundary.Add(2 * time.Second).UnixMilli()
	// 前两次未命中（边界那一秒还没到）, 第三次命中
	pick, calls := scriptPick([]pickStep{{miss: true}, {miss: true}, {price: 98_500, arrivedMs: arrived}})
	o := opts()
	o.Attempts, o.Interval = 20, 10*time.Millisecond // 预算 190ms; 命中在第 3 次（~20ms）

	var got []AnchorRecovery
	el := waitMs(t, 15*time.Millisecond, 100*time.Millisecond, func() {
		got = drain(t, RecoverAnchor(context.Background(), nil, pick, boundary,
			boundary.Add(5*time.Minute), o), 2*time.Second)
	})

	if len(got) != 1 || got[0].Source != AnchorSourceStream || got[0].Price != 98_500 {
		t.Fatalf("应只送一条 stream 结果, 得到 %+v", got)
	}
	if got[0].AtMs != arrived {
		t.Fatalf("AtMs 应为该推送的本地到达时刻 %d, 得到 %d", arrived, got[0].AtMs)
	}
	if n := len(*calls); n != 3 {
		t.Fatalf("命中后不应继续取, 取数次数 = %d（期望 3）", n)
	}
	if el > 100*time.Millisecond {
		t.Fatalf("命中后应立即关通道（不耗完预算）, 实际 %v", el)
	}
}

// TestRecoverAnchorBudgetExhausted 预算耗尽: 恰好 Attempts 次取数、按 Interval 节拍、
// 一条不送、通道关闭（调用方保留 anchor=0 → 本窗不产出观测）。
func TestRecoverAnchorBudgetExhausted(t *testing.T) {
	boundary := time.Now()
	pick, calls := scriptPick(nil) // 永不命中
	o := opts()
	o.Attempts, o.Interval = 5, 10*time.Millisecond // 首次立即 + 4 次等待 = ~40ms

	var got []AnchorRecovery
	el := waitMs(t, 30*time.Millisecond, 200*time.Millisecond, func() {
		got = drain(t, RecoverAnchor(context.Background(), nil, pick, boundary,
			boundary.Add(5*time.Minute), o), 2*time.Second)
	})

	if len(got) != 0 {
		t.Fatalf("预算耗尽不应送出任何结果, 得到 %+v", got)
	}
	if n := len(*calls); n != 5 {
		t.Fatalf("应取数 %d 次, 得到 %d", 5, n)
	}
	for i, c := range *calls {
		want := time.Duration(i) * 10 * time.Millisecond
		if d := c.Sub(boundary.Add(want)); d < -10*time.Millisecond || d > 40*time.Millisecond {
			t.Fatalf("第 %d 次取数距边界 %v, 期望 %v", i, d, want)
		}
	}
	if el > 150*time.Millisecond {
		t.Fatalf("预算耗尽后应立即关通道, 实际 %v", el)
	}
}

// TestRecoverAnchorOfficialDormant 官方路径默认休眠: Schedule 为空时——即使接了
// fetcher——一次请求都不发（需求: 官方 +40s 才收敛, 本窗机会早过）。
func TestRecoverAnchorOfficialDormant(t *testing.T) {
	boundary := time.Now()
	fetch, calls := seqFetch(99_000)
	pick, _ := scriptPick([]pickStep{{price: 98_500, arrivedMs: boundary.UnixMilli()}})

	got := drain(t, RecoverAnchor(context.Background(), fetch, pick, boundary,
		boundary.Add(5*time.Minute), opts() /* 空 Schedule */), 2*time.Second)

	if n := len(*calls); n != 0 {
		t.Fatalf("官方路径应休眠（0 次请求）, 得到 %d 次", n)
	}
	if len(got) != 1 || got[0].Source != AnchorSourceStream {
		t.Fatalf("应只送精确取锚结果, 得到 %+v", got)
	}
}

// TestRecoverAnchorOfficialLastWins（官方段, 休眠路径的回归）: **每次成功都采纳**
// （last-wins）——官方值本身在头十几秒仍在收敛, 最后一次采样即最终锚。
// SettleAfter=0 = 无收敛门。
func TestRecoverAnchorOfficialLastWins(t *testing.T) {
	boundary := time.Now()
	fetch, calls := seqFetch(99_000, 99_200, 99_300) // 三点各返回不同价（模拟收敛）
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

// TestRecoverAnchorSettleGuard 收敛门: 已有精确锚时, 收敛点之前的官方成功值只作兜底、
// 不覆盖流值（否则会把已知精确的边界那一秒推送换成收敛中的临时值）。
func TestRecoverAnchorSettleGuard(t *testing.T) {
	boundary := time.Now()
	sched := []time.Duration{10 * time.Millisecond, 30 * time.Millisecond, 60 * time.Millisecond}

	// ① 精确取锚已命中 → +10ms/+30ms 两点丢弃, +60ms（≥ 收敛点 50ms）采纳
	o := opts(sched...)
	o.SettleAfter = 50 * time.Millisecond
	fetch, _ := seqFetch(99_000, 99_200, 99_300)
	pick, _ := scriptPick([]pickStep{{price: 98_500, arrivedMs: boundary.UnixMilli()}})
	got := drain(t, RecoverAnchor(context.Background(), fetch, pick, boundary,
		boundary.Add(5*time.Minute), o), 2*time.Second)
	if len(got) != 2 || got[0].Source != AnchorSourceStream ||
		got[1].Source != AnchorSourceOfficial || got[1].Price != 99_300 {
		t.Fatalf("收敛门内应只采纳最后一个采样点（99300）, 得到 %+v", got)
	}

	// ② 无精确锚（缓存不命中）→ 早期点照常采纳（兜底, 好过整窗无锚）
	// 注意必须换新 boundary: ① 已跑掉 ~60ms, 复用会把三个采样点压成「一次性」。
	boundary2 := time.Now()
	o2 := opts(sched...)
	o2.SettleAfter = 50 * time.Millisecond
	fetch2, _ := seqFetch(99_000, 99_200, 99_300)
	got2 := drain(t, RecoverAnchor(context.Background(), fetch2, nopPick, boundary2,
		boundary2.Add(5*time.Minute), o2), 2*time.Second)
	if len(got2) != 3 {
		t.Fatalf("无精确锚时早期官方值应作兜底全部采纳, 得到 %+v", got2)
	}
}

// TestRecoverAnchorAllFail 官方全失败: 每个 Schedule 点各试一次（±40ms 节奏），
// 一条都不送出、通道关闭。
func TestRecoverAnchorAllFail(t *testing.T) {
	boundary := time.Now()
	sched := []time.Duration{10 * time.Millisecond, 30 * time.Millisecond, 60 * time.Millisecond}
	fetch, calls := seqFetch(0)
	var got []AnchorRecovery
	waitMs(t, 50*time.Millisecond, 2*time.Second, func() {
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

// TestRecoverAnchorContextCancel 窗口收尾取消 ctx → 立即关通道、不 panic、
// 不再发起后续取数（首个官方点已失败, 后点未到）。
func TestRecoverAnchorContextCancel(t *testing.T) {
	boundary := time.Now()
	fetch, calls := seqFetch(0)
	pick, pickCalls := scriptPick(nil)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		time.Sleep(20 * time.Millisecond)
		cancel()
	}()
	var got []AnchorRecovery
	waitMs(t, 0, 500*time.Millisecond, func() {
		got = drain(t, RecoverAnchor(ctx, fetch, pick, boundary,
			boundary.Add(5*time.Minute), opts(2*time.Millisecond, time.Hour)), time.Second)
	})
	if len(got) != 0 {
		t.Fatalf("取消前不应有结果: %+v", got)
	}
	if n := len(*calls); n != 1 {
		t.Fatalf("取消前只应发出首个点的官方请求, 得到 %d", n)
	}
	// 精确取锚在预算内每秒(2ms)一直未命中, 取消即停（远小于预算 40 次）
	if n := len(*pickCalls); n >= anchorRetryAttempts {
		t.Fatalf("ctx 取消应立即停取, 取数次数 = %d", n)
	}
}

// TestRecoverAnchorNilFetch 未配 fetcher（nil）不 panic: 精确取锚照常工作, 通道照常关闭。
func TestRecoverAnchorNilFetch(t *testing.T) {
	boundary := time.Now()
	pick, _ := scriptPick([]pickStep{{price: 98_500, arrivedMs: boundary.UnixMilli()}})
	waitMs(t, 0, 500*time.Millisecond, func() {
		got := drain(t, RecoverAnchor(context.Background(), nil, pick, boundary,
			boundary.Add(5*time.Minute), opts(5*time.Millisecond)), time.Second)
		if len(got) != 1 {
			t.Fatalf("nil fetcher 不应影响精确取锚: %+v", got)
		}
	})
}

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
