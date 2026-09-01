package feed

import (
	"context"
	"fmt"
	"log"
	"math"
	"math/rand"
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
	lastUpdateAt int64 // 最近一次有效推送的本地到达时间（unix 毫秒）

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
			t.mu.Lock()
			first := t.price == 0
			t.price = ep.Price
			t.lastUpdateAt = time.Now().UnixMilli()
			t.mu.Unlock()
			if first {
				log.Printf("[Twap] 📡 首条 TWAP-%ds 推送: %s=%.2f", t.windowSec, t.symbol, ep.Price)
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
// 用于启动时预热 HistRangeTracker，消除冷启动等待。
// twapLookbackSeconds 为 TWAP 回看窗口秒数（btc-updown-5m 为 60）。
// 个别窗口数据缺失时跳过；全部缺失时返回空切片（调用方回退冷启动）。
func FetchTwapRanges(client *sdk.PolymarketClient, windowN int, windowSec, twapLookbackSeconds int64) []float64 {
	// 对齐到最近的完整窗口边界，往前取 windowN 个已结束的窗口
	now := time.Now().UTC()
	aligned := now.Unix() / windowSec * windowSec

	ranges := make([]float64, 0, windowN)
	for k := windowN; k >= 1; k-- {
		start := time.Unix(aligned-int64(k)*windowSec, 0).UTC()
		end := start.Add(time.Duration(windowSec) * time.Second)
		openPrice, closePrice := client.FetchOpenPrice(
			sdk.BTC, start, end, sdk.Fiveminute, true, int(twapLookbackSeconds))
		if openPrice <= 0 || closePrice <= 0 {
			log.Printf("[Twap] ⚠️ 历史窗口 %s 官方价格缺失，跳过", start.Format("15:04"))
			continue
		}
		ranges = append(ranges, math.Abs(closePrice-openPrice))
		// crypto-price 接口限速低，逐窗口间隔 1s 防止启动时连发触发 429
		time.Sleep(time.Second)
	}
	return ranges
}

// pollOfficialOpenInterval 为开盘价轮询间隔（10s）；
// pollOfficialCloseInterval 为收盘价轮询间隔（5s，收盘产出延迟波动大，
// 更频繁轮询提高命中率）。crypto-price 接口限速低，SDK 已内部处理 429。
const (
	pollOfficialOpenInterval  = 10 * time.Second
	pollOfficialCloseInterval = 5 * time.Second
)

// pollOfficialPrice 以 interval 间隔轮询官方 crypto-price 接口，直至取得有效价格或超时。
// needClose=true 时要求 close 也有效（窗口结束后接口才产出官方收盘价）。
// ctx 取消或超时时提前返回 ok=false。返回该窗口的官方 open/close。
//
// 超时为硬约束：timeout 同时作为轮询总预算与单次请求的 context deadline
// （FetchOpenPriceContext 的请求与 429 重试等待均感知 ctx）。旧实现只在
// 两次调用之间检查 deadline，SDK 内 429 Retry-After 睡眠可让单次调用阻塞
// 数分钟，实测事件写盘被拖 ~5 分钟（见 2026-08-17 排障记录）。
func pollOfficialPrice(ctx context.Context, client *sdk.PolymarketClient, start time.Time,
	windowSec, twapLookbackSeconds int64, timeout time.Duration, needClose bool,
	interval time.Duration) (open, close float64, ok bool) {
	pollCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	deadline := time.Now().Add(timeout)
	for {
		// 先检查 deadline 再发起请求，避免超时后的多余调用
		wait := time.Until(deadline)
		if wait <= 0 {
			return 0, 0, false
		}
		open, close = client.FetchOpenPriceContext(pollCtx, sdk.BTC, start,
			start.Add(time.Duration(windowSec)*time.Second),
			sdk.Fiveminute, true, int(twapLookbackSeconds))
		if open > 0 && (!needClose || close > 0) {
			return open, close, true
		}
		// 固定间隔 + 0-2s 抖动：collect/lab/引擎都在 5 分边界对齐发起
		// 轮询，抖动避免多进程同拍并发打低限速的 crypto-price 接口触发 429
		jitter := time.Duration(rand.Int63n(int64(2 * time.Second)))
		if wait > interval+jitter {
			wait = interval + jitter
		}
		select {
		case <-ctx.Done():
			return 0, 0, false
		case <-time.After(wait):
		}
	}
}

// PollOfficialOpenPrice 从窗口起点起轮询官方 TWAP 开盘价。
// 接口有数据延迟，需从 5 分整边界开始轮询；超时或 ctx 取消时返回
// ok=false（调用方回退流采样）。
func PollOfficialOpenPrice(ctx context.Context, client *sdk.PolymarketClient, start time.Time,
	windowSec, twapLookbackSeconds int64, timeout time.Duration) (open float64, ok bool) {
	open, _, ok = pollOfficialPrice(ctx, client, start, windowSec, twapLookbackSeconds,
		timeout, false, pollOfficialOpenInterval)
	return open, ok
}

// PollOfficialClosePrice 窗口结束后轮询官方 TWAP 收盘价（close 有数据延迟，
// 产出延迟波动大：5s 间隔 + 最长 60s），同时返回官方 open 供事件记录修正。
// 超时或 ctx 取消时返回 ok=false。
// ⚠️ 调用方必须以异步方式调用（本函数最长阻塞 60s）。
func PollOfficialClosePrice(ctx context.Context, client *sdk.PolymarketClient, start time.Time,
	windowSec, twapLookbackSeconds int64, timeout time.Duration) (open, close float64, ok bool) {
	return pollOfficialPrice(ctx, client, start, windowSec, twapLookbackSeconds,
		timeout, true, pollOfficialCloseInterval)
}
