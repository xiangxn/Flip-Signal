package collect

import (
	"sort"
	"sync"
)

// WindowSec 是 5 分钟窗口长度（秒），与 lab.WindowSec 一致。
const WindowSec = 300

// TradeBucketer 将 PM last_trade_price 逐笔成交聚合为每 token 每秒一行。
//
// 原始逐笔数据量过大不落盘（见数据重采计划 §3.2）：Add 按成交自身时戳
// 归入 [窗口起点, 窗口终点) 的秒桶，累计主动买/卖笔数与量、最大单笔、
// vwap 与末笔 best bid/ask。窗口边界外的迟到笔直接丢弃。
//
// ⚠️ price_change 通道实测是挂单流（镜像对、buy≡sell 恒等），成交流
// 必须是 last_trade_price（2026-08-18 数据审计，raw WS 抓包 + data-api
// /trades 对账验证）。hash 去重防护 WS 重连重放。
//
// 线程安全：Add 可被 last_trade_price 消费 goroutine 与主循环并发调用。
type TradeBucketer struct {
	mu      sync.Mutex
	startMs int64 // 窗口起点（unix 毫秒）
	// token → 秒索引 → 聚合
	buckets map[string]map[int]*TradeAgg
	// 已见交易哈希（WS 重连重放防护）
	seen map[string]bool
}

// NewTradeBucketer 创建以 startMs 为窗口起点的聚合器。
func NewTradeBucketer(startMs int64) *TradeBucketer {
	return &TradeBucketer{
		startMs: startMs,
		buckets: make(map[string]map[int]*TradeAgg),
		seen:    make(map[string]bool),
	}
}

// Add 累加一笔成交。tsMs 为成交自身时戳（消息内服务端时间，unix 毫秒）。
// side 为 "BUY"/"SELL"（成交主动方/taker 口径）。
// hash 为成交 transaction_hash（非空时按哈希去重，防 WS 重连重放）。
func (t *TradeBucketer) Add(token string, tsMs int64, side string,
	price, size, bestBid, bestAsk float64, hash string) {
	// ⚠️ 不能直接 int((tsMs-startMs)/1000)：Go 整数除法向零截断，
	// 起点前 1s 内的远端时戳（如上一窗口尾笔迟到）会误入首桶（idx=0）。
	delta := tsMs - t.startMs
	if delta < 0 {
		return // 窗口起点前的笔（上一窗口尾笔迟到）
	}
	idx := int(delta / 1000)
	if idx >= WindowSec {
		return // 窗口终点外的笔
	}

	t.mu.Lock()
	defer t.mu.Unlock()

	if hash != "" {
		if t.seen[hash] {
			return // WS 重连重放的重复笔
		}
		t.seen[hash] = true
	}

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
