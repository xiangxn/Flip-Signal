// Package collect 实现高频数据采集（1s 分辨率 + PM 逐笔成交聚合）的
// 结构与工具，服务 cmd/collect。
//
// 数据格式 v2 见 docs/recollection_plan_2026-08-16.md：
// 每个 5 分钟窗口一行 JSON（事件元数据 + ticks 1s 数组 + trades 聚合数组），
// 一标的一目录（BTC → data/btc、ETH → data/eth；启动前须把同名旧格式目录
// 移走，见文档 §3.1 警告）。合并修正行用 cmd/compact。
//
// 2026-09-25 同步自 eth 分支，两处口径与 v4 引擎对齐（见 CLAUDE.md 决策 #23）：
//   - 窗口开/收盘价取 **边界那一秒的 TWAP 推送**（精确匹配, 决策 #15），它与
//     官方 openPrice/closePrice 逐位同源（决策 #19 实测 315/315 与 309/309）,
//     故事件行默认即官方口径（close_source=push）；官方 HTTP 只在推送缺失时兜底。
//   - 空侧盘口不再丢弃整条 book 消息（决策 #21）：照存 ⇒ 该侧价格为 0，
//     下游（含 python 分析）按「四档缺一即无效 tick」同口径处理。
package collect

import "time"

// 事件行的价格来源标记（Event.CloseSource / Event.AnchorSource）。
const (
	// SourcePush 边界那一秒的 TWAP 推送（精确匹配）——**默认口径**，
	// 与官方开/收盘逐位同源（决策 #19），无需再拉官方接口。
	SourcePush = "push"
	// SourceOfficial 官方 crypto-price 接口（polymarket.com）兜底：
	// 推送缺失时才用，产出延迟为分钟级。
	SourceOfficial = "official"
	// SourceStream TWAP 流的**到达口径**采样（Latest()，可能陈旧 ~2s）。
	// 只在推送缺失且官方接口也不可用时兜底，行上留标记供分析侧区分。
	SourceStream = "stream"
)

// BinTick 是 1 秒 Binance 聚合行（P0-1）。
type BinTick struct {
	Price   float64 `json:"price"`    // 本秒最新价
	BuyVol  float64 `json:"buy_vol"`  // 本秒主动买量（增量）
	SellVol float64 `json:"sell_vol"` // 本秒主动卖量（增量）
	Ticks   int     `json:"ticks"`    // 本秒成交笔数
	Bid5    float64 `json:"bid5"`     // depth20 前 5 档买量之和
	Ask5    float64 `json:"ask5"`     // depth20 前 5 档卖量之和
	Bid10   float64 `json:"bid10"`    // depth20 前 10 档买量之和
	Ask10   float64 `json:"ask10"`    // depth20 前 10 档卖量之和
}

// PMTick 是 1 秒 Polymarket 盘口快照行（P0-2）。
type PMTick struct {
	YesBid     float64 `json:"yes_bid"`
	YesAsk     float64 `json:"yes_ask"`
	YesBidTop5 float64 `json:"yes_bid_top5"` // 前 5 档数量（股）
	YesAskTop5 float64 `json:"yes_ask_top5"`
	NoBid      float64 `json:"no_bid"`
	NoAsk      float64 `json:"no_ask"`
	NoBidTop5  float64 `json:"no_bid_top5"`
	NoAskTop5  float64 `json:"no_ask_top5"`
	BookTs     int64   `json:"book_ts"`         // 盘口更新时戳（毫秒，reprice 速度）
	BookLatMs  int64   `json:"book_latency_ms"` // 传输延迟（毫秒）
}

// TwapTick 是 TWAP-60 流采样（每 tick 附带）。
type TwapTick struct {
	Price float64 `json:"price"`
	AgeMs int64   `json:"age_ms"`
}

// HFTick 是每秒一条的采集行（Binance + PM + TWAP 合并）。
type HFTick struct {
	Ts   int64    `json:"ts"`  // 本地采样时刻（unix 毫秒）
	Rem  int      `json:"rem"` // 窗口剩余秒数
	Bin  BinTick  `json:"bin"`
	PM   PMTick   `json:"pm"`
	Twap TwapTick `json:"twap"`
}

// TradeAgg 是 PM last_trade_price 按 token × 秒聚合行（P0-3）。
//
// 原始逐笔数据量过大不落盘（见数据重采计划 §3.2），在内存中聚合：
// 每 token 每秒一行，保留主动买卖笔数/量（OFI）、最大单笔（大单检测）、
// vwap 与末笔 best bid/ask；transaction_hash 等逐笔字段不保留
// （hash 仅用于窗口内去重，防 WS 重连重放）。
type TradeAgg struct {
	Ts        int64   `json:"ts"`        // 本秒桶起始（unix 毫秒，与 ticks 对齐）
	Rem       int     `json:"rem"`       // 窗口剩余秒数
	Token     string  `json:"token"`     // YES / NO（按订阅 token 映射）
	NBuy      int     `json:"n_buy"`     // 本秒主动买笔数
	BuySize   float64 `json:"buy_size"`  // 本秒主动买量（股）
	NSell     int     `json:"n_sell"`    // 本秒主动卖笔数
	SellSize  float64 `json:"sell_size"` // 本秒主动卖量（股）
	MaxSize   float64 `json:"max_size"`  // 本秒最大单笔（股）
	VWAP      float64 `json:"vwap"`      // 本秒成交量加权均价
	LastPrice float64 `json:"last_price"`
	BestBid   float64 `json:"best_bid"`
	BestAsk   float64 `json:"best_ask"`
}

// SettlementCorrection 是官方结算价产出后对已落盘事件的修正行。
//
// 只用于**推送缺失**的窗口（默认口径 close_source=push 时推送与官方逐位同源，
// 无需修正，见决策 #19）：SettlementWorker 后台轮询官方 open/close，
// 到达后追加修正行（与事件行同文件）。分析侧按 start_time 合并覆盖；
// EventType 区分行类型。
type SettlementCorrection struct {
	EventType      string  `json:"event_type"` // 恒为 "settlement_correction"
	StartTime      int64   `json:"start_time"`
	TwapOpenPrice  float64 `json:"twap_open_price"`
	TwapClosePrice float64 `json:"twap_close_price"`
	CloseSource    string  `json:"close_source"`  // SourceOfficial
	AnchorSource   string  `json:"anchor_source"` // SourceOfficial
	Outcome        int     `json:"outcome"`       // 0=Up 1=Down（官方口径）
}

// SettlementConfig 是结算修正队列的参数。
//
// ⚠️ 2026-09-25 同步时删掉了 eth 版的 `MinRange`（幅度阈值 $15）与
// `MaxStreamAgeMs`：这两个阈值是为「流采样收盘 vs 官方边界值」的量化误差
// 标定的（|close−open| 小到可能翻转 outcome 时才去拉官方）。改用精确边界
// 推送后，close 与官方**同一个值**，量化误差归零 ⇒ 修正的唯一触发条件变成
// 「这一窗没拿到推送」，与幅度/新鲜度无关。
type SettlementConfig struct {
	// PollInterval 官方价轮询间隔（默认 20s，附 0-2s 抖动避免多窗同拍）
	PollInterval time.Duration
	// MaxWait 单窗修正的最长等待（默认 55 分钟；官方收盘实测延迟为分钟级，
	// 偶尔数十分钟。到期放弃，事件保留推送/流值口径）
	MaxWait time.Duration
	// MaxConcurrent 并发修正轮询上限（默认 8；窗口 5 分钟一个，MaxWait 55 分钟
	// 时最多 11 窗在途，8 并发 + FIFO 足够）
	MaxConcurrent int
}

// DefaultSettlementConfig 返回结算修正队列的默认参数。
func DefaultSettlementConfig() SettlementConfig {
	return SettlementConfig{
		PollInterval:  20 * time.Second,
		MaxWait:       55 * time.Minute,
		MaxConcurrent: 8,
	}
}

// Event 是每个 5 分钟窗口的完整记录（数据格式 v2 顶层结构）。
type Event struct {
	ConditionID    string  `json:"condition_id"`
	Slug           string  `json:"slug"`
	StartTime      int64   `json:"start_time"` // unix 秒（窗口起点）
	TwapOpenPrice  float64 `json:"twap_open_price"`
	TwapClosePrice float64 `json:"twap_close_price"`
	// CloseSource 收盘价来源（SourcePush|SourceOfficial|SourceStream）：
	// 默认 push = 边界那一秒的推送，与官方 closePrice 逐位同源（决策 #19）；
	// stream = 推送缺失时回退到到达口径采样（本行会触发官方修正）。
	CloseSource string `json:"close_source"`
	// AnchorSource 开盘价（锚）来源，取值同上。锚口径必须与 v4 引擎一致
	// （决策 #15）：引擎判定用的 anchor 就是精确边界推送。
	AnchorSource string     `json:"anchor_source"`
	Outcome      int        `json:"outcome"` // 0=Up 1=Down（close >= open → Up）
	BinanceOpen  float64    `json:"binance_open"`
	Ticks        []HFTick   `json:"ticks"`
	Trades       []TradeAgg `json:"trades"`
}
