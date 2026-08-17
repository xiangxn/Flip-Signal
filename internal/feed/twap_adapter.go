package feed

import (
	"context"
	"log"
	"math"
	"math/rand"
	"strings"
	"sync"
	"time"

	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// TwapAdapter 封装 SDK CryptoPriceMonitor 的 Chainlink TWAP 订阅，
// 维护指定 symbol 的 TWAP 最新值与推送新鲜度，供 Collector 周期采样。
//
// btc-updown-5m 市场以 Chainlink TWAP-60（60 秒滚动窗口）判定胜负，
// 因此本适配器只保留 60s 窗口的推送（30s 窗口与其它 symbol 直接丢弃）。
type TwapAdapter struct {
	symbol    string
	windowSec int64
	updates   <-chan sdk.ExternalPrice

	mu           sync.RWMutex
	price        float64
	lastUpdateAt int64 // 最近一次有效推送的本地到达时间（unix 毫秒）
}

// NewTwapAdapter 从 CryptoPriceMonitor 的订阅通道创建 TWAP 适配器。
// monitor 应以 MonitorChainlinkTwap 类型创建（SDK 同时订阅 30s/60s 窗口）。
func NewTwapAdapter(monitor *sdk.CryptoPriceMonitor, symbol string, windowSec int64) *TwapAdapter {
	return NewTwapAdapterWithChannel(monitor.Subscribe(), symbol, windowSec)
}

// NewTwapAdapterWithChannel 直接从推送通道创建适配器（测试用）。
func NewTwapAdapterWithChannel(updates <-chan sdk.ExternalPrice, symbol string, windowSec int64) *TwapAdapter {
	return &TwapAdapter{
		symbol:    strings.ToUpper(symbol),
		windowSec: windowSec,
		updates:   updates,
	}
}

// Start 启动推送消费 goroutine，持续刷新最新 TWAP 值，ctx 取消后退出。
func (t *TwapAdapter) Start(ctx context.Context) {
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case ep, ok := <-t.updates:
				if !ok {
					return
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
	}()
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
