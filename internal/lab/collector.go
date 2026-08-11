package lab

import (
	"math"
	"time"

	"github.com/necklace/flip-signal/internal/feed"
)

// maxPriceHistoryAt5s 是默认 5s 采样间隔下的滚动价格历史长度（75秒回溯）。
const maxPriceHistoryAt5s = 15

// Collector 以可配置的采样间隔在 5 分钟事件窗口内生成 ResearchSnapshot。
//
// 内部维护滚动价格历史，用于计算:
//   - 2-tick 收益率
//   - 短期波动率 (6-tick)
//   - 长期波动率 (12-tick)
//
// 切换采样间隔时，maxPrices 会自动缩放以保持相同的回溯时长。
type Collector struct {
	binance *feed.BinanceAdapter

	// 滚动价格历史 —— 每次 Tick 追加，超过 maxPrices 则剔除最旧的
	prices    []float64
	maxPrices int // len(prices) 上限，由 maxPriceHistoryAt5s 按采样间隔缩放

	// Polymarket 价格（每次 Tick 前由外部调用 UpdatePolymarket 更新）
	yesPrice         float64
	noPrice          float64
	orderBookLatency int64

	// 采样间隔（秒）
	tickIntervalSec int

	// 当前事件状态
	conditionID string
	startTime   int64
	endTime     int64
	openPrice   float64
	snapshots   []*ResearchSnapshot
}

// NewCollector 创建绑定到 BinanceAdapter 的采集器。
// tickIntervalSec 为采样间隔（秒），典型值 1 或 5，≤0 时回退为默认值。
func NewCollector(binance *feed.BinanceAdapter, tickIntervalSec int) *Collector {
	if tickIntervalSec <= 0 {
		tickIntervalSec = DefaultTickIntervalSec
	}
	// 按采样间隔缩放价格历史容量，保持 ~75s 回溯时长不变。
	// 5s 间隔 → 15 点，1s 间隔 → 75 点。
	maxP := maxPriceHistoryAt5s * 5 / tickIntervalSec
	if maxP < maxPriceHistoryAt5s {
		maxP = maxPriceHistoryAt5s
	}
	return &Collector{
		binance:         binance,
		prices:          make([]float64, 0, maxP),
		maxPrices:       maxP,
		tickIntervalSec: tickIntervalSec,
	}
}

// UpdatePolymarket 设置最新的 YES/NO 价格和订单簿延迟。
// latencyMs 是 YES/NO 两个订单簿延迟的较大值（毫秒）。
func (c *Collector) UpdatePolymarket(yesPrice, noPrice float64, latencyMs int64) {
	c.yesPrice = yesPrice
	c.noPrice = noPrice
	c.orderBookLatency = latencyMs
}

// StartEvent 开始一个新的 5 分钟事件窗口。
func (c *Collector) StartEvent(conditionID string, startTime int64, openPrice float64) {
	c.conditionID = conditionID
	c.startTime = startTime
	c.endTime = startTime + WindowSec
	c.openPrice = openPrice
	c.snapshots = make([]*ResearchSnapshot, 0, WindowSec/c.tickIntervalSec+10)
	c.prices = c.prices[:0]
	c.yesPrice = 0
	c.noPrice = 0
	c.orderBookLatency = 0
}

// Tick 生成当前时刻的 ResearchSnapshot。
// Binance 数据不可用时返回 nil。
//
// 按配置的采样间隔调用。每次调用消费 BinanceAdapter 自上次 Tick 以来
// 累积的成交量。
func (c *Collector) Tick(now time.Time) *ResearchSnapshot {
	btc := c.binance.LatestData()
	if btc.Price == 0 {
		return nil
	}

	buyVol, sellVol := c.binance.ConsumeVolume()

	remaining := int(c.endTime - now.Unix())
	if remaining < 0 {
		remaining = 0
	}

	price := btc.Price

	// 滚动价格历史
	c.prices = append(c.prices, price)
	if len(c.prices) > c.maxPrices {
		c.prices = c.prices[1:]
	}

	// 2-tick 收益率（5s 间隔时 ≈10s，1s 间隔时 ≈2s）
	ret := 0.0
	if len(c.prices) >= 3 {
		prev := c.prices[len(c.prices)-3] // 2 ticks 之前
		if prev > 0 {
			ret = (price - prev) / prev
		}
	}

	snap := &ResearchSnapshot{
		Timestamp:     now.UnixMilli(),
		RemainingSec:  remaining,
		OpenPrice:     c.openPrice,
		CurrentPrice:  price,
		Return10s:      ret,
		BuyVolume5s:   buyVol,
		SellVolume5s:  sellVol,
		SignedFlow5s:  buyVol - sellVol,
		Volatility10s: c.computeVolatilityTicks(2), // 2 tick 波动率
		Volatility30s: c.computeVolatilityTicks(6), // 6 tick 波动率
		BidDepth:        btc.BidDepth5,
		AskDepth:        btc.AskDepth5,
		YesPrice:         c.yesPrice,
		NoPrice:          c.noPrice,
		OrderBookLatency: c.orderBookLatency,
	}

	c.snapshots = append(c.snapshots, snap)
	return snap
}

// computeVolatilityTicks 计算最近 nTicks 个数据点的 tick-to-tick 收益率标准差。
// 历史数据不足时返回 0。
func (c *Collector) computeVolatilityTicks(nTicks int) float64 {
	n := nTicks + 1 // 需要 nTicks+1 个价格才能得到 nTicks 个收益率
	if n > len(c.prices) {
		n = len(c.prices)
	}
	if n < 2 {
		return 0
	}

	window := c.prices[len(c.prices)-n:]
	returns := make([]float64, 0, n-1)
	for i := 1; i < len(window); i++ {
		if window[i-1] > 0 {
			returns = append(returns, (window[i]-window[i-1])/window[i-1])
		}
	}

	if len(returns) == 0 {
		return 0
	}

	var sum float64
	for _, r := range returns {
		sum += r
	}
	mean := sum / float64(len(returns))

	var sumSqDiff float64
	for _, r := range returns {
		diff := r - mean
		sumSqDiff += diff * diff
	}

	return math.Sqrt(sumSqDiff / float64(len(returns)))
}

// FinalizeEvent 封存当前事件并返回带结果的 Event。
func (c *Collector) FinalizeEvent() *Event {
	closePrice := 0.0
	if len(c.snapshots) > 0 {
		closePrice = c.snapshots[len(c.snapshots)-1].CurrentPrice
	}

	// Polymarket outcomes[] 约定: [0]=Up, [1]=Down
	outcome := 1 // Down
	if closePrice > c.openPrice {
		outcome = 0 // Up
	}

	return &Event{
		ConditionID: c.conditionID,
		StartTime:   c.startTime,
		OpenPrice:   c.openPrice,
		ClosePrice:  closePrice,
		Outcome:     outcome,
		Snapshots:   c.snapshots,
	}
}

// ── Dashboard 访问器 ──

// ConditionID 返回当前事件的 condition ID。
func (c *Collector) ConditionID() string { return c.conditionID }

// OpenPrice 返回当前事件的开盘价。
func (c *Collector) OpenPrice() float64 { return c.openPrice }

// Snapshots 返回当前事件 snapshot 切片的副本。
func (c *Collector) Snapshots() []*ResearchSnapshot {
	out := make([]*ResearchSnapshot, len(c.snapshots))
	copy(out, c.snapshots)
	return out
}
