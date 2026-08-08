package dashboard

import (
	"encoding/json"
	"net/http"
	"strconv"
	"time"

	"github.com/necklace/flip-signal/internal/flip"
	"github.com/necklace/flip-signal/internal/lab"
)

// ── Response types ──

type stateResponse struct {
	TS            string       `json:"ts"`
	Mode          string       `json:"mode"`
	Generation    int64        `json:"generation"`
	EngineState   string       `json:"engine_state"`
	EngineLabel   string       `json:"engine_state_label"`
	CurrentSide   string       `json:"current_side"`
	SnapCount     int          `json:"snap_count"`
	RemainingSec  int          `json:"remaining_sec"`
	ConditionID   string       `json:"condition_id"`
	OpenPrice     float64      `json:"open_price"`
	CurrentPrice  float64      `json:"current_price"`
	YesPrice      float64      `json:"yes_price"`
	NoPrice       float64      `json:"no_price"`
	BuyVol5s      float64      `json:"buy_vol_5s"`
	SellVol5s     float64      `json:"sell_vol_5s"`
	BidDepth      float64      `json:"bid_depth"`
	AskDepth      float64      `json:"ask_depth"`
	HistReady     bool         `json:"hist_ready"`
	HistAvgRange  float64      `json:"hist_avg_range"`
	HistWindowN   int          `json:"hist_window_n"`
	SignalCount   int          `json:"signal_count"`
	WonCount      int          `json:"won_count"`
	LostCount     int          `json:"lost_count"`
	PendingCount  int          `json:"pending_count"`
	WinRate       float64      `json:"win_rate"`
	CumulativePnl float64      `json:"cumulative_pnl"`

	// 多穿越重试
	AllowRetryCrossings bool         `json:"allow_retry_crossings"`
	RetryCount          int          `json:"retry_count"`
	CrossFeatures       *crossDetail `json:"cross_features"`       // 当前 Confirming 中
	LastFailedFeatures  *crossDetail `json:"last_failed_features"` // 最近一次失败穿越（多穿越模式诊断用）
}

type crossDetail struct {
	Side               string  `json:"side"`
	PathEff            float64 `json:"path_eff"`
	NoiseRatio         float64 `json:"noise_ratio"`
	Flips              int     `json:"flips"`
	IsOscillating      bool    `json:"is_oscillating"`
	RangeExpansion     float64 `json:"range_expansion"`
	BTCPosition        float64 `json:"btc_position"`
	BTCExtreme         bool    `json:"btc_extreme"`
	ConfirmTicksWaited int     `json:"confirm_ticks_waited"`
}

type signalsResponse struct {
	Signals       []*flip.FlipSignal `json:"signals"`
	Total         int                `json:"total"`
	Won           int                `json:"won"`
	Lost          int                `json:"lost"`
	Pending       int                `json:"pending"`
	WinRate       float64            `json:"win_rate"`
	CumulativePnl float64            `json:"cumulative_pnl"`
}

type snapshotsResponse struct {
	ConditionID string                  `json:"condition_id"`
	Snapshots    []*lab.ResearchSnapshot `json:"snapshots"`
	Count       int                     `json:"count"`
}

type histRangeResponse struct {
	Ready    bool      `json:"ready"`
	WindowN  int       `json:"window_n"`
	AvgRange float64   `json:"avg_range"`
	Ranges   []float64 `json:"ranges"`
	Count    int       `json:"count"`
}

type configResponse struct {
	AllowRetryCrossings   bool    `json:"allow_retry_crossings"`
	TriggerThreshold      float64 `json:"trigger_threshold"`
	MinPreSnaps           int     `json:"min_pre_snaps"`
	MaxRemainingSec       int     `json:"max_remaining_sec"`
	ConfirmDelayTicks     int     `json:"confirm_delay_ticks"`
	ScoreEntry            int     `json:"score_entry"`
	ScoreAdd              int     `json:"score_add"`
	PathEffOscillating    float64 `json:"path_eff_oscillating"`
	NoiseRatioOscillating float64 `json:"noise_ratio_oscillating"`
	FlipsOscillating      int     `json:"flips_oscillating"`
	RangeExpThreshold     float64 `json:"range_exp_threshold"`
	RangeExpMax           float64 `json:"range_exp_max"`
	OtherDeltaVStrong     float64 `json:"other_delta_vstrong"`
	OtherDeltaStrong      float64 `json:"other_delta_strong"`
	OtherDeltaWeak        float64 `json:"other_delta_weak"`
	BTCPosMax             float64 `json:"btc_pos_max"`
	BTCPosMin             float64 `json:"btc_pos_min"`
	EntryCheapStrong      float64 `json:"entry_cheap_strong"`
	EntryCheapWeak        float64 `json:"entry_cheap_weak"`
}

// ── Handlers ──

func (s *State) handleState(w http.ResponseWriter, r *http.Request) {
	btc := s.Binance.LatestData()
	snaps := s.Collector.Snapshots()

	remainingSec := 0
	yesPrice := 0.0
	noPrice := 0.0
	if len(snaps) > 0 {
		last := snaps[len(snaps)-1]
		remainingSec = last.RemainingSec
		yesPrice = last.YesPrice
		noPrice = last.NoPrice
	}

	total, won, lost, pending, winRate, cumPnl := s.Recorder.SignalStats()

	engLabel := s.Engine.StateLabel()

	resp := stateResponse{
		TS:            time.Now().UTC().Format(time.RFC3339),
		Mode:          s.Mode,
		Generation:    s.Engine.Generation(),
		EngineState:   engLabel,
		EngineLabel:   engLabel,
		CurrentSide:   s.Engine.CurrentSide(),
		SnapCount:     s.Engine.SnapCount(),
		RemainingSec:  remainingSec,
		ConditionID:   s.Collector.ConditionID(),
		OpenPrice:     s.Collector.OpenPrice(),
		CurrentPrice:  btc.Price,
		YesPrice:      yesPrice,
		NoPrice:       noPrice,
		BuyVol5s:      btc.BuyVolume,
		SellVol5s:     btc.SellVolume,
		BidDepth:      btc.BidDepth5,
		AskDepth:      btc.AskDepth5,
		HistReady:     s.HistRange.IsReady(),
		HistAvgRange:  s.HistRange.AvgRange(),
		HistWindowN:   s.Engine.Config().HistWindowN,
		SignalCount:   total,
		WonCount:      won,
		LostCount:     lost,
		PendingCount:  pending,
		WinRate:       winRate,
		CumulativePnl: cumPnl,

		AllowRetryCrossings: s.Engine.Config().AllowRetryCrossings,
		RetryCount:          s.Engine.RetryCount(),
	}

	// 当前 Confirming 中的穿越特征
	if engLabel == "Confirming" {
		t0 := s.Engine.T0Features()
		if t0 != nil {
			resp.CrossFeatures = &crossDetail{
				Side:               t0.Side,
				PathEff:            t0.PathEff,
				NoiseRatio:         t0.NoiseRatio,
				Flips:              t0.Flips,
				IsOscillating:      t0.Oscillating,
				RangeExpansion:     t0.RangeExpansion,
				BTCPosition:        t0.BTCPosition,
				BTCExtreme:         t0.BTCExtreme,
				ConfirmTicksWaited: s.Engine.ConfirmTicksWaited(),
			}
		}
	}

	// 最近一次失败穿越的特征（多穿越模式下诊断用）
	if t0 := s.Engine.LastFailedT0(); t0 != nil {
		resp.LastFailedFeatures = &crossDetail{
			Side:           t0.Side,
			PathEff:        t0.PathEff,
			NoiseRatio:     t0.NoiseRatio,
			Flips:          t0.Flips,
			IsOscillating:  t0.Oscillating,
			RangeExpansion: t0.RangeExpansion,
			BTCPosition:    t0.BTCPosition,
			BTCExtreme:     t0.BTCExtreme,
		}
	}

	writeJSON(w, http.StatusOK, resp)
}

func (s *State) handleSignals(w http.ResponseWriter, r *http.Request) {
	limit := 50
	if l := r.URL.Query().Get("limit"); l != "" {
		if n, err := strconv.Atoi(l); err == nil && n > 0 && n <= 500 {
			limit = n
		}
	}

	resolved := s.Recorder.ResolvedSignals()
	pending := s.Recorder.PendingSignals()

	// Merge: resolved first (newest first), then pending
	all := make([]*flip.FlipSignal, 0, len(resolved)+len(pending))
	for i := len(resolved) - 1; i >= 0; i-- {
		all = append(all, resolved[i])
	}
	for _, sig := range pending {
		all = append(all, sig)
	}

	if len(all) > limit {
		all = all[:limit]
	}

	total, won, lost, pendingCount, winRate, cumPnl := s.Recorder.SignalStats()

	writeJSON(w, http.StatusOK, signalsResponse{
		Signals:       all,
		Total:         total,
		Won:           won,
		Lost:          lost,
		Pending:       pendingCount,
		WinRate:       winRate,
		CumulativePnl: cumPnl,
	})
}

func (s *State) handleSnapshots(w http.ResponseWriter, r *http.Request) {
	snaps := s.Collector.Snapshots()
	writeJSON(w, http.StatusOK, snapshotsResponse{
		ConditionID: s.Collector.ConditionID(),
		Snapshots:   snaps,
		Count:       len(snaps),
	})
}

func (s *State) handleHistRange(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, histRangeResponse{
		Ready:    s.HistRange.IsReady(),
		WindowN:  s.Engine.Config().HistWindowN,
		AvgRange: s.HistRange.AvgRange(),
	})
}

func (s *State) handleConfig(w http.ResponseWriter, r *http.Request) {
	cfg := s.Engine.Config()
	writeJSON(w, http.StatusOK, configResponse{
		AllowRetryCrossings:   cfg.AllowRetryCrossings,
		TriggerThreshold:      cfg.TriggerThreshold,
		MinPreSnaps:           cfg.MinPreSnaps,
		MaxRemainingSec:       cfg.MaxRemainingSec,
		ConfirmDelayTicks:     cfg.ConfirmDelayTicks,
		ScoreEntry:            cfg.ScoreEntry,
		ScoreAdd:              cfg.ScoreAdd,
		PathEffOscillating:    cfg.PathEffOscillating,
		NoiseRatioOscillating: cfg.NoiseRatioOscillating,
		FlipsOscillating:      cfg.FlipsOscillating,
		RangeExpThreshold:     cfg.RangeExpThreshold,
		RangeExpMax:           cfg.RangeExpMax,
		OtherDeltaVStrong:     cfg.OtherDeltaVStrong,
		OtherDeltaStrong:      cfg.OtherDeltaStrong,
		OtherDeltaWeak:        cfg.OtherDeltaWeak,
		BTCPosMax:             cfg.BTCPosMax,
		BTCPosMin:             cfg.BTCPosMin,
		EntryCheapStrong:      cfg.EntryCheapStrong,
		EntryCheapWeak:        cfg.EntryCheapWeak,
	})
}

// ── Helpers ──

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}
