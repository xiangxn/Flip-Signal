package main

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/xiangxn/go-polymarket-sdk/orders"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"

	"github.com/necklace/flip-signal/internal/collect"
	"github.com/necklace/flip-signal/internal/feed"
)

// ───────────────────────── 测试替身 ─────────────────────────

// fakeBinance 复刻 *feed.BinanceAdapter 的量语义: LatestData 把「自上次
// ConsumeVolume 以来」的累计量贴进返回结构，ConsumeVolume 读取即清零。
// （真身的行情字段是私有的，测试注入不进去——binanceSource 接口就是为它开的缝。）
type fakeBinance struct {
	mu         sync.Mutex
	data       feed.BinanceMarketData
	buyVol     float64
	sellVol    float64
	tradeCount uint64
	klineOpen  float64
	latestCall int
}

func (f *fakeBinance) LatestData() feed.BinanceMarketData {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.latestCall++
	d := f.data
	d.BuyVolume = f.buyVol
	d.SellVolume = f.sellVol
	d.TradeCount = f.tradeCount
	return d
}

func (f *fakeBinance) ConsumeVolume() (buyAcc, sellAcc float64) {
	f.mu.Lock()
	defer f.mu.Unlock()
	buyAcc, sellAcc = f.buyVol, f.sellVol
	f.buyVol, f.sellVol, f.tradeCount = 0, 0, 0
	return
}

func (f *fakeBinance) FetchKlineOpenPrice() float64 { return f.klineOpen }

// accum 模拟「本秒内又来了几笔成交」。
func (f *fakeBinance) accum(price, buy, sell float64, n uint64) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.data.Price = price
	f.data.BidDepth5, f.data.AskDepth5 = 11, 22
	f.data.BidDepth10, f.data.AskDepth10 = 33, 44
	f.buyVol += buy
	f.sellVol += sell
	f.tradeCount += n
}

// testTwap 造一个只吃推送通道的 TWAP 适配器（无网络），返回适配器与「喂一条并等
// 它进缓存」的闭包（PushNearest 命中即证明 consume goroutine 已处理）。
func testTwap(t *testing.T) (*feed.TwapAdapter, func(price float64, tsMs int64)) {
	t.Helper()
	ch := make(chan sdk.ExternalPrice, 8)
	ad := feed.NewTwapAdapterWithChannel(ch, "BTC", sdk.ChainlinkTwapWindowSixty)
	ctx, cancel := context.WithCancel(context.Background())
	ad.Start(ctx)
	t.Cleanup(cancel)
	push := func(price float64, tsMs int64) {
		t.Helper()
		ch <- sdk.ExternalPrice{Symbol: "BTC", WindowSeconds: sdk.ChainlinkTwapWindowSixty,
			Price: price, Timestamp: tsMs}
		deadline := time.Now().Add(2 * time.Second)
		for time.Now().Before(deadline) {
			if _, _, ok := ad.PushNearest(time.UnixMilli(tsMs)); ok {
				return
			}
			time.Sleep(time.Millisecond)
		}
		t.Fatalf("TWAP 推送未被消费（price=%.2f ts=%d）", price, tsMs)
	}
	return ad, push
}

// orderBook 造一本单档订单簿（bid/ask ≤ 0 表示该侧为空 = 决策 #21 的整侧撤空）。
func orderBook(assetID string, bid, ask, size float64) *sdk.OrderBook {
	b := &sdk.OrderBook{AssetId: assetID, Timestamp: 1_700_000_000_000, Latency: 12}
	if bid > 0 {
		b.Bids = []orders.Book{{Price: bid, Size: size}}
	}
	if ask > 0 {
		b.Asks = []orders.Book{{Price: ask, Size: size}}
	}
	return b
}

func staticBooks(up, down *sdk.OrderBook) func() (*sdk.OrderBook, *sdk.OrderBook) {
	return func() (*sdk.OrderBook, *sdk.OrderBook) { return up, down }
}

// testWindow 造一个已采满的窗口现场（ticks/锚都齐，红线全过）。
func testWindow(start time.Time) *eventWindow {
	w := &eventWindow{
		condID:      "0xcond",
		slug:        "btc-updown-5m-1700000000",
		start:       start,
		end:         start.Add(collect.WindowSec * time.Second),
		binanceOpen: 60_000,
		sink: &tradeSink{
			startMs: start.UnixMilli(),
			yesTok:  "UPTOKEN",
			noTok:   "DOWNTOKEN",
			bucket:  collect.NewTradeBucketer(start.UnixMilli()),
		},
		anchorPrice: 60_010.5,
		anchorSrc:   collect.SourcePush,
	}
	// 300 条 tick（ts = st+1s … st+300s），rem 身份与校验脚本 B 段一致。
	for i := 1; i <= collect.WindowSec; i++ {
		ts := start.Add(time.Duration(i) * time.Second)
		w.ticks = append(w.ticks, collect.HFTick{
			Ts:  ts.UnixMilli(),
			Rem: max(int(w.end.Sub(ts).Seconds()), 0),
			Bin: collect.BinTick{Price: 60_000 + float64(i)},
			PM:  collect.MakePMTick(orderBook("UPTOKEN", 0.79, 0.80, 5), orderBook("DOWNTOKEN", 0.20, 0.21, 5)),
			Twap: collect.TwapTick{Price: 60_005, AgeMs: 900},
		})
	}
	return w
}

// ───────────────────────── 纯函数 ─────────────────────────

// TestAnchorSourceFor 钉住跨包同名词表陷阱: feed 的 "stream" 是**精确边界推送**，
// 落进事件行必须是 "push"（否则校验 G 段把它归进 legacy 分档、needsCorrection 恒真）。
func TestAnchorSourceFor(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{"精确边界推送 → push", feed.AnchorSourceStream, collect.SourcePush},
		{"官方接口 → official", feed.AnchorSourceOfficial, collect.SourceOfficial},
		{"空串按推送口径兜底", "", collect.SourcePush},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := anchorSourceFor(c.in); got != c.want {
				t.Errorf("anchorSourceFor(%q) = %q, 期望 %q", c.in, got, c.want)
			}
		})
	}
}

// TestOutcomeFor 平局算 Up（校验 H 段写死此式，与 internal/settle 的严格大于不同）。
func TestOutcomeFor(t *testing.T) {
	cases := []struct {
		name        string
		open, close float64
		want        int
	}{
		{"平局算 Up", 60_000, 60_000, 0},
		{"上涨 → Up", 60_000, 60_000.01, 0},
		{"下跌 → Down", 60_000, 59_999.99, 1},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := outcomeFor(c.open, c.close); got != c.want {
				t.Errorf("outcomeFor(%.2f, %.2f) = %d, 期望 %d", c.open, c.close, got, c.want)
			}
		})
	}
}

func TestPickSink(t *testing.T) {
	cur := &tradeSink{startMs: 2_000_000, yesTok: "CUR_UP", noTok: "CUR_DOWN"}
	prev := &tradeSink{startMs: 1_700_000, yesTok: "PREV_UP", noTok: "PREV_DOWN"}
	cases := []struct {
		name string
		ts   int64
		cur  *tradeSink
		prev *tradeSink
		want *tradeSink
	}{
		{"本窗内 → cur", 2_000_000, cur, prev, cur},
		{"正好等于本窗起点 → cur", 2_000_000, cur, prev, cur},
		{"边界前的迟到笔 → prev", 1_999_999, cur, prev, prev},
		{"正好等于上一窗起点 → prev", 1_700_000, cur, prev, prev},
		{"更早的迟到笔 → 丢弃", 1_699_999, cur, prev, nil},
		{"无 cur（启动首窗）→ prev", 1_700_000, nil, prev, prev},
		{"无 prev → 丢弃", 1_700_000, cur, nil, nil},
		{"两者皆无 → 丢弃", 1_700_000, nil, nil, nil},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := pickSink(c.ts, c.cur, c.prev); got != c.want {
				t.Errorf("pickSink(%d) = %v, 期望 %v", c.ts, got, c.want)
			}
		})
	}
}

// TestTradeSinkTokenFor 钉住「token 表随桶走」: 上一窗的桶必须用上一窗的 token 映射
// （本族的 upTok/downTok 每窗换装，用全局变量会把尾盘迟到笔误判成陌生 token 丢掉）。
func TestTradeSinkTokenFor(t *testing.T) {
	s := &tradeSink{yesTok: "CUR_UP", noTok: "CUR_DOWN"}
	cases := []struct{ in, want string }{
		{"CUR_UP", tokenYes},
		{"CUR_DOWN", tokenNo},
		{"PREV_UP", ""},   // 上一窗的 token = 陌生
		{"", ""},          // 缺 asset_id
		{"OTHER", ""},     // 无关市场
	}
	for _, c := range cases {
		if got := s.tokenFor(c.in); got != c.want {
			t.Errorf("tokenFor(%q) = %q, 期望 %q", c.in, got, c.want)
		}
	}
}

// ───────────────────────── 组装与红线 ─────────────────────────

func TestBuildEventFields(t *testing.T) {
	start := time.Unix(1_700_000_000, 0).UTC()
	ad, push := testTwap(t)
	_ = push
	c := &eventCollector{twap: ad}
	w := testWindow(start)

	// 两条成交（一笔 YES 主动买、一笔 NO 主动卖）落进桶。
	w.sink.bucket.Add(tokenYes, start.UnixMilli()+1000, "BUY", 0.80, 5, 0.79, 0.80, "hash1")
	w.sink.bucket.Add(tokenNo, start.UnixMilli()+2000, "SELL", 0.21, 3, 0.20, 0.21, "hash2")

	ev, reason := c.buildEvent(w, 60_020.25)
	if ev == nil {
		t.Fatalf("buildEvent 拒绝了一个完整窗口: %s", reason)
	}
	if ev.ConditionID != w.condID || ev.Slug != w.slug || ev.StartTime != start.Unix() {
		t.Errorf("元字段不符: %+v", ev)
	}
	if ev.TwapOpenPrice != w.anchorPrice || ev.AnchorSource != collect.SourcePush {
		t.Errorf("锚字段不符: open=%v src=%q", ev.TwapOpenPrice, ev.AnchorSource)
	}
	if ev.TwapClosePrice != 60_020.25 || ev.CloseSource != collect.SourcePush {
		t.Errorf("收盘字段不符: close=%v src=%q", ev.TwapClosePrice, ev.CloseSource)
	}
	if ev.Outcome != 0 {
		t.Errorf("outcome = %d, 期望 0（close > open 即 Up）", ev.Outcome)
	}
	if ev.BinanceOpen != w.binanceOpen {
		t.Errorf("binance_open = %v, 期望 %v", ev.BinanceOpen, w.binanceOpen)
	}
	if len(ev.Ticks) != collect.WindowSec {
		t.Errorf("ticks = %d, 期望 %d", len(ev.Ticks), collect.WindowSec)
	}
	if len(ev.Trades) != 2 {
		t.Fatalf("trades = %d, 期望 2", len(ev.Trades))
	}
	for _, tr := range ev.Trades {
		if tr.Token != tokenYes && tr.Token != tokenNo {
			t.Errorf("成交 token = %q, 越出 {YES,NO}", tr.Token)
		}
		// 校验 F 段: want = st + 300 − sec（无 −1）
		if want := int(start.Add(collect.WindowSec*time.Second).Sub(time.UnixMilli(tr.Ts)).Seconds()); tr.Rem != want {
			t.Errorf("成交 rem = %d, 期望 %d", tr.Rem, want)
		}
	}
}

// TestBuildEventCloseFallbacks 收盘推送缺失时的两级回退。
func TestBuildEventCloseFallbacks(t *testing.T) {
	start := time.Unix(1_700_000_000, 0).UTC()

	t.Run("推送缺失 → 流值 stream", func(t *testing.T) {
		ad, push := testTwap(t)
		push(60_030.75, start.UnixMilli()) // 只要 Latest 有值即可
		c := &eventCollector{twap: ad}
		ev, reason := c.buildEvent(testWindow(start), 0)
		if ev == nil {
			t.Fatalf("被拒: %s", reason)
		}
		if ev.CloseSource != collect.SourceStream || ev.TwapClosePrice != 60_030.75 {
			t.Errorf("close=%v src=%q, 期望流值 60_030.75 / stream", ev.TwapClosePrice, ev.CloseSource)
		}
	})

	t.Run("流值也为 0 → 回退锚价", func(t *testing.T) {
		ad, _ := testTwap(t) // 一条推送都没喂 ⇒ Latest() == 0
		c := &eventCollector{twap: ad}
		w := testWindow(start)
		ev, reason := c.buildEvent(w, 0)
		if ev == nil {
			t.Fatalf("被拒: %s", reason)
		}
		// 校验 A 段要求 twap_close_price > 0（硬 FAIL）——宁可是「振幅 0 的窗」，
		// 也不能落一个 0 收盘价的行。
		if ev.TwapClosePrice != w.anchorPrice {
			t.Errorf("close = %v, 期望回退到锚价 %v", ev.TwapClosePrice, w.anchorPrice)
		}
		if ev.Outcome != 0 {
			t.Errorf("outcome = %d, 期望 0（close == open 平局算 Up）", ev.Outcome)
		}
	})
}

// TestBuildEventRedLines 三条落盘红线（宁可留洞，不写脏行）。
func TestBuildEventRedLines(t *testing.T) {
	start := time.Unix(1_700_000_000, 0).UTC()
	cases := []struct {
		name   string
		mutate func(w *eventWindow)
	}{
		{"锚缺失", func(w *eventWindow) { w.anchorPrice = 0 }},
		{"Binance 未就绪", func(w *eventWindow) { w.binanceOpen = 0 }},
		{"半窗（292 tick）", func(w *eventWindow) { w.ticks = w.ticks[:292] }},
		{"迟入（首 tick rem=294）", func(w *eventWindow) { w.ticks[0].Rem = 294 }},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			ad, _ := testTwap(t)
			col := &eventCollector{twap: ad}
			w := testWindow(start)
			c.mutate(w)
			if ev, reason := col.buildEvent(w, 60_020); ev != nil || reason == "" {
				t.Errorf("红线未拦住: ev=%v reason=%q", ev, reason)
			}
		})
	}

	t.Run("边界值 293 tick / rem=295 放行", func(t *testing.T) {
		ad, _ := testTwap(t)
		col := &eventCollector{twap: ad}
		w := testWindow(start)
		w.ticks = w.ticks[:293]
		w.ticks[0].Rem = 295
		if ev, reason := col.buildEvent(w, 60_020); ev == nil {
			t.Errorf("边界值被误拦: %s", reason)
		}
	})
}

// ───────────────────────── 采样组装 ─────────────────────────

// TestSampleMapsRawFields 逐字段对账: 量是**秒增量**、价格取**原始值**（不套引擎的
// 陈旧钳零——校验 D 段 `bin.price <= 0` 是硬 FAIL）、盘口走 collect.MakePMTick。
func TestSampleMapsRawFields(t *testing.T) {
	start := time.Unix(1_700_000_000, 0).UTC()
	b := &fakeBinance{}
	// ⚠️ 现货是「陈旧」的（RxAtMs 停在 1 小时前）: 采样照样落原始价——
	// engine 的 tick 会把它钳成 0（missing_spot），事件行不能。
	b.data.RxAtMs = time.Now().UnixMilli() - 3_600_000
	b.accum(60_123.5, 7, 3, 4)

	up := orderBook("UPTOKEN", 0.78, 0.80, 5)
	down := orderBook("DOWNTOKEN", 0.20, 0.22, 6)
	ad, push := testTwap(t)
	push(60_100, start.UnixMilli()+500)

	c := &eventCollector{binance: b, twap: ad, books: staticBooks(up, down)}
	w := &eventWindow{start: start, end: start.Add(collect.WindowSec * time.Second)}
	c.sample(w, start.Add(time.Second))

	if len(w.ticks) != 1 {
		t.Fatalf("ticks = %d, 期望 1", len(w.ticks))
	}
	tk := w.ticks[0]
	if tk.Ts != start.Add(time.Second).UnixMilli() {
		t.Errorf("ts = %d, 期望 %d", tk.Ts, start.Add(time.Second).UnixMilli())
	}
	if tk.Rem != 299 {
		t.Errorf("rem = %d, 期望 299（st+1s 的剩余整秒）", tk.Rem)
	}
	if tk.Bin.Price != 60_123.5 {
		t.Errorf("bin.price = %v, 期望原始值 60123.5（陈旧也不钳零）", tk.Bin.Price)
	}
	if tk.Bin.BuyVol != 7 || tk.Bin.SellVol != 3 || tk.Bin.Ticks != 4 {
		t.Errorf("量字段不符: %+v", tk.Bin)
	}
	if tk.Bin.Bid5 != 11 || tk.Bin.Ask5 != 22 || tk.Bin.Bid10 != 33 || tk.Bin.Ask10 != 44 {
		t.Errorf("深度字段不符: %+v", tk.Bin)
	}
	if tk.PM.YesBid != 0.78 || tk.PM.YesAsk != 0.80 || tk.PM.NoBid != 0.20 || tk.PM.NoAsk != 0.22 {
		t.Errorf("盘口字段不符: %+v", tk.PM)
	}
	if tk.PM.YesBidTop5 != 5 || tk.PM.NoAskTop5 != 6 || tk.PM.BookLatMs != 12 {
		t.Errorf("盘口深度/延迟不符: %+v", tk.PM)
	}
	if tk.Twap.Price != 60_100 {
		t.Errorf("twap = %v, 期望 60100", tk.Twap.Price)
	}

	// 第二秒: 量必须只剩**本秒增量**（ConsumeVolume 已清零）。
	b.accum(60_124, 1, 0, 1)
	c.sample(w, start.Add(2*time.Second))
	if w.ticks[1].Bin.BuyVol != 1 || w.ticks[1].Bin.SellVol != 0 || w.ticks[1].Bin.Ticks != 1 {
		t.Errorf("第二秒不是增量: %+v", w.ticks[1].Bin)
	}
}

// TestSampleKeepsEmptyBookSide 决策 #21: 赢家侧整侧撤空照存（该侧价格为 0），
// 不拿「撤单前的旧簿」冒充。
func TestSampleKeepsEmptyBookSide(t *testing.T) {
	start := time.Unix(1_700_000_000, 0).UTC()
	b := &fakeBinance{}
	ad, _ := testTwap(t)
	// 持仓侧（热门）只剩 bid、没有 ask（尾盘赢家侧被整侧撤空）
	up := orderBook("UPTOKEN", 0.97, 0, 5)
	down := orderBook("DOWNTOKEN", 0.01, 0.03, 5)
	c := &eventCollector{binance: b, twap: ad, books: staticBooks(up, down)}
	w := &eventWindow{start: start, end: start.Add(collect.WindowSec * time.Second)}
	c.sample(w, start.Add(time.Second))

	if w.ticks[0].PM.YesAsk != 0 {
		t.Errorf("YesAsk = %v, 期望 0（空侧如实落 0，不填旧簿）", w.ticks[0].PM.YesAsk)
	}
	if w.ticks[0].PM.YesBid != 0.97 {
		t.Errorf("YesBid = %v, 期望 0.97（有档的一侧照常）", w.ticks[0].PM.YesBid)
	}
}

// ───────────────────────── 成交路由 ─────────────────────────

// TestConsumeTradesRouting 成交按**自身时戳**选桶、按**桶自己的 token 表**映射。
func TestConsumeTradesRouting(t *testing.T) {
	start := time.Unix(1_700_000_000, 0).UTC()
	prevStart := start.Add(-collect.WindowSec * time.Second)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	cur := &eventWindow{
		start: start, end: start.Add(collect.WindowSec * time.Second),
		sink: &tradeSink{startMs: start.UnixMilli(), yesTok: "UP2", noTok: "DOWN2",
			bucket: collect.NewTradeBucketer(start.UnixMilli())},
	}
	prevSink := &tradeSink{startMs: prevStart.UnixMilli(), yesTok: "UP1", noTok: "DOWN1",
		bucket: collect.NewTradeBucketer(prevStart.UnixMilli())}
	c := &eventCollector{
		cur:   cur,
		prev:  prevSink,
		books: staticBooks(orderBook("UP2", 0.79, 0.80, 5), orderBook("DOWN2", 0.20, 0.21, 5)),
	}

	ch := make(chan *sdk.LastTradePriceInfo, 16)
	done := make(chan struct{})
	go func() { defer close(done); c.consumeTrades(ctx, ch) }()

	ch <- &sdk.LastTradePriceInfo{AssetID: "UP2", Timestamp: start.UnixMilli() + 5000,
		Side: "BUY", Price: "0.80", Size: "5", TransactionHash: "h1"}
	ch <- &sdk.LastTradePriceInfo{AssetID: "DOWN2", Timestamp: start.UnixMilli() + 5000,
		Side: "SELL", Price: "0.21", Size: "3", TransactionHash: "h2"}
	// 上一窗的尾盘笔（时戳落在上一窗、token 也是上一窗的）
	ch <- &sdk.LastTradePriceInfo{AssetID: "UP1", Timestamp: prevStart.UnixMilli() + 299_000,
		Side: "BUY", Price: "0.75", Size: "9", TransactionHash: "h3"}
	// 陌生 token（无关市场）——必须丢弃
	ch <- &sdk.LastTradePriceInfo{AssetID: "STRANGER", Timestamp: start.UnixMilli() + 5000,
		Side: "BUY", Price: "0.50", Size: "1", TransactionHash: "h4"}
	// 更早窗口的迟到笔（两桶都不匹配）——必须丢弃
	ch <- &sdk.LastTradePriceInfo{AssetID: "UP1", Timestamp: prevStart.UnixMilli() - 1000,
		Side: "BUY", Price: "0.50", Size: "1", TransactionHash: "h5"}

	// 等消费完（桶里出现 3 笔 = 全部非丢弃笔）
	waitFor(t, func() bool { return countTrades(cur.sink.bucket) == 2 && countTrades(prevSink.bucket) == 1 })

	curTrades := cur.sink.bucket.Snapshot()
	var yesBuy, noSell int
	for _, tr := range curTrades {
		switch tr.Token {
		case tokenYes:
			yesBuy += tr.NBuy
		case tokenNo:
			noSell += tr.NSell
		}
	}
	if yesBuy != 1 || noSell != 1 {
		t.Errorf("本窗桶 = %+v, 期望 YES 主动买 1 笔 + NO 主动卖 1 笔", curTrades)
	}
	prevTrades := prevSink.bucket.Snapshot()
	if len(prevTrades) != 1 || prevTrades[0].Token != tokenYes || prevTrades[0].NBuy != 1 {
		t.Errorf("上一窗桶 = %+v, 期望 1 行 YES 主动买", prevTrades)
	}
	cancel()
	<-done
}

func countTrades(b *collect.TradeBucketer) int {
	n := 0
	for _, tr := range b.Snapshot() {
		n += tr.NBuy + tr.NSell
	}
	return n
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatal("等待条件超时")
}

// TestWindowSwapWhileConsuming 竞态回归（-race）: 窗口换装（主循环写 cur/prev）与
// 成交消费 goroutine 并发时, 已写下的成交行不被改写, 且迟到笔/新笔各归其桶。
func TestWindowSwapWhileConsuming(t *testing.T) {
	start := time.Unix(1_700_000_000, 0).UTC()
	next := start.Add(collect.WindowSec * time.Second)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	bin := &fakeBinance{}
	ad, _ := testTwap(t)
	c := &eventCollector{
		binance: bin, twap: ad, books: staticBooks(nil, nil),
		worker: collect.NewSettlementWorker(t.TempDir(),
			func(context.Context, time.Time) (float64, float64, bool) { return 0, 0, false },
			collect.DefaultSettlementConfig()),
	}
	c.worker.Start(ctx)
	ch := make(chan *sdk.LastTradePriceInfo, 64)
	go c.consumeTrades(ctx, ch)

	c.BeginWindow(ctx, "0x1", "slug-1", start, "UP1", "DOWN1")
	c.mu.Lock()
	first := c.cur.sink
	c.mu.Unlock()
	ch <- &sdk.LastTradePriceInfo{AssetID: "UP1", Timestamp: start.UnixMilli() + 1000,
		Side: "BUY", Price: "0.8", Size: "1", TransactionHash: "a"}
	waitFor(t, func() bool { return len(first.bucket.Snapshot()) == 1 })
	frozen := first.bucket.Snapshot()

	// 换装下一窗（其 runWindow 同时起跑），再继续灌上一窗的迟到笔与本窗新笔。
	c.BeginWindow(ctx, "0x2", "slug-2", next, "UP2", "DOWN2")
	c.mu.Lock()
	second := c.cur.sink
	c.mu.Unlock()
	for i := 0; i < 20; i++ {
		ch <- &sdk.LastTradePriceInfo{AssetID: "UP1", Timestamp: start.UnixMilli() + 200_000,
			Side: "BUY", Price: "0.8", Size: "1", TransactionHash: "late"}
		ch <- &sdk.LastTradePriceInfo{AssetID: "UP2", Timestamp: next.UnixMilli() + 1000,
			Side: "BUY", Price: "0.9", Size: "1", TransactionHash: "new"}
		time.Sleep(time.Millisecond)
	}
	// 20 笔同哈希迟到笔（桶内去重）⇒ 上一窗只多一行；20 笔同哈希新笔 ⇒ 下一窗一行。
	waitFor(t, func() bool {
		return len(first.bucket.Snapshot()) == 2 && len(second.bucket.Snapshot()) == 1
	})
	got := first.bucket.Snapshot()
	if got[0] != frozen[0] {
		t.Errorf("已定稿窗口的成交行被改写: %+v → %+v", frozen[0], got[0])
	}
	// 迟到笔归上一窗、新笔归下一窗——两边都不能漏进对方。
	if last := got[1]; last.Ts != start.UnixMilli()+200_000 || last.NBuy != 1 {
		t.Errorf("迟到笔未归上一窗: %+v", last)
	}
	if nw := second.bucket.Snapshot()[0]; nw.Ts != next.UnixMilli()+1000 || nw.NBuy != 1 {
		t.Errorf("新笔未归下一窗: %+v", nw)
	}
}

// ───────────────────────── 落盘 ─────────────────────────

// TestSubmitWritesEventFile 端到端: 组装好的事件经 settlement worker 落进
// events_dir，文件形状与校验脚本的期望一致（10 个顶层键齐全）。
func TestSubmitWritesEventFile(t *testing.T) {
	dir := t.TempDir()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	start := time.Unix(1_700_000_000, 0).UTC()
	ad, _ := testTwap(t)
	c := &eventCollector{dir: dir, twap: ad}
	// 官方取数回调恒失败: 本窗 close_source=push ⇒ needsCorrection=false ⇒ 不会被调；
	// 真被调了也不会产出修正行，故下面的「只有一行」断言同时钉住了这条。
	c.worker = collect.NewSettlementWorker(dir,
		func(ctx context.Context, s time.Time) (float64, float64, bool) { return 0, 0, false },
		collect.DefaultSettlementConfig())
	c.worker.Start(ctx)

	w := testWindow(start)
	ev, reason := c.buildEvent(w, 60_020.25)
	if ev == nil {
		t.Fatalf("被拒: %s", reason)
	}
	c.worker.Submit(ev, false)

	var line []byte
	waitFor(t, func() bool {
		matches, _ := filepath.Glob(filepath.Join(dir, "events_*.jsonl"))
		if len(matches) != 1 {
			return false
		}
		b, err := os.ReadFile(matches[0])
		if err != nil || len(b) == 0 {
			return false
		}
		line = b
		return true
	})
	// 归日按窗口起点（跨午夜窗的事件与修正行必须同文件）
	matches, _ := filepath.Glob(filepath.Join(dir, "events_*.jsonl"))
	if want := "events_" + start.Format("2006-01-02") + ".jsonl"; filepath.Base(matches[0]) != want {
		t.Errorf("文件名 = %s, 期望 %s", filepath.Base(matches[0]), want)
	}

	// 推送口径的窗**只落一行**（needsCorrection=false ⇒ 不该有官方修正行）
	lines := bytes.Split(bytes.TrimSpace(line), []byte("\n"))
	if len(lines) != 1 {
		t.Fatalf("落盘行数 = %d, 期望 1（推送口径的窗不排队官方修正）", len(lines))
	}
	var raw map[string]any
	if err := json.Unmarshal(lines[0], &raw); err != nil {
		t.Fatalf("落盘行不是合法 JSON: %v", err)
	}
	for _, k := range []string{"condition_id", "slug", "start_time", "twap_open_price",
		"twap_close_price", "close_source", "anchor_source", "outcome", "binance_open",
		"ticks", "trades"} {
		if _, ok := raw[k]; !ok {
			t.Errorf("落盘行缺键 %q", k)
		}
	}
	if raw["anchor_source"] != collect.SourcePush || raw["close_source"] != collect.SourcePush {
		t.Errorf("来源词表不符: anchor=%v close=%v", raw["anchor_source"], raw["close_source"])
	}
	if n, ok := raw["ticks"].([]any); !ok || len(n) != collect.WindowSec {
		t.Errorf("ticks 落盘条数 = %v, 期望 %d", raw["ticks"], collect.WindowSec)
	}
	// trades 必须是数组（哪怕空）——落 null 会让 python 侧 len() 炸
	if _, ok := raw["trades"].([]any); !ok {
		t.Errorf("trades 不是数组: %T", raw["trades"])
	}
}

// TestNilCollectorNoop 采集关闭（events_dir 空串 ⇒ newEventCollector 返回 nil）时,
// 生产路径上会发生的三个调用必须是安全的空操作。release 由 runWindow 独占调用
// （只有非 nil 采集器才可能起 runWindow），故不在本测试之列。
func TestNilCollectorNoop(t *testing.T) {
	var c *eventCollector
	c.SetAnchor(1_700_000_000, 60_000, feed.AnchorSourceStream)
	c.BeginWindow(context.Background(), "0x1", "s", time.Unix(1_700_000_000, 0), "A", "B")
	c.ConsumeTrades(context.Background(), nil)
}
