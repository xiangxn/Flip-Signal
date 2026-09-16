package feed

import (
	"context"
	"time"
)

// 锚恢复（2026-09-16，docs/dog020_anchor_recovery_2026-09-16.md）: 窗口边界 TWAP
// 采样缺失/陈旧时，用官方开盘价重试 + 推送窄窗口直采把本窗救回来。
//
// 为什么: 边界一次推送抖动（断流/重建间隙）就会让 anchor 置 0 → 引擎整窗不产出
// 观测，白丢一个 5 分钟窗口——而官方 crypto-price 的 openPrice 恰是窗口边界真值
// （与回测 anchor 同口径），通常几十秒内就绪；TWAP 推送也可能随即恢复。
//
// 取值优先级 = **官方 open 优先，推送只做窄窗口直采**（|到达时刻 − 边界| ≤ PushGrace）:
// 迟到 30s 的推送值是边界后某一时刻的滚动值，在 σ≈6~9bps 尺度下可把 dist_s 带偏
// ~0.3-0.5σ，足以改变浅洞带的进出——宁可不要这个窗口，也不要一个偏锚的信号。

// 锚恢复来源（AnchorRecovery.Source）。
const (
	AnchorSourceOfficial = "official" // 官方 crypto-price 开盘价（= 边界真值）
	AnchorSourcePush     = "push"     // TWAP 推送（仅边界窄窗口内到达者）
)

// pushProbeInterval 是恢复期轮询推送的间隔: 推送通道无事件语义（适配器只保留
// 最新值 + 到达时刻），只能按粒度探测——与引擎 tick 同为 1s。
const pushProbeInterval = time.Second

// AnchorRecoverOpts 是锚恢复的重试参数。
type AnchorRecoverOpts struct {
	Attempts     int           // 官方开盘价最大尝试次数（含首次；≤0 按 1 处理）
	Interval     time.Duration // 相邻尝试间隔: 第 i 次在边界后 i×Interval（0 = 各次连续发出）
	PushGrace    time.Duration // 推送可用窗口: |到达时刻 − 窗口边界| ≤ 此值
	FetchTimeout time.Duration // 单次官方请求超时（≤0 = 不额外限时）
}

// AnchorRecovery 是一次成功的锚恢复结果。
type AnchorRecovery struct {
	Price  float64 // 锚值
	Source string  // official | push
	AtMs   int64   // 恢复时刻（unix 毫秒；用于统计边界后多久拿到）
}

// OpenPriceFetcher 拉取本窗官方开盘价（≤0 = 数据未就绪/请求失败）。
type OpenPriceFetcher func(ctx context.Context) float64

// PushSource 读取最近一条 TWAP 推送的值与本地到达时刻（unix 毫秒；无推送返回 0, 0）。
type PushSource func() (price float64, arrivedAtMs int64)

// RecoverAnchor 在窗口边界锚缺失后尝试恢复锚值，成功返回 (结果, true)。
//
// 循环（1s 粒度轮询推送，避免忙等）:
//  1. 推送直采: 到达时刻距边界 ≤ PushGrace 的推送立即采纳——**取绝对值**，边界前
//     到达的陈旧推送同样被挡（否则触发锚缺失的那条老推送会被立刻误判成恢复成功）；
//     也覆盖「边界前 ±grace 内」的推送，与边界守卫 AnchorUsableAtBoundary 同容差。
//  2. 到点（边界后第 i×Interval 秒）发一次官方请求，带独立 FetchTimeout 子 ctx 兜底
//     （SDK 内部 429 Retry-After 睡眠可拖数分钟，v3 排障记录实测把写盘拖了 ~5 分钟）。
//  3. 尝试次数用尽立即返回 false，不空等: PushGrace 内的推送早已在第 1 步被看到，
//     再等只能拿到超窗口的偏锚值。ctx 取消（窗口结束/进程退出）同样即刻返回。
func RecoverAnchor(ctx context.Context, fetch OpenPriceFetcher, push PushSource,
	windowStart time.Time, opts AnchorRecoverOpts) (AnchorRecovery, bool) {

	attempts := opts.Attempts
	if attempts < 1 {
		attempts = 1
	}
	boundaryMs := windowStart.UnixMilli()
	graceMs := opts.PushGrace.Milliseconds()

	// pushOK 判定最近一条推送是否落在边界窄窗口内（见上文第 1 点）。
	pushOK := func() (float64, int64, bool) {
		if push == nil {
			return 0, 0, false
		}
		price, atMs := push()
		if !(price > 0) || atMs <= 0 {
			return 0, 0, false
		}
		diff := atMs - boundaryMs
		if diff < 0 {
			diff = -diff
		}
		if diff > graceMs {
			return 0, 0, false
		}
		return price, atMs, true
	}

	// 首次尝试在边界当场发出（与 v3 的 PollOfficialOpenPrice 同款）: 接口数据就绪
	// 本身有延迟，早探一次成本极低（失败才计一次尝试）。
	for i := 0; i < attempts; i++ {
		// 到点前等待: 每秒探一次推送，可被 ctx 取消。
		for {
			if ctx.Err() != nil {
				return AnchorRecovery{}, false
			}
			if price, atMs, ok := pushOK(); ok {
				return AnchorRecovery{Price: price, Source: AnchorSourcePush, AtMs: atMs}, true
			}
			due := time.UnixMilli(boundaryMs + int64(i)*opts.Interval.Milliseconds())
			wait := time.Until(due)
			if wait <= 0 {
				break
			}
			select {
			case <-ctx.Done():
				return AnchorRecovery{}, false
			case <-time.After(min(wait, pushProbeInterval)):
			}
		}

		if price := fetchWithTimeout(ctx, fetch, opts.FetchTimeout); price > 0 {
			return AnchorRecovery{
				Price:  price,
				Source: AnchorSourceOfficial,
				AtMs:   time.Now().UnixMilli(),
			}, true
		}
	}
	return AnchorRecovery{}, false
}

// fetchWithTimeout 以 timeout 为上限调用官方拉取（timeout ≤0 或 fetcher 缺省时不限时）。
func fetchWithTimeout(ctx context.Context, fetch OpenPriceFetcher, timeout time.Duration) float64 {
	if fetch == nil {
		return 0
	}
	if timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, timeout)
		defer cancel()
	}
	return fetch(ctx)
}
