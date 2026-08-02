package feed

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"
)

// BinanceMarketData holds aggregated market data from Binance WebSocket streams.
type BinanceMarketData struct {
	Price     float64 // Latest trade price
	OpenPrice float64 // Current 5-minute kline open price (refreshed per cycle)

	// Volume since last snapshot (reset on each ConsumeVolume call)
	BuyVolume1s  float64
	SellVolume1s float64

	// Cumulative volume for the window
	BuyVolume10s  float64
	SellVolume10s float64

	// Order book depth
	BidDepth5  float64
	AskDepth5  float64
	BidDepth10 float64
	AskDepth10 float64

	// Timestamp
	UpdatedAt int64 // unix milliseconds
}

// BinanceConfig holds configuration for the Binance adapter.
type BinanceConfig struct {
	Symbol        string // e.g. "BTCUSDT"
	StreamBaseURL string // e.g. "wss://stream.binance.com:9443"
	RestBaseURL   string // e.g. "https://data-api.binance.vision"
}

func DefaultBinanceConfig() BinanceConfig {
	return BinanceConfig{
		Symbol:        "BTCUSDT",
		StreamBaseURL: "wss://stream.binance.com:9443",
		RestBaseURL:   "https://data-api.binance.vision",
	}
}

// BinanceAdapter connects to Binance WebSocket for real-time market data.
// Uses a SINGLE WebSocket connection with combined streams:
//   - <symbol>@trade        → real-time trades (price + buy/sell volume)
//   - <symbol>@depth20@100ms → order book depth (bid/ask levels)
//
// The WS connection persists across market cycles. OpenPrice is refreshed
// per cycle via FetchKlineOpenPrice().
type BinanceAdapter struct {
	cfg BinanceConfig

	conn   *websocket.Conn
	connMu sync.Mutex

	data   BinanceMarketData
	dataMu sync.RWMutex

	volMu       sync.Mutex
	buyVol1s    float64
	sellVol1s   float64
	buyVol10s   float64
	sellVol10s  float64
	volSnapshot [10]volBucket
	volIdx      int

	dataCh chan BinanceMarketData

	started atomic.Bool
}

type volBucket struct {
	buy, sell float64
}

func NewBinanceAdapter() *BinanceAdapter {
	return NewBinanceAdapterWithConfig(DefaultBinanceConfig())
}

func NewBinanceAdapterWithConfig(cfg BinanceConfig) *BinanceAdapter {
	if cfg.Symbol == "" {
		cfg.Symbol = "BTCUSDT"
	}
	if cfg.StreamBaseURL == "" {
		cfg.StreamBaseURL = "wss://stream.binance.com:9443"
	}
	if cfg.RestBaseURL == "" {
		cfg.RestBaseURL = "https://api.binance.com"
	}
	cfg.Symbol = strings.ToUpper(cfg.Symbol)

	return &BinanceAdapter{
		cfg:    cfg,
		dataCh: make(chan BinanceMarketData, 128),
	}
}

func (b *BinanceAdapter) Symbol() string { return b.cfg.Symbol }
func (b *BinanceAdapter) Started() bool  { return b.started.Load() }

func (b *BinanceAdapter) streamURL() string {
	symbol := strings.ToLower(b.cfg.Symbol)
	return fmt.Sprintf("%s/stream?streams=%s@trade/%s@depth20@100ms",
		b.cfg.StreamBaseURL, symbol, symbol)
}

// Start connects to Binance WS. Idempotent — safe to call multiple times.
func (b *BinanceAdapter) Start(ctx context.Context) error {
	if b.started.Swap(true) {
		return nil
	}

	url := b.streamURL()
	log.Printf("[BinanceAdapter] connecting to %s", url)

	dialer := websocket.DefaultDialer
	conn, _, err := dialer.Dial(url, nil)
	if err != nil {
		b.started.Store(false)
		return fmt.Errorf("binance ws dial: %w", err)
	}

	b.connMu.Lock()
	b.conn = conn
	b.connMu.Unlock()

	log.Printf("[BinanceAdapter] connected — symbol=%s", b.cfg.Symbol)

	go b.readLoop(ctx)
	go b.volumeWindowLoop(ctx)
	return nil
}

// FetchKlineOpenPrice fetches the current 5-minute kline open price from Binance REST.
// Called at the start of each market cycle (with 1-2s delay after window start).
// This replaces the previous value so each new 5-minute window gets its own open price.
func (b *BinanceAdapter) FetchKlineOpenPrice() {
	url := fmt.Sprintf("%s/api/v3/klines?symbol=%s&interval=5m&limit=1",
		b.cfg.RestBaseURL, b.cfg.Symbol)

	resp, err := http.Get(url)
	if err != nil {
		log.Printf("[BinanceAdapter] fetch kline error: %v", err)
		return
	}
	defer resp.Body.Close()

	var klines [][]any
	if err := json.NewDecoder(resp.Body).Decode(&klines); err != nil {
		log.Printf("[BinanceAdapter] decode kline error: %v", err)
		return
	}

	if len(klines) == 0 || len(klines[0]) < 2 {
		log.Printf("[BinanceAdapter] empty kline response")
		return
	}

	openStr, ok := klines[0][1].(string)
	if !ok {
		return
	}

	openPrice := parseFloat(openStr)
	b.dataMu.Lock()
	b.data.OpenPrice = openPrice
	b.dataMu.Unlock()

	log.Printf("[BinanceAdapter] %s 5m kline open: %.2f", b.cfg.Symbol, openPrice)
}

func (b *BinanceAdapter) MarketData() <-chan BinanceMarketData { return b.dataCh }

func (b *BinanceAdapter) LatestData() BinanceMarketData {
	b.dataMu.RLock()
	defer b.dataMu.RUnlock()
	return b.data
}

func (b *BinanceAdapter) ConsumeVolume() (buy1s, sell1s, buy10s, sell10s float64) {
	b.volMu.Lock()
	defer b.volMu.Unlock()
	buy1s, sell1s = b.buyVol1s, b.sellVol1s
	buy10s, sell10s = b.buyVol10s, b.sellVol10s
	b.buyVol1s = 0
	b.sellVol1s = 0
	return
}

func (b *BinanceAdapter) readLoop(ctx context.Context) {
	defer func() {
		b.connMu.Lock()
		if b.conn != nil {
			b.conn.Close()
		}
		b.connMu.Unlock()
	}()

	tradeStream := strings.ToLower(b.cfg.Symbol) + "@trade"
	depthStream := strings.ToLower(b.cfg.Symbol) + "@depth20@100ms"

	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		b.connMu.Lock()
		conn := b.conn
		b.connMu.Unlock()
		if conn == nil {
			return
		}

		conn.SetReadDeadline(time.Now().Add(5 * time.Minute))
		_, msg, err := conn.ReadMessage()
		if err != nil {
			log.Printf("[BinanceAdapter] read error: %v", err)
			return
		}
		b.handleMessage(msg, tradeStream, depthStream)
	}
}

func (b *BinanceAdapter) handleMessage(msg []byte, tradeStream, depthStream string) {
	var envelope struct {
		Stream string          `json:"stream"`
		Data   json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(msg, &envelope); err != nil {
		return
	}
	switch envelope.Stream {
	case tradeStream:
		b.handleTrade(envelope.Data)
	case depthStream:
		b.handleDepth(envelope.Data)
	}
}

func (b *BinanceAdapter) handleTrade(data json.RawMessage) {
	var trade struct {
		Price     string `json:"p"`
		Quantity  string `json:"q"`
		TradeTime int64  `json:"T"`
		IsBuyerMM bool   `json:"m"`
	}
	if err := json.Unmarshal(data, &trade); err != nil {
		return
	}
	price := parseFloat(trade.Price)
	qty := parseFloat(trade.Quantity)

	b.dataMu.Lock()
	b.data.Price = price
	b.data.UpdatedAt = trade.TradeTime
	b.dataMu.Unlock()

	b.volMu.Lock()
	if trade.IsBuyerMM {
		b.sellVol1s += qty
		b.sellVol10s += qty
	} else {
		b.buyVol1s += qty
		b.buyVol10s += qty
	}
	b.volMu.Unlock()
}

func (b *BinanceAdapter) handleDepth(data json.RawMessage) {
	var depth struct {
		Bids [][2]string `json:"bids"`
		Asks [][2]string `json:"asks"`
	}
	if err := json.Unmarshal(data, &depth); err != nil {
		return
	}
	var bid5, ask5, bid10, ask10 float64
	for i, bid := range depth.Bids {
		if i < 5 {
			bid5 += parseFloat(bid[1])
		}
		if i < 10 {
			bid10 += parseFloat(bid[1])
		}
	}
	for i, ask := range depth.Asks {
		if i < 5 {
			ask5 += parseFloat(ask[1])
		}
		if i < 10 {
			ask10 += parseFloat(ask[1])
		}
	}
	b.dataMu.Lock()
	b.data.BidDepth5 = bid5
	b.data.AskDepth5 = ask5
	b.data.BidDepth10 = bid10
	b.data.AskDepth10 = ask10
	b.dataMu.Unlock()
}

func (b *BinanceAdapter) volumeWindowLoop(ctx context.Context) {
	ticker := time.NewTicker(1 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			b.volMu.Lock()
			b.volIdx = (b.volIdx + 1) % 10
			b.volSnapshot[b.volIdx].buy = 0
			b.volSnapshot[b.volIdx].sell = 0
			b.volMu.Unlock()
		}
	}
}

func parseFloat(s string) float64 {
	var f float64
	fmt.Sscanf(s, "%f", &f)
	return f
}
