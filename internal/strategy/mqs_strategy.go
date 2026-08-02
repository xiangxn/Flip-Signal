package strategy

import (
	"context"
	"fmt"
	"sync"

	"github.com/necklace/lasttrading/internal/decision"
	"github.com/necklace/lasttrading/internal/mqs"
	"github.com/necklace/lasttrading/internal/snapshot"

	"github.com/spf13/viper"
	"github.com/xiangxn/go-polymarket-sdk/orders"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
	"github.com/xiangxn/polypilot/core"
	"github.com/xiangxn/polypilot/runtime"
	"github.com/xiangxn/polypilot/state"
)

// MQSStrategy is a polypilot Strategy that uses MQS for trading decisions.
// It implements runtime.Strategy, runtime.TickStrategy, and runtime.ExecutionAwareStrategy.
type MQSStrategy struct {
	Bus           *core.EventBus
	engine        *mqs.Engine
	recorder      *decision.Recorder
	collector     *snapshot.Collector
	config        MQSStrategyConfig

	mu             sync.Mutex
	lastDecision   decision.Decision
	placedMarkets  map[string]bool // marketID → has open orders
}

// MQSStrategyConfig holds configuration for the MQS strategy.
type MQSStrategyConfig struct {
	BaseSize   float64 `mapstructure:"base_size"`   // Base position size
	StrongSize float64 `mapstructure:"strong_size"` // Position size for strong signal (defaults to 2x base)
	LogPath    string  `mapstructure:"log_path"`    // Decision log path
}

// DefaultMQSStrategyConfig returns sensible defaults.
func DefaultMQSStrategyConfig() MQSStrategyConfig {
	return MQSStrategyConfig{
		BaseSize:   5.0,
		StrongSize: 10.0,
		LogPath:    "data/mqs_decisions.jsonl",
	}
}

// Init initializes the strategy with the event bus and configuration.
func (s *MQSStrategy) Init(bus *core.EventBus, ctx context.Context, cfg *viper.Viper) {
	s.Bus = bus
	s.engine = mqs.NewEngine()
	s.placedMarkets = make(map[string]bool)

	sc := DefaultMQSStrategyConfig()
	if cfg != nil {
		if sub := cfg.Sub("strategies.mqs"); sub != nil {
			if err := sub.Unmarshal(&sc); err != nil {
				// Use defaults
			}
		}
	}
	s.config = sc

	var err error
	s.recorder, err = decision.NewRecorder(sc.LogPath)
	if err != nil {
		// Non-fatal: strategy works without logging
		s.recorder = nil
	}
}

// OnUpdate is the main strategy entry point called on market/orderbook events.
func (s *MQSStrategy) OnUpdate(e core.Event, o runtime.Observation, snap state.Snapshot) []runtime.OrderIntent {
	switch e.Type {
	case core.EventOrderBook:
		return s.handleOrderBookUpdate(o, snap)
	case core.EventMarket:
		return s.handleMarketUpdate(o, snap)
	case core.EventMarketResolved:
		return s.handleMarketResolved(o)
	}
	return nil
}

// OnTick handles periodic tick events for time-based decisions.
func (s *MQSStrategy) OnTick(now interface{}, o runtime.Observation, snap state.Snapshot) []runtime.OrderIntent {
	// Tick-based evaluation is handled by the snapshot feed
	return nil
}

// OnExecution handles execution events (fill confirmations, rejections).
func (s *MQSStrategy) OnExecution(ev core.ExecutionEvent, o runtime.Observation, snap state.Snapshot) []runtime.OrderIntent {
	if ev.Status == core.ExecutionStatusFilled || ev.Status == core.ExecutionStatusCancelled {
		s.mu.Lock()
		delete(s.placedMarkets, ev.MarketID)
		s.mu.Unlock()
	}
	return nil
}

// OnResolved handles market resolution events.
func (s *MQSStrategy) OnResolved(info *sdk.ResolvedInfo) {
	// Update the last decision record with the result
	// (result recording is done by the recorder when the market resolves)
}

func (s *MQSStrategy) handleOrderBookUpdate(o runtime.Observation, snap state.Snapshot) []runtime.OrderIntent {
	// Extract MQS from observation features if available
	mq, ok := extractMQS(o)
	if !ok {
		// MQS not available yet — the snapshot feed hasn't published one
		return nil
	}

	return s.evaluateDecision(mq, o, snap)
}

func (s *MQSStrategy) handleMarketUpdate(o runtime.Observation, snap state.Snapshot) []runtime.OrderIntent {
	// Market metadata update — cache market info but don't trade yet
	return nil
}

func (s *MQSStrategy) handleMarketResolved(o runtime.Observation) []runtime.OrderIntent {
	// Clean up tracking
	s.mu.Lock()
	delete(s.placedMarkets, o.MarketID)
	s.mu.Unlock()
	return nil
}

// evaluateDecision applies MQS rules and produces order intents.
func (s *MQSStrategy) evaluateDecision(mq mqs.MarketQuality, o runtime.Observation, snap state.Snapshot) []runtime.OrderIntent {
	remainingSec := o.TimeLeftSec

	result := decision.Decide(mq, int(remainingSec))

	// Log the decision
	btcReturn := 0.0
	if r, ok := o.Features["btcReturn"].(float64); ok {
		btcReturn = r
	}
	if s.recorder != nil {
		_ = s.recorder.Record(mq, int(remainingSec), btcReturn, result.Decision, "")
	}

	s.mu.Lock()
	s.lastDecision = result.Decision
	s.mu.Unlock()

	switch result.Decision {
	case decision.DecisionForbidden:
		return s.cancelAllOrders(snap)

	case decision.DecisionNoTrade:
		return nil

	case decision.DecisionStandard, decision.DecisionStrong:
		return s.placeDirectionalOrders(mq, o, snap, result.Decision)
	}

	return nil
}

// placeDirectionalOrders determines direction and creates BUY orders.
func (s *MQSStrategy) placeDirectionalOrders(mq mqs.MarketQuality, o runtime.Observation, snap state.Snapshot, dec decision.Decision) []runtime.OrderIntent {
	s.mu.Lock()
	if s.placedMarkets[o.MarketID] {
		s.mu.Unlock()
		return nil // Already have orders for this market
	}
	s.mu.Unlock()

	// Determine direction: compare current BTC price vs open
	openPrice, _ := o.Features["openPrice"].(float64)
	latestPrice, _ := o.Features["latestPrice"].(float64)

	if openPrice <= 0 {
		return nil
	}

	isUp := latestPrice >= openPrice

	// Select token: in polypilot, tokens[0] is typically UP/YES, tokens[1] is DOWN/NO
	var targetTokenID string
	if len(o.TokenIds) >= 2 {
		if isUp {
			targetTokenID = o.TokenIds[0] // YES/UP
		} else {
			targetTokenID = o.TokenIds[1] // NO/DOWN
		}
	} else if len(o.TokenIds) == 1 {
		targetTokenID = o.TokenIds[0]
	} else {
		return nil
	}

	// Determine size
	size := s.config.BaseSize
	decisionStr := "BUY_YES"
	if !isUp {
		decisionStr = "BUY_NO"
	}
	if dec == decision.DecisionStrong {
		size = s.config.StrongSize
		decisionStr = "STRONG_" + decisionStr
	}

	// Get the token's ask price for a marketable limit order
	token, exists := o.Tokens[targetTokenID]
	if !exists {
		return nil
	}

	price := token.AskPrice
	if price <= 0 {
		price = 0.99 // fallback
	}

	s.mu.Lock()
	s.placedMarkets[o.MarketID] = true
	s.mu.Unlock()

	return []runtime.OrderIntent{
		{
			Action:    runtime.OrderIntentActionPlace,
			MarketID:  o.MarketID,
			TokenID:   targetTokenID,
			Price:     price,
			Side:      orders.BUY,
			Size:      size,
			OrderType: orders.GTC,
			IntentID:  fmt.Sprintf("mqs_%s_%d", o.MarketID, o.TimeLeftSec),
		},
	}
}

// cancelAllOrders cancels all open orders for the current market.
func (s *MQSStrategy) cancelAllOrders(snap state.Snapshot) []runtime.OrderIntent {
	var intents []runtime.OrderIntent
	for orderID := range snap.Orders {
		intents = append(intents, runtime.OrderIntent{
			Action:  runtime.OrderIntentActionCancel,
			OrderID: orderID,
		})
	}
	if len(intents) > 0 {
		s.mu.Lock()
		// Clear placed markets since we're canceling everything
		s.placedMarkets = make(map[string]bool)
		s.mu.Unlock()
	}
	return intents
}

// extractMQS extracts MarketQuality from observation features.
func extractMQS(o runtime.Observation) (mqs.MarketQuality, bool) {
	mqRaw, ok := o.Features["mqs"]
	if !ok {
		return mqs.MarketQuality{}, false
	}
	mq, ok := mqRaw.(mqs.MarketQuality)
	return mq, ok
}
