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

	// Volume accumulated since last ConsumeVolume call.
	BuyVolume  float64 `json:"buy_vol"`
	SellVolume float64 `json:"sell_vol"`

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
//
// On disconnect, the adapter automatically reconnects with exponential
// backoff (1s → 30s max). The connection is re-established transparently;
// callers continue to read LatestData() which returns the last-known values
// during the gap.
type BinanceAdapter struct {
	cfg BinanceConfig

	conn   *websocket.Conn
	connMu sync.Mutex

	data   BinanceMarketData
	dataMu sync.RWMutex

	volMu       sync.Mutex
	buyVol5s    float64
	sellVol5s   float64

	started atomic.Bool

	// Reconnection tracking
	reconnecting atomic.Bool
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
		cfg: cfg,
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
// Launches a persistent read loop that automatically reconnects on disconnect
// with exponential backoff (1s → 30s max). Returns when ctx is cancelled.
func (b *BinanceAdapter) Start(ctx context.Context) error {
	if b.started.Swap(true) {
		return nil
	}

	if err := b.dialAndSet(); err != nil {
		b.started.Store(false)
		return err
	}

	log.Printf("[BinanceAdapter] connected — symbol=%s", b.cfg.Symbol)

	go b.runReadLoop(ctx)
	return nil
}

// dialAndSet dials a new WS connection and stores it in b.conn.
func (b *BinanceAdapter) dialAndSet() error {
	url := b.streamURL()
	conn, _, err := websocket.DefaultDialer.Dial(url, nil)
	if err != nil {
		return fmt.Errorf("binance ws dial: %w", err)
	}
	b.connMu.Lock()
	b.conn = conn
	b.connMu.Unlock()
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

func (b *BinanceAdapter) LatestData() BinanceMarketData {
	b.dataMu.RLock()
	d := b.data
	b.dataMu.RUnlock()

	b.volMu.Lock()
	d.BuyVolume = b.buyVol5s
	d.SellVolume = b.sellVol5s
	b.volMu.Unlock()

	return d
}

func (b *BinanceAdapter) ConsumeVolume() (buyAcc, sellAcc float64) {
	b.volMu.Lock()
	defer b.volMu.Unlock()
	buyAcc, sellAcc = b.buyVol5s, b.sellVol5s
	b.buyVol5s = 0
	b.sellVol5s = 0
	return
}

// runReadLoop is the persistent read loop that handles reconnection.
// It reads from the current connection until error, then reconnects with
// exponential backoff. Only returns when ctx is cancelled.
func (b *BinanceAdapter) runReadLoop(ctx context.Context) {
	defer b.closeConn()

	tradeStream := strings.ToLower(b.cfg.Symbol) + "@trade"
	depthStream := strings.ToLower(b.cfg.Symbol) + "@depth20@100ms"

	backoff := time.Second
	const maxBackoff = 30 * time.Second

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

		// Read from current connection until error or ctx cancel
		err := b.readFromConn(ctx, conn, tradeStream, depthStream)
		if err == nil {
			return // ctx cancelled (clean exit)
		}

		// Connection lost — close and reconnect
		log.Printf("[BinanceAdapter] ⚠️ read error: %v — reconnecting in %v", err, backoff.Round(time.Millisecond))
		b.closeConn()
		b.reconnecting.Store(true)

		select {
		case <-ctx.Done():
			return
		case <-time.After(backoff):
		}

		// Exponential backoff
		backoff *= 2
		if backoff > maxBackoff {
			backoff = maxBackoff
		}

		if err := b.dialAndSet(); err != nil {
			log.Printf("[BinanceAdapter] reconnect dial failed: %v", err)
			continue // retry with same backoff
		}

		// Reset backoff on successful connection
		backoff = time.Second
		b.reconnecting.Store(false)
		log.Printf("[BinanceAdapter] 🔄 reconnected — symbol=%s", b.cfg.Symbol)
	}
}

// readFromConn reads messages from a single connection. Returns nil on ctx
// cancellation, or the error that caused the read to fail.
func (b *BinanceAdapter) readFromConn(ctx context.Context, conn *websocket.Conn, tradeStream, depthStream string) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}

		conn.SetReadDeadline(time.Now().Add(5 * time.Minute))
		_, msg, err := conn.ReadMessage()
		if err != nil {
			return err
		}
		b.handleMessage(msg, tradeStream, depthStream)
	}
}

// closeConn closes the current WS connection (if any).
func (b *BinanceAdapter) closeConn() {
	b.connMu.Lock()
	defer b.connMu.Unlock()
	if b.conn != nil {
		b.conn.Close()
		b.conn = nil
	}
}

// IsReconnecting returns true when the adapter is between connections.
func (b *BinanceAdapter) IsReconnecting() bool { return b.reconnecting.Load() }

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
		BestMatch bool   `json:"M"` // absorbs M (best price match) to prevent Go's case-insensitive fallback from overwriting IsBuyerMM
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
		b.sellVol5s += qty
	} else {
		b.buyVol5s += qty
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

func parseFloat(s string) float64 {
	var f float64
	fmt.Sscanf(s, "%f", &f)
	return f
}
