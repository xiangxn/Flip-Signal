package feed

import (
	"context"
	"fmt"
	"log"
	"math"
	"strings"
	"sync"
	"time"

	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// TwapAdapter 封装 SDK CryptoPriceMonitor 的 Chainlink TWAP 订阅，
// 维护指定 symbol 的 TWAP 最新值与推送新鲜度，供引擎诊断采样。
//
// btc-updown-5m 市场以 Chainlink TWAP-60（60 秒滚动窗口）判定胜负，
// 因此本适配器只保留 60s 窗口的推送（30s 窗口与其它 symbol 直接丢弃）。
//
// 重连策略：SDK 只处理连接层重连（断开自动重连并重订阅），连接正常但
// 推送断流（服务器/代理静默丢流）时只有数据面监控能发现——超过 maxStale
// 未收到推送即重建订阅（取消旧连接 + 新建 monitor + 热替换推送通道），
// 重建后有同长宽限期防抖（2026-09-01 服务器实测 twap_age 持续增长，
// 重启进程才恢复，见本注释）。
type TwapAdapter struct {
	symbol    string
	windowSec int64

	client  *sdk.PolymarketClient // 重建订阅用（nil 时看门狗仅告警不重建）
	symbols []string              // 重建订阅的 symbol 列表（含窗口后缀）

	maxStale      time.Duration // 推送新鲜度阈值：超过则重建订阅（0 不启用）
	checkInterval time.Duration // 看门狗检查间隔

	mu           sync.RWMutex
	price        float64
	lastUpdateAt int64      // 最近一次有效推送的本地到达时间（unix 毫秒）
	pushes       []twapPush // 推送环形缓存（见 twapPushCap / PushNearest）
	tsFallback   bool       // 已有条目因缺 payload.timestamp 回退到本地到达时刻（日志只打一次）

	updates <-chan sdk.ExternalPrice        // 当前推送通道（consume goroutine 内热替换）
	swapCh  chan (<-chan sdk.ExternalPrice) // 热替换通道（括号防 <-chan 歧义解析）
	staleCh chan struct{}                   // monitor.Run 异常退出通知（重建管理用）

	// 生命周期状态（manage goroutine 独占访问）
	lc *twapLifecycle
}

// twapLifecycle 是一个 monitor 会话的生命周期（重建时整体替换）。
type twapLifecycle struct {
	mCtx    context.Context
	mCancel context.CancelFunc
	monitor *sdk.CryptoPriceMonitor
}

const defaultTwapCheckInterval = 15 * time.Second

// twapPushCap 是推送环形缓存容量（条）。@1 条/s ≈ 100s，需覆盖锚升级通道的最晚
// 官方尝试点（cmd/flip `anchorRefineSchedule` 末点 +40s）外加 FetchTimeout(10s)
// 的余量——拉长 Schedule 时同步放大此值，否则边界那一秒的推送会被后面刷掉。
const twapPushCap = 100

// twapPush 是缓存的一条 TWAP-60 推送。
//
// tsMs 是 `payload.timestamp` = **TWAP 评估时刻**（服务器侧 1 秒整格, unix 毫秒），
// 与 arrivedMs（本地收到时刻）相差一个服务器发布延迟（实测 1.0~1.5s）。
// 取值必须按 tsMs 而非 arrivedMs: 窗口边界那一秒的评估值要等 ~1.6s 才到，
// 按到达时刻取只能拿到「边界前那一秒」的值（官方 open 是前者的口径）。
type twapPush struct {
	price     float64
	tsMs      int64 // 评估时刻（缺 payload.timestamp 时回退 = arrivedMs）
	arrivedMs int64 // 本地到达时刻
}

// NewTwapAdapter 创建 TWAP 适配器。client 供重建订阅用；
// maxStale 为推送新鲜度阈值（超过该时长未收到推送则重建订阅，0 不启用）。
func NewTwapAdapter(client *sdk.PolymarketClient, symbol string, windowSec int64, maxStale time.Duration) *TwapAdapter {
	a := newTwapAdapter(symbol, windowSec)
	a.client = client
	a.maxStale = maxStale
	// 重建订阅的 symbol 带窗口后缀（SDK 约定: btc 等价 btc_30，btc_60 才订阅 twap_sixty）
	a.symbols = []string{fmt.Sprintf("%s_%d", strings.ToLower(symbol), windowSec)}
	return a
}

// NewTwapAdapterWithChannel 直接从推送通道创建适配器（测试用，无重建能力）。
func NewTwapAdapterWithChannel(updates <-chan sdk.ExternalPrice, symbol string, windowSec int64) *TwapAdapter {
	a := newTwapAdapter(symbol, windowSec)
	a.updates = updates
	return a
}

func newTwapAdapter(symbol string, windowSec int64) *TwapAdapter {
	return &TwapAdapter{
		symbol:        strings.ToUpper(symbol),
		windowSec:     windowSec,
		checkInterval: defaultTwapCheckInterval,
		swapCh:        make(chan (<-chan sdk.ExternalPrice), 1),
		staleCh:       make(chan struct{}, 1),
	}
}

// Start 启动推送消费 goroutine（不管理 monitor 生命周期，测试用）。
func (t *TwapAdapter) Start(ctx context.Context) {
	go t.consume(ctx)
}

// StartWithMonitor 启动完整生命周期：消费推送 + monitor Run +
// 新鲜度看门狗（超过 maxStale 未收到推送则重建订阅）。
func (t *TwapAdapter) StartWithMonitor(ctx context.Context) {
	t.spawnRun(ctx)
	go t.consume(ctx)
	go t.manage(ctx)
}

// consume 消费推送并刷新最新值，支持经 swapCh 热替换推送通道（重建后生效）。
func (t *TwapAdapter) consume(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case ch := <-t.swapCh:
			t.updates = ch
		case ep, ok := <-t.updates:
			if !ok {
				return // SDK 推送通道永不关闭，防御性退出
			}
			if !strings.EqualFold(ep.Symbol, t.symbol) || ep.WindowSeconds != t.windowSec {
				continue
			}
			if !(ep.Price > 0) {
				continue // 非正值不入缓存（否则 PushNearest 会以 ok=true 送回 0 锚）
			}
			now := time.Now().UnixMilli()
			tsMs, fallback := ep.Timestamp, false
			if tsMs <= 0 {
				tsMs, fallback = now, true // 无评估时刻 → 本条退化为到达口径
			}
			t.mu.Lock()
			first := t.price == 0
			firstFallback := fallback && !t.tsFallback
			t.tsFallback = t.tsFallback || fallback
			t.price = ep.Price
			t.lastUpdateAt = now
			t.pushes = append(t.pushes, twapPush{price: ep.Price, tsMs: tsMs, arrivedMs: now})
			if n := len(t.pushes) - twapPushCap; n > 0 {
				t.pushes = append(t.pushes[:0], t.pushes[n:]...) // 整体前移，防底层数组无限增长
			}
			t.mu.Unlock()
			if first {
				log.Printf("[Twap] 📡 首条 TWAP-%ds 推送: %s=%.2f 评估时刻=%d（unix 毫秒）",
					t.windowSec, t.symbol, ep.Price, ep.Timestamp)
			}
			if firstFallback {
				log.Printf("[Twap] ⚠️ TWAP 推送缺 payload.timestamp，缓存条目按本地到达时刻兜底"+
					"（该窗锚精度下降，注意 winstats 的 anchor_pick_ms）")
			}
		}
	}
}

// manage 是生命周期管理 goroutine：周期检查推送新鲜度，超阈值重建订阅；
// monitor.Run 异常退出（非重建触发）同样重建。重建后进入同长宽限期，
// 给新连接订阅恢复时间，防止断流恢复期重复重建。
func (t *TwapAdapter) manage(ctx context.Context) {
	ticker := time.NewTicker(t.checkInterval)
	defer ticker.Stop()
	lastRebuild := time.Time{}
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.staleCh:
			if ctx.Err() != nil {
				return
			}
			t.rebuild(ctx, &lastRebuild)
		case <-ticker.C:
			if t.maxStale <= 0 {
				continue
			}
			_, age := t.Latest()
			if age >= int64(t.maxStale/time.Millisecond) {
				t.rebuild(ctx, &lastRebuild)
			}
		}
	}
}

// rebuild 重建 TWAP 订阅：取消旧连接（Run 内 ctx.Done → Close），
// 新建 monitor + 推送通道（Swap 热替换）+ Run goroutine。
// 距上次重建不足 maxStale 时跳过（宽限期防抖，告警同频降噪）。
func (t *TwapAdapter) rebuild(ctx context.Context, lastRebuild *time.Time) {
	if time.Since(*lastRebuild) < t.maxStale {
		return
	}
	*lastRebuild = time.Now()
	if t.client == nil {
		log.Printf("[Twap] ⚠️ TWAP 推送停更超过 %v，但未配置重建能力（client=nil）",
			t.maxStale.Round(time.Second))
		return
	}
	log.Printf("[Twap] ⚠️ TWAP 推送停更超过 %v，重建订阅", t.maxStale.Round(time.Second))
	t.spawnRun(ctx)
}

// spawnRun 启动一个 monitor 会话（初始/重建共用）：新建 monitor 并热替换
// 推送通道，Run 异常退出时通知 manage 重建。
// 仅 manage goroutine 调用（StartWithMonitor 首次调用与其构成 happen-before，
// 之后独占）。
func (t *TwapAdapter) spawnRun(ctx context.Context) {
	if t.client == nil {
		log.Printf("[Twap] ⚠️ 未配置重建能力（client=nil），跳过 monitor 启动")
		return
	}
	// 重建时先取消上一会话：SDK 的 ctx cancel 会关闭旧 WS 连接并停止推流。
	// 否则旧 monitor 的推送通道已无人消费（consume 已热替换到新通道），
	// 旧连接持续推流会把旧通道堆满，SDK 侧随后逐条打印
	// "fill channel full, dropping fill"（drop 速率 = 旧连接推送速率）。
	// 注：mCancel 2026-09-02 引入重建看门狗时即存在但从未被调用（遗漏实现），
	// 2026-09-06 修复；首次启动 t.lc 为 nil 无需取消。
	if t.lc != nil {
		t.lc.mCancel()
	}
	lc := &twapLifecycle{}
	lc.mCtx, lc.mCancel = context.WithCancel(ctx)
	lc.monitor = sdk.NewCryptoPriceMonitor(t.client, sdk.MonitorChainlinkTwap, t.symbols...)
	t.lc = lc
	t.Swap(lc.monitor.Subscribe())
	go func() {
		err := lc.monitor.Run(lc.mCtx)
		if lc.mCtx.Err() != nil {
			return // 重建/退出触发的 cancel，无需再通知
		}
		log.Printf("[Twap] ⚠️ TWAP monitor 退出: %v", err)
		select {
		case t.staleCh <- struct{}{}:
		case <-ctx.Done():
		}
	}()
}

// Swap 热替换推送通道（重建订阅后生效；consume goroutine 内切换）。
func (t *TwapAdapter) Swap(ch <-chan sdk.ExternalPrice) {
	select {
	case t.swapCh <- ch:
	default:
		log.Printf("[Twap] ⚠️ 订阅通道替换缓冲忙，丢弃替换")
	}
}

// PushNearest 返回缓存中**自带时间戳（评估时刻）**距 windowStart 最近的一条推送:
// 价格、该评估时刻相对 windowStart 的偏移（毫秒，可负）、以及是否命中。
//
// 未命中（ok=false）的三种情形: 缓存为空、最近一条的 |偏移| > tol、tol < 0。
//
// 与 Latest 的区别是选条口径: Latest 按**本地到达**给值（服务器发布延迟 1.0~1.5s，
// 窗口边界当场只能取到「边界前那一秒」的评估值），PushNearest 按**评估时刻**给值
// ——官方 open 正是边界那一秒的评估值，它在边界后 ~1.6s 才到达，故调用方要按 1s
// 粒度连续取（见 anchor_recover.go 的升级通道）。
//
// |偏移| 并列时取**后到者**（重连补发/乱序下把结果钉死为确定值）。
func (t *TwapAdapter) PushNearest(windowStart time.Time, tol time.Duration) (price float64, pickMs int64, ok bool) {
	tolMs := tol.Milliseconds()
	if tolMs < 0 {
		return 0, 0, false
	}
	target := windowStart.UnixMilli()
	best := int64(math.MaxInt64)
	t.mu.RLock()
	defer t.mu.RUnlock()
	for _, p := range t.pushes {
		d := p.tsMs - target
		if d < 0 {
			d = -d
		}
		if d <= best { // 等于时后来者覆盖 = 后到者优先
			best, price, pickMs = d, p.price, p.tsMs-target
		}
	}
	if best > tolMs {
		return 0, 0, false
	}
	return price, pickMs, true
}

// Latest 返回最新 TWAP 价格与距上次推送的毫秒数。
// 尚未收到任何推送时返回 (0, 0)。
func (t *TwapAdapter) Latest() (price float64, ageMs int64) {
	t.mu.RLock()
	defer t.mu.RUnlock()
	if t.price == 0 {
		return 0, 0
	}
	age := time.Now().UnixMilli() - t.lastUpdateAt
	if age < 0 {
		age = 0
	}
	return t.price, age
}

// FetchTwapRanges 通过 Polymarket crypto-price 接口拉取最近 windowN 个
// 完整窗口的官方 TWAP 开/收盘价，返回各窗口振幅 |close-open|（按时间升序）。
// 用于启动时预热 σ 滚动窗（cmd/flip main 的 histState），消除冷启动等待。
// twapLookbackSeconds 为 TWAP 回看窗口秒数（btc-updown-5m 为 60）。
// 个别窗口数据缺失时跳过；全部缺失时返回空切片（调用方回退冷启动）。
func FetchTwapRanges(client *sdk.PolymarketClient, windowN int, windowSec, twapLookbackSeconds int64) []float64 {
	// 对齐到最近的完整窗口边界，往前取 windowN 个已结束的窗口
	now := time.Now().UTC()
	aligned := now.Unix() / windowSec * windowSec

	ranges := make([]float64, 0, windowN)
	for k := windowN; k >= 1; k-- {
		// 相邻窗口请求起点至少间隔 1s（含失败窗——失败 continue 若跳过间隔，
		// 连续失败会退化成 ~0.2s 连发，实测反而加重上游限流）
		if k < windowN {
			time.Sleep(time.Second)
		}
		start := time.Unix(aligned-int64(k)*windowSec, 0).UTC()
		end := start.Add(time.Duration(windowSec) * time.Second)
		openPrice, closePrice := client.FetchOpenPrice(
			sdk.BTC, start, end, sdk.Fiveminute, true, int(twapLookbackSeconds))
		if openPrice <= 0 || closePrice <= 0 {
			// 该接口失败多为 Polymarket 后端上游 Chainlink 限流——上游 429 被包成
			// HTTP 400（body: Chainlink API error 429），SDK 仅对 429 重试故不生效。
			// 等 2s 重试一次（给上游喘息），仍失败才跳过。
			log.Printf("[Twap] ⚠️ 历史窗口 %s 官方价格缺失（上游限流？），2s 后重试", start.Format("15:04"))
			time.Sleep(2 * time.Second)
			openPrice, closePrice = client.FetchOpenPrice(
				sdk.BTC, start, end, sdk.Fiveminute, true, int(twapLookbackSeconds))
			if openPrice <= 0 || closePrice <= 0 {
				log.Printf("[Twap] ⚠️ 历史窗口 %s 官方价格缺失，跳过", start.Format("15:04"))
				continue
			}
		}
		ranges = append(ranges, math.Abs(closePrice-openPrice))
	}
	return ranges
}

// FetchTwapRanges 是 v4 引擎 σ 冷启动预热的唯一数据源（见上方实现）。
// 官方 open/close 逐窗轮询（PollOfficialOpenPrice/ClosePrice）随 cmd/collect
// 于 v4 分支删除——live σ 只在窗口结束追加流值 close，无需官方收盘口径。
