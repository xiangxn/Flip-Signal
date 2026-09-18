package feed

import (
	"context"
	"fmt"
	"log"
	"math"
	"time"

	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// 锚升级（2026-09-18, docs/dog020_anchor_upgrade_2026-09-18.md）: 窗口开始后把本窗
// 锚从「边界瞬间最新到达的推送」升级到更可信来源——缓存里评估时刻最贴边界的那条
// （stream），或官方 crypto-price 的开盘价（official，权威值）。
//
// 为什么: 服务器发布延迟 ~1.0-1.5s，边界当场只能取到「边界前那一秒」的评估值，
// 与官方 open（= 边界那一秒的评估值）差 p90 0.24bps、|差|>0.5bps 占 7.3%；而锚是
// 浅洞腿的分母基准兼结算线，口径必须对齐。本通道同时取代 09-16 的「锚缺失才恢复」
// 逻辑——升级是每窗都跑的主路径，锚缺失窗口顺带被它救回（流值/官方任一可用即可）。
//
// 优先级 = **官方 > 缓存重选**——但官方**只在收敛之后**才压过流值（2026-09-18 §9 实测）:
// 官方 open 接口在边界后头几十秒返回的是**还在收敛中的临时值**（同一窗口 +6.9s 取到
// 80718.92、+33.3s 与 +62.9s 都是 80721.40；另一窗 +2.8s/+10.3s 同为 80721.68、+40.4s
// 才跳到 80722.35。17 窗配对检验里早期值 vs 收敛值 13/17 不同, |差| p90 1.18bps ≈ 9.5 美元
// —— 比它要修掉的 t=0 误差 p90 0.59bps 还大）。而**「评估时刻 == 边界」的那条推送与
// 收敛官方只差 0.0002-0.0006bps（3 厘美元）= 同一个值**。故官方采样点分两类
// （opts.SettleAfter 为界, 生产取 +40s = 最后一个采样点）:
//   - 收敛点之前: 仅作**无流值时的兜底**——已有流值锚（t=0 初值或任一重选命中）时不采纳，
//     否则会把已知精确的边界那一秒推送换成未收敛的临时值。
//   - 收敛点及之后: 一律采纳，且**每次都采纳**（last-wins）——最后一次成功即最终锚，
//     取代原「首次成功即终局」（那会把 +2s 的临时值锁成终值）。
// 缓存重选是本地零成本路径，按「评估时刻偏移单调下降」逐秒逼近边界那一秒。

// 锚升级来源（AnchorRecovery.Source）。
const (
	AnchorSourceOfficial = "official" // 官方 crypto-price 开盘价（= 边界真值; 收敛点后每次覆盖）
	AnchorSourceStream   = "stream"   // TWAP 推送缓存重选（评估时刻更贴边界）
)

// anchorPickInterval 是升级通道的轮询节拍: 缓存重选每秒一次（推送频率 1 条/s，
// 边界那一秒的推送 p90 在 +2.28s 才到达，定点单采会漏——必须连续取），
// 官方尝试也挂在这个节拍上（到点判定另行按绝对时刻算）。
const anchorPickInterval = time.Second

// AnchorUpgradeOpts 是锚升级通道的参数。
type AnchorUpgradeOpts struct {
	Schedule     []time.Duration // 官方尝试时刻（自窗口边界起算, 如 {2s,5s,10s,20s,40s}）
	SeedPickMs   int64           // t=0 初值的评估偏移（单调门基准; SeedOK=false 时忽略）
	SeedOK       bool            // t=0 是否已有可用初值（false = 无基准, 首条命中即胜出）
	SettleAfter  time.Duration   // 官方值可压过流值锚的起点（≤0 = 首个采样点起即可采纳）
	PickTol      time.Duration   // 缓存重选容差: |评估偏移| ≤ 此值才算可用
	FetchTimeout time.Duration   // 单次官方请求超时（≤0 = 不额外限时）
	PickInterval time.Duration   // 轮询节拍（≤0 = anchorPickInterval; 仅供测试与实验, 生产用 1s）
}

// AnchorRecovery 是一次锚升级结果。
type AnchorRecovery struct {
	Price  float64 // 锚值
	Source string  // official | stream
	AtMs   int64   // 取得时刻（unix 毫秒; 用于统计边界后多久拿到）
	PickMs int64   // 该值的评估时刻相对窗口边界的偏移（毫秒, 可负; official 时无意义）
}

// OpenPriceFetcher 拉取指定窗口的官方开盘价（≤0 = 数据未就绪/请求失败）。
type OpenPriceFetcher func(ctx context.Context, windowStart, windowEnd time.Time) float64

// PushPicker 取 TWAP 推送缓存中「自带时间戳距 windowStart 最近」的一条
// （见 TwapAdapter.PushNearest）。
type PushPicker func(windowStart time.Time, tol time.Duration) (price float64, pickMs int64, ok bool)

// NewOpenPriceFetcher 构造官方开盘价取数器（Polymarket crypto-price 接口）。
// symbol / variant / 回看秒数在此固定，调用方只负责构造注入——HTTP 取数细节不再
// 散落在 cmd/flip（2026-09-18 由 main 下沉到本包）。
// btc-updown-5m 口径: symbol=BTC, variant=fiveminute, twapLookbackSeconds=60。
func NewOpenPriceFetcher(client *sdk.PolymarketClient, symbol sdk.CryptoPriceSymbol,
	variant sdk.CryptoPriceUint, twapLookbackSeconds int64) OpenPriceFetcher {
	return func(ctx context.Context, windowStart, windowEnd time.Time) float64 {
		if client == nil {
			return 0
		}
		open, _ := client.FetchOpenPriceContext(ctx, symbol, windowStart.UTC(),
			windowEnd.UTC(), variant, true, int(twapLookbackSeconds))
		return open
	}
}

// RecoverAnchor 在窗口开始后持续把锚升级到更可信来源，逐次经通道送出结果。
// 通道在三种情形关闭: ctx 结束（窗口收尾/进程退出）、Schedule 用尽（官方不再有新采样点，
// 无论成功与否）、硬停时刻到（缓存重选再也不可能改善）。
//
// 每拍（anchorPickInterval）两件事:
//  1. 缓存重选: 取评估时刻最贴边界的推送，仅当 |偏移| **严格小于**基准时送出
//     （Source=stream）——单调门保证重选只会让锚更贴边界，不会被重连补发/乱序的
//     旧评估值带偏；基准初值 = t=0 初值的偏移（opts.SeedPickMs）。
//  2. 官方到点: 跳过已流过的到点时刻，对最新到点请求一次；**成功不结束通道**——
//     收敛点（opts.SettleAfter）之前的成功值仅在**当前完全没有流值锚**时采纳
//     （兜底），收敛点及之后一律采纳且每次覆盖（last-wins，见文件头）。失败则等
//     下一个到点时刻（补发只会拿到早已失去意义的迟到值）。
//
// 调用方必须在**独立 goroutine** 里消费: 本函数阻塞到通道关闭为止。
func RecoverAnchor(ctx context.Context, fetch OpenPriceFetcher, pick PushPicker,
	windowStart, windowEnd time.Time, opts AnchorUpgradeOpts) <-chan AnchorRecovery {

	pickEvery := opts.PickInterval
	if pickEvery <= 0 {
		pickEvery = anchorPickInterval
	}
	due := make([]time.Time, len(opts.Schedule))
	for i, d := range opts.Schedule {
		due[i] = windowStart.Add(d)
	}
	// 硬停时刻: 缓存重选再也不可能改善之后（边界 + 容差 + 一次发布延迟）与最后一个
	// 官方点之后取晚者——保证 Schedule 为空时循环也必然终止。
	hardStop := windowStart.Add(opts.PickTol + 2*pickEvery)
	if len(due) > 0 {
		if last := due[len(due)-1].Add(opts.FetchTimeout + anchorPickInterval); last.After(hardStop) {
			hardStop = last
		}
	}

	ch := make(chan AnchorRecovery)
	go func() {
		defer close(ch)
		// 单调门基准 = 当前生效锚的 |评估偏移|; 无初值时无基准（首条命中即胜出,
		// 因为锚 ≤ 0 时引擎本来就不产出观测）。
		best := int64(math.MaxInt64)
		if opts.SeedOK {
			best = abs64(opts.SeedPickMs)
		}

		send := func(r AnchorRecovery) bool {
			select {
			case ch <- r:
				return true
			case <-ctx.Done():
				return false
			}
		}

		next, tries, adopted := 0, 0, 0 // next = 下一个待发的 Schedule 下标, adopted = 已采纳的官方值数
		haveStream := opts.SeedOK       // 是否已有可信流值锚（t=0 初值或任一重选命中）
		ticker := time.NewTicker(pickEvery)
		defer ticker.Stop()
		for {
			if !time.Now().Before(hardStop) {
				return
			}
			// ① 缓存重选（本地零成本）
			if pick != nil {
				if price, pickMs, ok := pick(windowStart, opts.PickTol); ok && abs64(pickMs) < best {
					best, haveStream = abs64(pickMs), true
					if !send(AnchorRecovery{Price: price, Source: AnchorSourceStream,
						AtMs: time.Now().UnixMilli(), PickMs: pickMs}) {
						return
					}
				}
			}
			// ② 官方到点（跳过已流过的到点时刻, 只对最新到点请求一次）
			k := next
			for k < len(due) && !time.Now().Before(due[k]) {
				k++
			}
			if k > next {
				off := due[k-1].Sub(windowStart) // 本次实际对应的采样点（可能因卡顿晚于计划）
				next, tries = k, tries+1
				if price := fetchWithTimeout(ctx, fetch, opts.FetchTimeout,
					windowStart, windowEnd); price > 0 {
					if opts.SettleAfter > 0 && off < opts.SettleAfter && haveStream {
						log.Printf("[Anchor] 官方 %+v 采样值（边界后 +%v）未到收敛点 %+v, 保留流值锚",
							off, time.Since(windowStart).Round(time.Millisecond), opts.SettleAfter)
					} else {
						adopted++
						if !send(AnchorRecovery{Price: price, Source: AnchorSourceOfficial,
							AtMs: time.Now().UnixMilli()}) {
							return
						}
					}
				}
				if ctx.Err() != nil {
					return
				}
			}
			if len(due) > 0 && next >= len(due) {
				if adopted == 0 {
					log.Printf("[Anchor] ⚠️ 官方 open %d 次尝试均未采纳（未就绪或未到收敛点, 计划 %s），本窗锚保持流值",
						tries, scheduleStr(opts.Schedule))
				} else {
					log.Printf("[Anchor] 官方 open 采样结束: %d/%d 次采纳（计划 %s）",
						adopted, tries, scheduleStr(opts.Schedule))
				}
				return
			}
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
			}
		}
	}()
	return ch
}

// fetchWithTimeout 以 timeout 为上限调用官方拉取（timeout ≤0 或 fetcher 缺省时不限时）。
func fetchWithTimeout(ctx context.Context, fetch OpenPriceFetcher, timeout time.Duration,
	windowStart, windowEnd time.Time) float64 {
	if fetch == nil {
		return 0
	}
	if timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, timeout)
		defer cancel()
	}
	return fetch(ctx, windowStart, windowEnd)
}

// scheduleStr 把尝试时刻格式化成 "+2s/+5s/+10s"（仅用于日志）。
func scheduleStr(sched []time.Duration) string {
	s := ""
	for i, d := range sched {
		if i > 0 {
			s += "/"
		}
		s += fmt.Sprintf("+%v", d)
	}
	return s
}

// abs64 返回 int64 的绝对值。
func abs64(v int64) int64 {
	if v < 0 {
		return -v
	}
	return v
}
