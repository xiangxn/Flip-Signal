// Package dashboard provides an HTTP dashboard for the Flip Signal Detection engine.
//
// It exposes read-only JSON APIs and an embedded HTML page for real-time
// monitoring of the flip engine, market data, and signal history.
package dashboard

import (
	"github.com/necklace/flip-signal/internal/feed"
	"github.com/necklace/flip-signal/internal/flip"
	"github.com/necklace/flip-signal/internal/lab"
)

// State bundles references to all runtime components the dashboard needs to read.
// All reads are thread-safe via the components' own mutexes or because writes
// happen in a single goroutine.
type State struct {
	Collector *lab.Collector
	Engine    *flip.Engine
	HistRange *flip.HistRangeTracker
	Recorder  *flip.FlipRecorder
	Binance   *feed.BinanceAdapter

	// Static config
	Symbol string
	Mode   string // "paper" or "live"
}

// New creates a new State holding references to the running components.
func New(
	collector *lab.Collector,
	engine *flip.Engine,
	histRange *flip.HistRangeTracker,
	recorder *flip.FlipRecorder,
	binance *feed.BinanceAdapter,
	symbol string,
	mode string,
) *State {
	return &State{
		Collector: collector,
		Engine:    engine,
		HistRange: histRange,
		Recorder:  recorder,
		Binance:   binance,
		Symbol:    symbol,
		Mode:      mode,
	}
}
