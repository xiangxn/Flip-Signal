// Package collect 实现高频数据采集（1s 分辨率 + PM 逐笔成交聚合）的
// 结构与工具，服务 cmd/collect。
//
// 数据格式 v2 见 docs/recollection_plan_2026-08-16.md：
// 每个 5 分钟窗口一行 JSON（事件元数据 + ticks 1s 数组 + trades 聚合数组），
// 写入 data/btc/（启动前须把旧 5s 快照目录移走，见文档 §3.1 警告）。
package collect

// BinTick 是 1 秒 Binance 聚合行（P0-1）。
type BinTick struct {
	Price    float64 `json:"price"`     // 本秒最新价
	BuyVol   float64 `json:"buy_vol"`   // 本秒主动买量（增量）
	SellVol  float64 `json:"sell_vol"`  // 本秒主动卖量（增量）
	Ticks    int     `json:"ticks"`     // 本秒成交笔数
	Bid5     float64 `json:"bid5"`      // depth20 前 5 档买量之和
	Ask5     float64 `json:"ask5"`      // depth20 前 5 档卖量之和
	Bid10    float64 `json:"bid10"`     // depth20 前 10 档买量之和
	Ask10    float64 `json:"ask10"`     // depth20 前 10 档卖量之和
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
	Ts  int64    `json:"ts"`  // 本地采样时刻（unix 毫秒）
	Rem int      `json:"rem"` // 窗口剩余秒数
	Bin BinTick  `json:"bin"`
	PM  PMTick   `json:"pm"`
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

// Event 是每个 5 分钟窗口的完整记录（数据格式 v2 顶层结构）。
type Event struct {
	ConditionID    string     `json:"condition_id"`
	Slug           string     `json:"slug"`
	StartTime      int64      `json:"start_time"` // unix 秒（窗口起点）
	TwapOpenPrice  float64    `json:"twap_open_price"`
	TwapClosePrice float64    `json:"twap_close_price"`
	// CloseSource 标记收盘价来源: "official"=官方 crypto-price 接口
	// （60s 内到达）；"stream"=窗口末 TWAP-60 流采样定稿（官方延迟可达
	// 数十分钟，60s 未产出即不再等）
	CloseSource    string     `json:"close_source"`
	Outcome        int        `json:"outcome"` // 0=Up 1=Down（close >= open → Up）
	BinanceOpen    float64    `json:"binance_open"`
	Ticks          []HFTick   `json:"ticks"`
	Trades         []TradeAgg `json:"trades"`
}
