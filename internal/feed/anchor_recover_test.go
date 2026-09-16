// 2026-09-16 锚恢复编排测试（docs/dog020_anchor_recovery_2026-09-16.md）。
// 全部注入假 fetcher / 假推送源（零网络），时长取毫秒级——重试节奏用「调用时刻
// 相对边界」核验，不依赖真实等待 20s。
package feed

import (
	"context"
	"testing"
	"time"
)

// seqFetch 返回按调用次序取值的假 fetcher（越界复用最后一个）与调用时刻记录。
func seqFetch(prices ...float64) (OpenPriceFetcher, *[]time.Time) {
	var calls []time.Time
	i := 0
	return func(context.Context) float64 {
		calls = append(calls, time.Now())
		p := prices[min(i, len(prices)-1)]
		i++
		return p
	}, &calls
}

// fixedPush 返回恒定推送值/到达时刻的假推送源。
func fixedPush(price float64, atMs int64) PushSource {
	return func() (float64, int64) { return price, atMs }
}

func msAgo(d time.Duration) int64 { return time.Now().Add(-d).UnixMilli() }

// waitMs 断言恢复耗时落在 [lo, hi]（执行越界即节奏错）。
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

// TestRecoverAnchorOfficialFirst 首次官方请求即成功: 只发一次请求、不空跑剩余尝试。
func TestRecoverAnchorOfficialFirst(t *testing.T) {
	boundary := time.Now()
	fetch, calls := seqFetch(99_000)
	var r AnchorRecovery
	var ok bool
	waitMs(t, 0, 500*time.Millisecond, func() {
		r, ok = RecoverAnchor(context.Background(), fetch, fixedPush(98_000, msAgo(time.Minute)),
			boundary, AnchorRecoverOpts{
				Attempts: 3, Interval: time.Hour, PushGrace: 10 * time.Second, FetchTimeout: time.Second,
			})
	})
	if !ok || r.Source != AnchorSourceOfficial || r.Price != 99_000 {
		t.Fatalf("恢复结果 = %+v ok=%v, 期望 official/99000", r, ok)
	}
	if len(*calls) != 1 {
		t.Fatalf("官方请求应恰好 1 次, 得到 %d", len(*calls))
	}
	if r.AtMs < boundary.UnixMilli() {
		t.Fatalf("AtMs = %d, 早于边界 %d", r.AtMs, boundary.UnixMilli())
	}
}

// TestRecoverAnchorAllAttemptsFail 官方 3 次全失败 → 放弃，且按 i×Interval 节奏发请求
// （第三次失败即刻返回，不空等下一次）。
func TestRecoverAnchorAllAttemptsFail(t *testing.T) {
	boundary := time.Now()
	fetch, calls := seqFetch(0)
	var ok bool
	waitMs(t, 100*time.Millisecond, time.Second, func() {
		_, ok = RecoverAnchor(context.Background(), fetch, nil, boundary, AnchorRecoverOpts{
			Attempts: 3, Interval: 60 * time.Millisecond, PushGrace: 10 * time.Second, FetchTimeout: 50 * time.Millisecond,
		})
	})
	if ok {
		t.Fatal("3 次全失败应返回 ok=false")
	}
	if len(*calls) != 3 {
		t.Fatalf("官方应恰好尝试 3 次, 得到 %d", len(*calls))
	}
	for i := 0; i < len(*calls); i++ {
		// 第 i 次在边界后 i×Interval（±40ms 容差, 覆盖调度抖动）
		want := boundary.Add(time.Duration(i) * 60 * time.Millisecond)
		if d := (*calls)[i].Sub(want); d < -40*time.Millisecond || d > 40*time.Millisecond {
			t.Fatalf("第 %d 次请求距边界 %v, 期望 %v（±40ms）", i, d, want.Sub(boundary))
		}
	}
}

// TestRecoverAnchorPushWithinGrace 边界窄窗口内的推送直接采纳，不再发官方请求；
// 边界前的陈旧推送（触发锚缺失的那条）与超窗口推送一律不采纳。
func TestRecoverAnchorPushWithinGrace(t *testing.T) {
	boundary := time.Now()
	cases := []struct {
		name  string
		atMs  int64
		grace time.Duration
		want  bool
	}{
		{"边界后 +2s（窗口内）", boundary.Add(2 * time.Second).UnixMilli(), 10 * time.Second, true},
		{"恰在边界", boundary.UnixMilli(), 10 * time.Second, true},
		{"边界前 −3s（边界守卫同容差）", boundary.Add(-3 * time.Second).UnixMilli(), 10 * time.Second, true},
		{"边界前 −30s（陈旧推送, 必须拒）", boundary.Add(-30 * time.Second).UnixMilli(), 10 * time.Second, false},
		{"边界后 +30s（超窗口, 必须拒）", boundary.Add(30 * time.Second).UnixMilli(), 10 * time.Second, false},
		{"边界后 +15s（超 10s 窗口）", boundary.Add(15 * time.Second).UnixMilli(), 10 * time.Second, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			fetch, _ := seqFetch(99_000) // 官方可用: 推送被拒时兜底成功
			r, ok := RecoverAnchor(context.Background(), fetch, fixedPush(98_000, c.atMs),
				boundary, AnchorRecoverOpts{
					Attempts: 1, Interval: 50 * time.Millisecond,
					PushGrace: c.grace, FetchTimeout: 50 * time.Millisecond,
				})
			if !ok {
				t.Fatal("应恢复成功（推送或官方）")
			}
			if got := r.Source == AnchorSourcePush; got != c.want {
				t.Fatalf("source = %s（期望推送可用=%v）", r.Source, c.want)
			}
			if c.want && (r.Price != 98_000 || r.AtMs != c.atMs) {
				t.Fatalf("推送结果 = %+v, 期望价 98000 / 到达 %d", r, c.atMs)
			}
		})
	}
}

// TestRecoverAnchorPushDuringWait 首次官方失败后、等待期间推送到位 → 采纳推送
// （「同时等 twap 的推送」：不必等满 20s 重试间隔）。
func TestRecoverAnchorPushDuringWait(t *testing.T) {
	boundary := time.Now()
	ready := make(chan struct{})
	go func() {
		time.Sleep(20 * time.Millisecond)
		close(ready) // 推送在等待期到位（channel 同步, -race 干净）
	}()
	push := func() (float64, int64) {
		select {
		case <-ready:
			return 98_000, boundary.Add(50 * time.Millisecond).UnixMilli()
		default:
			return 0, 0
		}
	}
	fetch, calls := seqFetch(0) // 官方恒失败

	var r AnchorRecovery
	var ok bool
	waitMs(t, 0, 500*time.Millisecond, func() {
		r, ok = RecoverAnchor(context.Background(), fetch, push, boundary, AnchorRecoverOpts{
			Attempts: 3, Interval: 200 * time.Millisecond, PushGrace: 10 * time.Second, FetchTimeout: 20 * time.Millisecond,
		})
	})
	if !ok || r.Source != AnchorSourcePush || r.Price != 98_000 {
		t.Fatalf("恢复结果 = %+v ok=%v, 期望 push/98000", r, ok)
	}
	if len(*calls) != 1 {
		t.Fatalf("推送在等待期到位, 官方应只失败过 1 次, 得到 %d", len(*calls))
	}
}

// TestRecoverAnchorContextCancel 窗口结束/进程退出取消 ctx → 立即返回，不等下一次尝试。
func TestRecoverAnchorContextCancel(t *testing.T) {
	boundary := time.Now()
	fetch, calls := seqFetch(0)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		time.Sleep(30 * time.Millisecond)
		cancel()
	}()
	var ok bool
	waitMs(t, 0, 500*time.Millisecond, func() {
		_, ok = RecoverAnchor(ctx, fetch, nil, boundary, AnchorRecoverOpts{
			Attempts: 3, Interval: time.Hour, PushGrace: 10 * time.Second, FetchTimeout: time.Second,
		})
	})
	if ok {
		t.Fatal("ctx 取消应返回 ok=false")
	}
	if len(*calls) != 1 {
		t.Fatalf("取消前只应发出首次请求, 得到 %d", len(*calls))
	}
}

// TestRecoverAnchorNilFetch 未配 fetcher（nil）不 panic，按失败处理。
func TestRecoverAnchorNilFetch(t *testing.T) {
	var ok bool
	waitMs(t, 0, 300*time.Millisecond, func() {
		_, ok = RecoverAnchor(context.Background(), nil, nil, time.Now(), AnchorRecoverOpts{
			Attempts: 2, Interval: 20 * time.Millisecond, PushGrace: time.Second,
		})
	})
	if ok {
		t.Fatal("无 fetcher 应返回 ok=false")
	}
}
