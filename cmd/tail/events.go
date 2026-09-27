// events.go 是 cmd/tail 兼作**数据采集器**的部分：把每秒原始采样按「数据格式 v2」
// （与 data/btc 同构，结构见 internal/collect/types.go）落到 runtime.events_dir。
//
// 动机（2026-09-27 用户需求）: 服务器上常驻跑的是本进程（扫尾盘 live），而 BTC 的
// 原始采集（cmd/collect）自 2026-08-31 起已停——离线研究（止损成交换手、成交量、
// OFI…）没有逐秒原始盘口/Binance 微观数据可用。本进程顺手把它采下来，不新增进程、
// 不新增 flag（部署脚本把参数都放配置文件里）。
//
// 三条设计要求:
//
//  1. **不拖累交易路径**（这是活钱进程）: 采样在**独立 goroutine** 里自带 1s ticker，
//     交易 tick 循环一行不改——那个循环里有同步 gamma 预取、live POST、持仓监察落盘，
//     卡顿会丢 tick（校验脚本 B 段要求 tick 数落在 [293,302]）。落盘失败只记日志；
//     两个 goroutine 入口各有 recover（Go 里未恢复的 panic 会杀掉整个进程）。
//  2. **与 data/btc 口径逐项对齐**: 下游 python（python/v2/lib.py、
//     python/v4/24_asset_data_check.py）按同一套字段读，差一处就是一个 FAIL 段。
//     逐条口径见 anchorSourceFor / outcomeFor / sample / buildEvent 的注释。
//  3. **宁可留洞，不写脏行**（三条红线，见 buildEvent）: 元数据不全 / 半窗 / 迟入
//     一律整窗丢弃，各打一行 ⚠️（丢窗必须有痕）。跳窗路径**从不 BeginWindow** ⇒
//     那些窗天然一行不产出。
//
// ⚠️ 与 cmd/collect 的关系: **有意不去重构它**。两处代码形状相近（都调
// internal/collect 的 MakePMTick/TradeBucketer/SettlementWorker/WriteUniqueEvent），
// 但把采集循环下沉进 internal/collect 会让格式层反向依赖 feed 的数据源层；
// 且两族同目录双写会互相盖行（同 start_time 判重跳过）。这是刻意的重复。
package main

import (
	"context"
	"log"
	"strconv"
	"sync"
	"time"

	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"

	"github.com/necklace/flip-signal/internal/collect"
	"github.com/necklace/flip-signal/internal/config"
	"github.com/necklace/flip-signal/internal/feed"
)

// ── 采集参数（与 cmd/collect 同值; 括号内是该值的出处）──
const (
	// closePushWait 是收盘推送的等待预算（决策 #19 的推送层时点: 闭市 +10s）。
	// 闭市那一秒的推送实测只有 +1s/+2s 两档、无一晚于 +2s ⇒ 10s = 5 倍上界。
	closePushWait = 10 * time.Second
	// closePushPoll 是收盘推送的轮询节拍（与取锚通道同为 500ms）。
	closePushPoll = 500 * time.Millisecond
	// klineOpenDelay 是 Binance 5m K 线开盘价的取值延迟: 新 K 线在边界后 1~2s
	// 才稳定生成（与 cmd/collect 同值）。
	klineOpenDelay = 2 * time.Second
	// tickMin 是单窗 tick 数下限——**指向校验脚本的 TICK_MIN**
	// （python/v4/24_asset_data_check.py:60 `TICK_MIN, TICK_MAX = 293, 302`）。
	// 少于它 = 本进程没采满整窗，落盘会让 B 段直接 FAIL。
	tickMin = 293
	// firstRemMin 是首 tick 的 rem 下限: 正常窗首 tick 在边界后 ~1s（rem≈299），
	// 迟入窗口会让它线性变小。取 295 = 容忍边界后 ~5s 才开跑——本族的 lateLimit
	// 是 15s（远宽于 cmd/collect 的 2s），闸门必须在这里自己卡到与脚本一致的位置。
	firstRemMin = 295
)

// 成交行的 token 取值（与 internal/collect.TradeAgg.Token 口径一致, cmd/collect 同）。
const (
	tokenYes = "YES"
	tokenNo  = "NO"
)

// tradeSink 是一个窗口的成交归属: 聚合桶 + 该窗**自己**的 YES/NO token 表。
//
// ⚠️ token 表必须随桶走，不能像 cmd/collect 那样用一对全局 prev 变量: 本族的
// upTok/downTok 每窗换装，而收盘后 1~2s 才推送的尾盘笔要回填进**上一窗**的桶——
// 那一刻全局变量已指向新窗 token，拿它映射会把上一窗的笔判成「陌生 token」丢掉。
type tradeSink struct {
	startMs int64 // 窗口起点（unix 毫秒; 桶的归属判据）
	yesTok  string
	noTok   string
	bucket  *collect.TradeBucketer
}

// tokenFor 用本窗的 token 表把 asset_id 映射成 YES/NO；空串 = 陌生 token（丢弃）。
func (s *tradeSink) tokenFor(assetID string) string {
	switch assetID {
	case s.yesTok:
		return tokenYes
	case s.noTok:
		return tokenNo
	default:
		return ""
	}
}

// pickSink 按成交**自身时戳**选桶（纯函数，表驱测试）。
//
// 先 cur 后 prev: tsMs 落在本窗起点之后进本窗；否则若还在上一窗起点之后，进上一窗
// （边界后 1~3s 才推送的尾盘笔靠这条回填，与 cmd/collect 保留旧 bucketer 同一目的）。
// 两者都不匹配 = 更早窗口的迟到笔（或本进程没采的跳窗）⇒ 丢弃。
//
// ⚠️ 越界（ts 落在桶窗口之外，如 startMs+300s 之后）不在这里判: TradeBucketer.Add
// 自己会按 idx ∈ [0, WindowSec) 丢——判定单一来源，两处各判一套容易打架。
func pickSink(tsMs int64, cur, prev *tradeSink) *tradeSink {
	switch {
	case cur != nil && tsMs >= cur.startMs:
		return cur
	case prev != nil && tsMs >= prev.startMs:
		return prev
	default:
		return nil
	}
}

// eventWindow 是**一个窗口**的采集现场。由该窗的采样 goroutine 独占（ticks 只在
// 它手里增长），唯一的外部写者是取锚 goroutine 经 SetAnchor 写锚 ⇒ 用 mu 保护。
type eventWindow struct {
	condID string
	slug   string
	start  time.Time
	end    time.Time

	binanceOpen float64    // 本窗 Binance 5m K 线开盘价（只由采样 goroutine 写）
	sink        *tradeSink // 本窗成交槽（成交消费 goroutine 并发写它的 bucket）

	mu          sync.Mutex
	ticks       []collect.HFTick
	anchorPrice float64
	anchorSrc   string // collect 词表（anchorSourceFor 的产物; 空 = 锚未到手）
}

// setAnchor 记录本窗锚（取锚 goroutine 调用；窗口已换装则丢弃）。
func (w *eventWindow) setAnchor(price float64, src string) {
	w.mu.Lock()
	w.anchorPrice, w.anchorSrc = price, src
	w.mu.Unlock()
}

// binanceSource 是采集器需要的 Binance 侧能力（真身 = *feed.BinanceAdapter）。
// 抽成接口只有一个目的: 让采样组装能被单测直接驱动——适配器的行情字段是私有的，
// 测试注入不进去（生产路径只有一个实现，无第二实现负担）。
type binanceSource interface {
	LatestData() feed.BinanceMarketData
	ConsumeVolume() (buyAcc, sellAcc float64)
	FetchKlineOpenPrice() float64
}

// eventCollector 是采集器的落盘侧（与交易引擎零共享状态）。
type eventCollector struct {
	dir     string
	binance binanceSource
	twap    *feed.TwapAdapter
	books   func() (*sdk.OrderBook, *sdk.OrderBook)
	worker  *collect.SettlementWorker

	// mu 保护窗口换装: 主循环 BeginWindow 写、采样 goroutine 收尾 release 写、
	// 成交消费 goroutine 读。
	mu   sync.Mutex
	cur  *eventWindow
	prev *tradeSink // 上一窗的成交槽（尾盘迟到笔按自身时戳回填）
}

// newEventCollector 构造采集器并启动落盘 worker。
//
// rtCfg.EventsDir 为空串 ⇒ 返回 nil（采集关闭；全部方法都是 nil-safe）。默认值是
// "data/events" ⇒ **默认开启**，服务器那份手工维护的 tail 配置没有这个键，走默认值
// 即自动生效（见 internal/config 的三层加载）。
//
// client/asset 只用于官方价取数回调（推送缺失窗的修正层，与 cmd/collect 同源）。
func newEventCollector(ctx context.Context, rtCfg config.RuntimeConfig, client *sdk.PolymarketClient,
	asset feed.Asset, binance binanceSource, twap *feed.TwapAdapter,
	books func() (*sdk.OrderBook, *sdk.OrderBook)) *eventCollector {
	if rtCfg.EventsDir == "" {
		log.Println("[Events] 原始采集关闭（runtime.events_dir 为空串）")
		return nil
	}
	if rtCfg.EventsDir == rtCfg.OutputDir {
		// 不是硬错误（前缀不同, 不会互相盖行），但两个格式混在一个目录里迟早有人看错。
		log.Printf("[Events] ⚠️ events_dir 与 output_dir 相同（%s）—— 原始事件与 tail_ 观测行会混在"+
			"同一目录（前缀不同不冲突）, 建议分开", rtCfg.EventsDir)
	}

	c := &eventCollector{
		dir:     rtCfg.EventsDir,
		binance: binance,
		twap:    twap,
		books:   books,
	}
	// 结算修正 worker（内部自带 goroutine）: 推送缺失的窗才排队官方修正。
	pairFetch := feed.NewPricePairFetcher(client, asset.Chainlink, sdk.Fiveminute, twapLookbackSeconds)
	c.worker = collect.NewSettlementWorker(rtCfg.EventsDir,
		func(ctx context.Context, start time.Time) (float64, float64, bool) {
			open, close := pairFetch(ctx, start, start.Add(collect.WindowSec*time.Second))
			return open, close, open > 0 && close > 0
		}, collect.DefaultSettlementConfig())
	c.worker.Start(ctx)

	log.Printf("[Events] 📥 原始采集开启: %s（数据格式 v2, 每窗一行 ticks+trades, 与 data/btc 同构）",
		rtCfg.EventsDir)
	return c
}

// BeginWindow 换装到新窗口并启动本窗的采样 goroutine。
//
// 调用点即**窗口起点**（主循环刚等到边界），故首 tick 落在边界后 ~1s（rem≈299）。
// 跳窗路径（late/no_market/no_token/dup_record/no_sigma）**刻意不调用它** ⇒ 那些窗
// 一行事件都不产出（语料洞: 下游按 start_time 对齐，不能按行数）。
func (c *eventCollector) BeginWindow(ctx context.Context, condID, slug string,
	start time.Time, upTokenID, downTokenID string) {
	if c == nil {
		return
	}
	w := &eventWindow{
		condID: condID,
		slug:   slug,
		start:  start,
		end:    start.Add(collect.WindowSec * time.Second),
		sink: &tradeSink{
			startMs: start.UnixMilli(),
			yesTok:  upTokenID,
			noTok:   downTokenID,
			bucket:  collect.NewTradeBucketer(start.UnixMilli()),
		},
	}
	c.mu.Lock()
	if c.cur != nil {
		c.prev = c.cur.sink // 上一窗的桶留给边界后迟到的尾盘笔
	}
	c.cur = w
	c.mu.Unlock()

	go c.runWindow(ctx, w)
}

// SetAnchor 记录本窗锚（取锚 goroutine 调用，与 settle.Anchors.Put 并排）。
//
// ⚠️ **无条件**调用: 引擎 UpgradeAnchor 可能拒收（本窗已出信号 ⇒ 锚冻结），但锚值
// 本身仍是官方 open 口径——事件行要的就是它，与引擎是否接纳无关。
//
// eventStart 是防串窗的硬判据: 只有起点与当前窗口一致才写入（时间上锚总在 +2s 到手、
// 窗口终点在 +300s，正常不会错，但判据不该依赖时序巧合）。
func (c *eventCollector) SetAnchor(eventStart int64, price float64, feedSrc string) {
	if c == nil || price <= 0 {
		return
	}
	c.mu.Lock()
	w := c.cur
	c.mu.Unlock()
	if w == nil || w.start.Unix() != eventStart {
		return
	}
	w.setAnchor(price, anchorSourceFor(feedSrc))
}

// release 在本窗定稿后摘掉它的成交槽（此后更早窗口的迟到笔不再有桶可进）。
// 不摘也不出错（桶自己会按 idx 越界丢），摘是为了不把桶挂在内存里。
func (c *eventCollector) release(w *eventWindow) {
	c.mu.Lock()
	if c.prev == w.sink {
		c.prev = nil
	}
	c.mu.Unlock()
}

// ConsumeTrades 启动成交流消费（调用方 `go` 出去即可）。
//
// ⚠️ 必须在**采集开着**时才调用: SubscribeLastTradePrice 只是把 SDK 的解析开关打开
// （subscribe 上行报文与它无关，不会多订任何流），但没人消费的通道会积压到 4096 条后
// 被 SDK 丢最旧——白做功。故 nil 采集器直接返回，连订阅都不开。
func (c *eventCollector) ConsumeTrades(ctx context.Context, m *sdk.MarketMonitor) {
	if c == nil {
		return
	}
	c.consumeTrades(ctx, m.SubscribeLastTradePrice())
}

// consumeTrades 消费成交通道（channel 由调用方给，便于表驱测试）。
func (c *eventCollector) consumeTrades(ctx context.Context, ch <-chan *sdk.LastTradePriceInfo) {
	defer func() {
		if r := recover(); r != nil {
			// 采集侧 panic 绝不允许杀进程（活钱进程）——停用成交采集，交易照跑。
			log.Printf("[Events] 🔴 成交采集 goroutine panic: %v —— 成交采集停用（事件行将无 trades），交易不受影响", r)
		}
	}()

	for {
		select {
		case <-ctx.Done():
			return
		case trade := <-ch:
			if trade == nil {
				continue
			}
			// 成交自身时戳（消息内服务端时间）——归桶判据，缺时戳回退本地到达时刻
			// （与 cmd/collect 同）。
			tsMs := trade.Timestamp
			if tsMs == 0 {
				tsMs = time.Now().UnixMilli()
			}
			c.mu.Lock()
			var cur *tradeSink
			if c.cur != nil {
				cur = c.cur.sink
			}
			sink := pickSink(tsMs, cur, c.prev)
			c.mu.Unlock()
			if sink == nil {
				continue // 更早窗口的迟到笔 / 本进程没采的跳窗
			}
			token := sink.tokenFor(trade.AssetID)
			if token == "" {
				continue // 陌生 token
			}
			// 盘口上下文取**当前**簿（成交后簿可能已被后续更新覆盖——本地到达时刻的
			// 近似上下文，与 cmd/collect 同口径；行内 best_bid/ask 只作参考）。
			yb, nb := c.books()
			var bb, ba float64
			if token == tokenYes {
				bb, ba = collect.BestBid(yb), collect.BestAsk(yb)
			} else {
				bb, ba = collect.BestBid(nb), collect.BestAsk(nb)
			}
			sink.bucket.Add(token, tsMs, trade.Side,
				parseFloat(trade.Price), parseFloat(trade.Size), bb, ba, trade.TransactionHash)
		}
	}
}

// runWindow 是一个窗口的采集 goroutine: 1s ticker 采样 → 终点补采 → 等精确收盘推送
// → 组装 → Submit。**独立于交易 tick 循环**（见文件头第 1 条）。
func (c *eventCollector) runWindow(ctx context.Context, w *eventWindow) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("[Events] 🔴 窗口 %s 采集 goroutine panic: %v —— 本窗不落盘，交易不受影响", w.slug, r)
		}
		c.release(w)
	}()

	// 开盘价初值 = 边界那一刻的现货价；K 线值 +2s 到达后覆盖（校验 D 段要求它与
	// 5m K 线开盘**逐位相等**，初值只是兜底）。
	w.binanceOpen = c.binance.LatestData().Price
	// 丢弃窗口间隙累计的成交量: 上一窗收尾 + 市场切换期间的成交不属于本窗首秒，
	// 否则首 tick 出现周期性量尖峰（cmd/collect 实测发现的数据质量 bug）。
	c.binance.ConsumeVolume()

	klineCh := c.klineOpenAsync(ctx)

	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()

sampleLoop:
	for {
		select {
		case <-ctx.Done():
			return // 关闭: 在途窗直接丢弃（绝不补写半窗——红线条 2/3）
		case open := <-klineCh:
			if open > 0 {
				w.binanceOpen = open
			}
		case tickTime := <-ticker.C:
			if !tickTime.Before(w.end) {
				break sampleLoop
			}
			c.sample(w, tickTime)
		}
	}

	// 补采窗口终点快照（rem=0）: ticker 的最后一拍落在终点前，不补就缺最后一秒的
	// 量增量（cmd/collect 实测每窗 298 ticks 而非 300 的同一条）。
	c.sample(w, w.end)

	// 收盘推送: 精确匹配「评估时刻 == 闭市那一秒」的那条 = 官方 closePrice（决策 #19）。
	closePush := c.waitClosePush(ctx, w.end)

	ev, reason := c.buildEvent(w, closePush)
	if ev == nil {
		log.Printf("[Events] ⚠️ 窗口 %s 不落盘: %s", w.slug, reason)
		return
	}
	needsCorrection := ev.CloseSource != collect.SourcePush || ev.AnchorSource != collect.SourcePush
	log.Printf("[Events] 📥 窗口 %s 采集完成: ticks=%d trades=%d close=%.2f(%s) outcome=%d",
		w.slug, len(ev.Ticks), len(ev.Trades), ev.TwapClosePrice, ev.CloseSource, ev.Outcome)
	c.worker.Submit(ev, needsCorrection)
}

// klineOpenAsync 起一个 goroutine 在边界后 klineOpenDelay 取 5m K 线开盘价，返回
// 结果通道（0 = 失败，调用方沿用初值）。**必须异步**: 取数是同步 HTTP（5s 超时），
// 放在采样循环里会让 ticker 停摆 ⇒ 丢 tick。
func (c *eventCollector) klineOpenAsync(ctx context.Context) <-chan float64 {
	ch := make(chan float64, 1)
	go func() {
		defer func() {
			if r := recover(); r != nil {
				log.Printf("[Events] ⚠️ K 线开盘价采集 panic: %v（沿用现货初值）", r)
			}
		}()
		select {
		case <-ctx.Done():
			return
		case <-time.After(klineOpenDelay):
		}
		open := c.binance.FetchKlineOpenPrice()
		select {
		case ch <- open:
		default:
		}
	}()
	return ch
}

// sample 采一行 1s 快照（ticker 与终点补采共用）。
//
// ⚠️ Binance 侧一律用 **LatestData() 原始值**，不套引擎 tick 的陈旧钳零: 引擎把
// 超龄 spot 钳成 0 是判定要的（missing_spot），但事件行里 bin.price ≤ 0 是校验 D 段
// 的硬 FAIL——原始数据要的是「那一刻收到了什么」。同理**不复用 lastTick**: 它是
// flip.Tick，没有量/深度四档。
func (c *eventCollector) sample(w *eventWindow, tickTime time.Time) {
	rem := max(int(w.end.Sub(tickTime).Seconds()), 0)
	// 先读累计（价格/深度快照 + 自上次 ConsumeVolume 以来的买卖量与笔数），再清零
	// ——bd 中即本秒增量。⚠️ 本采集器是**唯一**消费者（引擎不读量字段）。
	bd := c.binance.LatestData()
	c.binance.ConsumeVolume()
	yb, nb := c.books()
	twPrice, twAge := c.twap.Latest()

	w.mu.Lock()
	w.ticks = append(w.ticks, collect.HFTick{
		Ts:  tickTime.UnixMilli(),
		Rem: rem,
		Bin: collect.BinTick{
			Price:   bd.Price,
			BuyVol:  bd.BuyVolume,
			SellVol: bd.SellVolume,
			Ticks:   int(bd.TradeCount),
			Bid5:    bd.BidDepth5,
			Ask5:    bd.AskDepth5,
			Bid10:   bd.BidDepth10,
			Ask10:   bd.AskDepth10,
		},
		PM:   collect.MakePMTick(yb, nb),
		Twap: collect.TwapTick{Price: twPrice, AgeMs: twAge},
	})
	w.mu.Unlock()
}

// waitClosePush 等「评估时刻恰好等于 at」的那条 TWAP 推送（= 官方 closePrice，
// 逐位同源，决策 #19）。命中返回价格，预算耗尽或 ctx 取消返回 0（调用方回退流值
// 并触发官方修正层）。
func (c *eventCollector) waitClosePush(ctx context.Context, at time.Time) float64 {
	deadline := time.Now().Add(closePushWait)
	for {
		if p, _, ok := c.twap.PushNearest(at); ok {
			return p
		}
		if !time.Now().Before(deadline) {
			return 0
		}
		select {
		case <-ctx.Done():
			return 0
		case <-time.After(closePushPoll):
		}
	}
}

// buildEvent 组装事件行并执行三条落盘红线；返回 nil 表示本窗不落盘（reason 进日志）。
//
// 红线（宁可留洞，不写脏行——半个窗的语料比没有更坏: 下游按行数/覆盖度做的统计
// 会被静默带偏）:
//  1. **元数据不全**: 锚 ≤ 0（决策 #15: 0 锚会让下游特征算出 inf/垃圾）或
//     Binance 开盘价 ≤ 0（校验 A 段硬 FAIL）。
//  2. **半窗**: tick 数 < tickMin（校验 B 段硬 FAIL）。
//  3. **迟入窗口**: 首 tick rem < firstRemMin ⇒ tick 数与 rem 身份都对不上脚本期望。
func (c *eventCollector) buildEvent(w *eventWindow, closePush float64) (*collect.Event, string) {
	w.mu.Lock()
	ticks := w.ticks
	anchor, anchorSrc := w.anchorPrice, w.anchorSrc
	w.mu.Unlock()

	if anchor <= 0 {
		return nil, "锚缺失（决策 #15: 锚 ≤ 0 的窗口一行不产出）"
	}
	if w.binanceOpen <= 0 {
		return nil, "Binance 未就绪（binance_open ≤ 0）"
	}
	if len(ticks) < tickMin {
		return nil, "半窗（tick 数 " + strconv.Itoa(len(ticks)) + " < " + strconv.Itoa(tickMin) +
			"，进程迟入或采样卡顿）"
	}
	if ticks[0].Rem < firstRemMin {
		return nil, "迟入窗口（首 tick rem=" + strconv.Itoa(ticks[0].Rem) + " < " +
			strconv.Itoa(firstRemMin) + "）"
	}

	// 收盘价: 优先边界推送（与官方逐位同源）; 缺推送才回退到达口径流值（并触发官方
	// 修正层）。⚠️ 流值也可能为 0（TWAP 断流）——此时回退锚价: 校验 A 段要求
	// twap_close_price > 0（硬 FAIL），而 |close−open| = 0 只是一个「振幅为 0 的窗」，
	// 比整窗丢弃温和（该窗的 outcome 恒为 Up，close_source=stream 已留痕）。
	closePrice, closeSrc := closePush, collect.SourcePush
	if closePrice <= 0 {
		closePrice, _ = c.twap.Latest()
		closeSrc = collect.SourceStream
		if closePrice <= 0 {
			closePrice = anchor
		}
	}

	return &collect.Event{
		ConditionID:    w.condID,
		Slug:           w.slug,
		StartTime:      w.start.Unix(),
		TwapOpenPrice:  anchor,
		TwapClosePrice: closePrice,
		CloseSource:    closeSrc,
		AnchorSource:   anchorSrc,
		Outcome:        outcomeFor(anchor, closePrice),
		BinanceOpen:    w.binanceOpen,
		Ticks:          ticks,
		Trades:         w.sink.bucket.Snapshot(),
	}, ""
}

// anchorSourceFor 把 feed 的锚来源词表翻译成事件行的词表（与 cmd/collect 同源）。
//
// ⚠️ 两个包的 "stream" **不是一回事**（同名不同义，最容易串的地方）:
//   - feed.AnchorSourceStream = 评估时刻恰好等于窗口边界的那条 TWAP 推送 =
//     精确边界推送（决策 #15），它就是官方 openPrice（决策 #19）⇒ collect.SourcePush
//   - collect.SourceStream    = **到达口径**采样（Latest()，可能陈旧 ~2s）⇒ 近似锚
//
// 不翻译直接落盘: 取锚成功的窗会被标成「到达口径」，校验脚本 G 段把它归进 legacy
// 分档（不再要求与官方逐位相等，白丢一道红线），且 needsCorrection 恒真 ⇒ 每窗白跑
// 一次官方修正轮询。
func anchorSourceFor(feedSrc string) string {
	switch feedSrc {
	case feed.AnchorSourceOfficial:
		return collect.SourceOfficial
	default: // feed.AnchorSourceStream（精确边界推送）
		return collect.SourcePush
	}
}

// outcomeFor 按事件行口径判胜负: close >= open → Up(0)，否则 Down(1)。
// ⚠️ **平局算 Up**——与 internal/settle.Outcome（严格大于）有意不同，校验脚本 H 段
// 写死的就是本式（与 cmd/collect 逐字同源）。
func outcomeFor(open, close float64) int {
	if close >= open {
		return 0
	}
	return 1
}

// parseFloat 解析价格/数量字符串，失败返回 0（SDK last_trade_price 字段为字符串）。
func parseFloat(s string) float64 {
	f, _ := strconv.ParseFloat(s, 64)
	return f
}
