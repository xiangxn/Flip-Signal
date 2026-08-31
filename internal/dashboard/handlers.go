package dashboard

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"sort"
	"time"

	"github.com/necklace/flip-signal/internal/flip"
)

// ── Response 类型 ──

// stateResponse 是 /api/state 的响应体。
type stateResponse struct {
	TS string `json:"ts"` // 服务器当前时间（RFC3339）

	Mode        string `json:"mode"`
	UptimeSec   int64  `json:"uptime_sec"`
	ConditionID string `json:"condition_id"`
	Slug        string `json:"slug"`
	EventStart  int64  `json:"event_start"`
	EngineState string `json:"engine_state"`

	// 当前窗口盘口（实时诊断）
	UpBid     float64 `json:"up_bid"`
	UpAsk     float64 `json:"up_ask"`
	DownBid   float64 `json:"down_bid"`
	DownAsk   float64 `json:"down_ask"`
	Remaining int     `json:"remaining_sec"` // 窗口剩余秒（0 = 窗口间）
	BookLatMs int64   `json:"book_latency_ms"`
	TwapAgeMs int64   `json:"twap_age_ms"`

	// 统计汇总（已结算 + 待结算信号）
	SignalCount  int     `json:"signal_count"`
	WonCount     int     `json:"won_count"`
	LostCount    int     `json:"lost_count"`
	PendingCount int     `json:"pending_count"`
	WinRate      float64 `json:"win_rate"`
	CumPnl       float64 `json:"cumulative_pnl"`
	CrossCount   int     `json:"cross_count"` // 全部穿越观测（含失败）
}

// crossResponse 是 /api/crosses 与 /api/signals 的元素。
type crossResponse struct {
	Ts           int64   `json:"ts"`
	Date         string  `json:"date"`
	ConditionID  string  `json:"condition_id"`
	Slug         string  `json:"slug"`
	Side         string  `json:"side"`
	Rem          int     `json:"rem"`
	TriggerBid   float64 `json:"trigger_bid"`
	PostEnd      float64 `json:"post_end"`
	Fill         float64 `json:"fill"`
	FillComp     float64 `json:"fill_comp"`
	Shares       float64 `json:"shares"`
	OK           bool    `json:"ok"`
	RejectReason string  `json:"reject_reason,omitempty"`
	Cls          string  `json:"cls"`
	Won          *bool   `json:"won,omitempty"`
	PnL          float64 `json:"pnl,omitempty"`
	ResolvedAt   string  `json:"resolved_at,omitempty"`
}

// ── Handlers ──

// handleState 返回运行状态与统计汇总。
func (s *State) handleState(w http.ResponseWriter, r *http.Request) {
	live := s.snapshot.Snapshot()
	total, won, lost, pending, winRate, cumPnl := s.recorder.Stats()

	writeJSON(w, stateResponse{
		TS:           s.nowFn().UTC().Format(time.RFC3339),
		Mode:         s.mode,
		UptimeSec:    int64(s.nowFn().Sub(s.startedAt).Seconds()),
		ConditionID:  live.ConditionID,
		Slug:         live.Slug,
		EventStart:   live.EventStart,
		EngineState:  live.EngineState,
		UpBid:        live.UpBid,
		UpAsk:        live.UpAsk,
		DownBid:      live.DownBid,
		DownAsk:      live.DownAsk,
		Remaining:    s.remaining(live),
		BookLatMs:    live.BookLatMs,
		TwapAgeMs:    live.TwapAgeMs,
		SignalCount:  total,
		WonCount:     won,
		LostCount:    lost,
		PendingCount: pending,
		WinRate:      winRate,
		CumPnl:       cumPnl,
		CrossCount:   len(s.recorder.Crosses(100000)),
	})
}

// handleCrosses 返回穿越观测列表（成功+失败，时间倒序，?limit= 分页）。
func (s *State) handleCrosses(w http.ResponseWriter, r *http.Request) {
	limit := queryLimit(r, 50)
	records := s.recorder.Crosses(limit)
	writeJSON(w, mapRecords(records))
}

// handleSignals 返回信号列表（ok=true，含 P&L，时间倒序）。
func (s *State) handleSignals(w http.ResponseWriter, r *http.Request) {
	records := s.recorder.Signals()
	sort.Slice(records, func(i, j int) bool { return records[i].Ts > records[j].Ts })
	writeJSON(w, mapRecords(records))
}

// handleConfig 返回当前策略配置（前端展示标定参数）。
func (s *State) handleConfig(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, map[string]interface{}{
		"trigger_threshold": s.cfg.TriggerThreshold,
		"trigger_bid_min":   s.cfg.TriggerBidMin,
		"post_end_max":      s.cfg.PostEndMax,
		"confirm_sec":       s.cfg.ConfirmSec,
		"max_remaining":     s.cfg.MaxRemaining,
		"min_remaining":     s.cfg.MinRemaining,
		"stake":             s.cfg.Stake,
	})
}

// ── 内部 ──

// remaining 计算当前窗口剩余秒（未到窗口起点或已结束为 0）。
func (s *State) remaining(live LiveSnapshot) int {
	if live.EventStart == 0 {
		return 0
	}
	rem := live.EventStart + 300 - s.nowFn().Unix()
	if rem < 0 {
		return 0
	}
	return int(rem)
}

// mapRecords 将 flip.Record 映射为 API 响应元素。
func mapRecords(records []*flip.Record) []crossResponse {
	out := make([]crossResponse, 0, len(records))
	for _, rec := range records {
		out = append(out, crossResponse{
			Ts:           rec.Ts,
			Date:         rec.Date,
			ConditionID:  rec.ConditionID,
			Slug:         rec.Slug,
			Side:         rec.Side,
			Rem:          rec.Rem,
			TriggerBid:   rec.TriggerBid,
			PostEnd:      rec.PostEnd,
			Fill:         rec.Fill,
			FillComp:     rec.FillComp,
			Shares:       rec.Shares,
			OK:           rec.OK,
			RejectReason: rec.RejectReason,
			Cls:          rec.Cls,
			Won:          rec.Won,
			PnL:          rec.PnL,
			ResolvedAt:   rec.ResolvedAt,
		})
	}
	return out
}

// queryLimit 解析 ?limit= 参数（1..1000，默认 def）。
func queryLimit(r *http.Request, def int) int {
	v := r.URL.Query().Get("limit")
	if v == "" {
		return def
	}
	var n int
	if _, err := fmt.Sscanf(v, "%d", &n); err != nil {
		return def
	}
	if n < 1 {
		return 1
	}
	if n > 1000 {
		return 1000
	}
	return n
}

// writeJSON 统一 JSON 响应。
func writeJSON(w http.ResponseWriter, v interface{}) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("[Dashboard] 编码响应失败: %v", err)
	}
}
