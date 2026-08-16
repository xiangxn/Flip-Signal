package collect

import (
	"sort"
	"sync"
)

// WindowSec 是 5 分钟窗口长度（秒），与 lab.WindowSec 一致。
const WindowSec = 300

// TradeBucketer 将 PM price_change 逐笔成交聚合为每 token 每秒一行。
//
// 原始逐笔数据量过大不落盘（见数据重采计划 §3.2）：Add 按本地接收时刻
// 归入 [窗口起点, 窗口终点) 的秒桶，累计主动买/卖笔数与量、最大单笔、
// vwap 与末笔 best bid/ask。窗口边界外的迟到笔直接丢弃。
//
// 线程安全：Add 可被 price_change 消费 goroutine 与主循环并发调用。
type TradeBucketer struct {
	mu      sync.Mutex
	startMs int64 // 窗口起点（unix 毫秒）
	// token → 秒索引 → 聚合
	buckets map[string]map[int]*TradeAgg
}

// NewTradeBucketer 创建以 startMs 为窗口起点的聚合器。
func NewTradeBucketer(startMs int64) *TradeBucketer {
	return &TradeBucketer{
		startMs: startMs,
		buckets: make(map[string]map[int]*TradeAgg),
	}
}

// Add 累加一笔成交。tsMs 为本地接收时刻（unix 毫秒）。
// side 为 "BUY"/"SELL"（CLOB 口径的主动方）。
func (t *TradeBucketer) Add(token string, tsMs int64, side string,
	price, size, bestBid, bestAsk float64) {
	idx := int((tsMs - t.startMs) / 1000)
	if idx < 0 || idx >= WindowSec {
		return // 窗口边界外的迟到笔
	}

	t.mu.Lock()
	defer t.mu.Unlock()

	inner := t.buckets[token]
	if inner == nil {
		inner = make(map[int]*TradeAgg)
		t.buckets[token] = inner
	}
	agg := inner[idx]
	if agg == nil {
		agg = &TradeAgg{
			Ts:      t.startMs + int64(idx)*1000,
			Rem:     WindowSec - idx,
			Token:   token,
			BestBid: bestBid,
			BestAsk: bestAsk,
		}
		inner[idx] = agg
	}

	switch side {
	case "BUY":
		agg.NBuy++
		agg.BuySize += size
	default: // SELL 及未知主动方一律计入卖侧
		agg.NSell++
		agg.SellSize += size
	}
	if size > agg.MaxSize {
		agg.MaxSize = size
	}
	total := agg.BuySize + agg.SellSize
	if total > 0 {
		agg.VWAP = (agg.VWAP*(total-size) + price*size) / total
	} else {
		agg.VWAP = price
	}
	agg.LastPrice = price
	agg.BestBid = bestBid
	agg.BestAsk = bestAsk
}

// Snapshot 返回全部聚合行，按 (token, ts) 排序。
func (t *TradeBucketer) Snapshot() []TradeAgg {
	t.mu.Lock()
	defer t.mu.Unlock()

	out := make([]TradeAgg, 0)
	for _, inner := range t.buckets {
		for _, agg := range inner {
			out = append(out, *agg)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Token != out[j].Token {
			return out[i].Token < out[j].Token
		}
		return out[i].Ts < out[j].Ts
	})
	return out
}
