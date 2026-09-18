package feed

import (
	"context"
	"fmt"
	"log"
	"time"

	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// 精确取锚（2026-09-19, docs/dog020_anchor_exact_open_2026-09-19.md）: 窗口开始后
// 反复从 TWAP 推送缓存里取**评估时刻恰好等于窗口边界**的那一条——它就是官方
// openPrice（实测 8/8 窗逐位相同, 差 ≤0.0006bps ≈ 3 厘美元）, 也是本策略唯一的锚口径。
//
// 为什么不再用官方 HTTP（需求 2026-09-19）: 官方 open 接口要 **+40s 才收敛**
// （头几十秒返回的是收敛中的临时值: 同窗 +2.8s/+10.3s 同为 80721.68 而 +40.4s 才
// 跳到 80722.35; 17 窗配对 |差| p90 1.18bps ≈ 9.5 美元, 比它要修的 t=0 误差还大）,
// 而本策略的信号全部落在窗口前 2 分钟（rem > 180; 回测最早一笔 rem=295 = 边界后 5s）
// ——等官方就是错过整窗。官方取数工具（NewOpenPriceFetcher / OpenPriceFetcher）与
// 收敛门（SettleAfter）保留在本文件, **默认不接线**: AnchorUpgradeOpts.Schedule
// 为空即整段跳过, 将来若允许 40s+ 延迟把 Schedule 与 fetcher 接回来即可。
//
// 时间预算: 边界那一秒的推送实测 p50 +2.0s 到达（p90 +2.3s; 观测到最晚 +12.1s）,
// 故默认 500ms 一次 × 40 次 = 20s（p50 的 10 倍余量）。命中即终局——精确匹配下
// 不可能再有更好的取值。预算耗尽 = 本窗无锚: 引擎 anchor ≤0 天然只占槽、闸住触发
// 判定、不产出观测（决策 #12/#14 同款原则: 数据源不可信 → 本窗不观测）。
//
// 调用方必须在**独立 goroutine** 里消费: 本函数阻塞到通道关闭为止。取锚**绝不阻塞
// 主循环**——主循环照常每秒 tick、照常推进窗口, 锚到手与否只影响判定是否放行。
func RecoverAnchor(ctx context.Context, fetch OpenPriceFetcher, pick PushPicker,
	windowStart, windowEnd time.Time, opts AnchorUpgradeOpts) <-chan AnchorRecovery {

	every := opts.Interval
	if every <= 0 {
		every = anchorRetryInterval
	}
	attempts := opts.Attempts
	if attempts <= 0 {
		attempts = anchorRetryAttempts
	}
	due := make([]time.Time, len(opts.Schedule))
	for i, d := range opts.Schedule {
		due[i] = windowStart.Add(d)
	}

	ch := make(chan AnchorRecovery)
	go func() {
		defer close(ch)

		send := func(r AnchorRecovery) bool {
			select {
			case ch <- r:
				return true
			case <-ctx.Done():
				return false
			}
		}

		// ── ① 精确取锚（本窗主路径）──
		// 首次立即尝试（缓存里若已有边界那一秒的推送, 零延迟命中——测试与重放场景）,
		// 之后每 `every` 一次。命中即送出并转入官方段（若已接线）。
		haveStream := false
		if pick != nil {
			for i := 0; i < attempts; i++ {
				if i > 0 {
					select {
					case <-ctx.Done():
						return
					case <-time.After(every):
					}
				}
				price, arrivedMs, ok := pick(windowStart)
				if !ok {
					continue
				}
				haveStream = true
				if !send(AnchorRecovery{Price: price, Source: AnchorSourceStream, AtMs: arrivedMs}) {
					return
				}
				break
			}
		}

		// ── ② 官方段（默认休眠: Schedule 为空或未配 fetcher 时到此即关通道）──
		// 到点采样（跳过已流过的点、不补发迟到值）; 收敛点（SettleAfter）之前的成功值
		// 仅在**当前完全没有流值锚**时采纳（兜底, 好过整窗无锚）, 收敛点及之后一律采纳
		// 且每次覆盖（last-wins）。`haveStream` 由精确命中置位——否则会把已知精确的
		// 边界那一秒推送换成收敛中的临时值（2026-09-18 §9 修掉的正是这个 bug）。
		if len(due) == 0 || fetch == nil {
			return
		}
		next, tries, adopted := 0, 0, 0
		ticker := time.NewTicker(every)
		defer ticker.Stop()
		for {
			// 跳过已流过的到点时刻，只对最新到点请求一次
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
			if next >= len(due) {
				if adopted == 0 {
					log.Printf("[Anchor] ⚠️ 官方 open %d 次尝试均未采纳（未就绪或未到收敛点, 计划 %s）",
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

// 精确取锚的默认节拍与预算（生产 40 × 500ms = 20s; 见文件头）。
const (
	anchorRetryInterval = 500 * time.Millisecond
	anchorRetryAttempts = 40
)

// 锚来源（AnchorRecovery.Source / winstats 的 anchor_src）。
const (
	AnchorSourceOfficial = "official" // 官方 crypto-price 开盘价（官方路径休眠中, 见文件头）
	AnchorSourceStream   = "stream"   // TWAP 推送: 评估时刻恰好等于窗口边界的那条
)

// AnchorUpgradeOpts 是取锚通道的参数。
type AnchorUpgradeOpts struct {
	Attempts int           // 精确取锚的最大尝试次数（≤0 = anchorRetryAttempts）
	Interval time.Duration // 精确取锚的尝试间隔（≤0 = anchorRetryInterval; 亦作官方段的轮询节拍）
	// 以下为官方路径参数（**默认不接线**: Schedule 为空即整段跳过）。
	Schedule     []time.Duration // 官方尝试时刻（自窗口边界起算, 如 {2s,5s,10s,20s,40s}）
	SettleAfter  time.Duration   // 官方值可压过流值锚的起点（≤0 = 首个采样点起即可采纳）
	FetchTimeout time.Duration   // 单次官方请求超时（≤0 = 不额外限时）
}

// AnchorRecovery 是一次取锚结果。
type AnchorRecovery struct {
	Price  float64 // 锚值
	Source string  // official | stream
	AtMs   int64   // 该值的可用时刻（unix 毫秒）: stream = 推送的**本地到达时刻**
	//               （= 发布延迟的无偏观测量, 不带轮询相位）, official = 请求返回时刻
}

// OpenPriceFetcher 拉取指定窗口的官方开盘价（≤0 = 数据未就绪/请求失败）。
type OpenPriceFetcher func(ctx context.Context, windowStart, windowEnd time.Time) float64

// PushPicker 取 TWAP 推送缓存里评估时刻**恰好等于** windowStart 的那条
// （见 TwapAdapter.PushNearest）。
type PushPicker func(windowStart time.Time) (price float64, arrivedMs int64, ok bool)

// NewOpenPriceFetcher 构造官方开盘价取数器（Polymarket crypto-price 接口）。
// symbol / variant / 回看秒数在此固定，调用方只负责构造注入——HTTP 取数细节不再
// 散落在 cmd/flip（2026-09-18 由 main 下沉到本包）。**当前未接线**（见文件头）。
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
