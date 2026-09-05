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

	// Binance spot（−1 = 尚无推送）
	SpotPrice float64 `json:"spot_price"`
	SpotAgeMs int64   `json:"spot_age_ms"`

	// 统计汇总（已结算 + 待结算信号）
	ObservationCount int     `json:"observation_count"` // 全部触底观测（含失败）
	SignalCount      int     `json:"signal_count"`
	WonCount         int     `json:"won_count"`
	LostCount        int     `json:"lost_count"`
	PendingCount     int     `json:"pending_count"`
	WinRate          float64 `json:"win_rate"`
	CumPnl           float64 `json:"cumulative_pnl"`
	DayPnlPos        int     `json:"day_pnl_pos"`  // 逐日盈利天数（已结算）
	DayTotal         int     `json:"day_total"`    // 有结算信号的天数
	MaxDrawdown      float64 `json:"max_drawdown"` // 累计 P&L 最大回撤（USDC）
}

// recordResponse 是 /api/observations 与 /api/signals 的元素。
// 字段 = 观测记录（ts/date/condition_id/slug 对齐回测 CSV 键，09-15 复验映射用）。
type recordResponse struct {
	Ts           int64   `json:"ts"`
	Date         string  `json:"date"`
	ConditionID  string  `json:"condition_id"`
	Slug         string  `json:"slug"`
	Side         string  `json:"side"` // 狗侧: yes/no
	Rem          int     `json:"rem"`
	Fill         float64 `json:"fill"`
	M20          float64 `json:"m_20"`
	M30          float64 `json:"m_30"`
	M45          float64 `json:"m_45"`
	DistS        float64 `json:"dist_s,omitempty"`
	DistT        float64 `json:"dist_t,omitempty"`
	OK           bool    `json:"ok"`
	RejectReason string  `json:"reject_reason,omitempty"`
	Shares       float64 `json:"shares,omitempty"`
	BookLatMs    int64   `json:"book_latency_ms,omitempty"`
	Won          *bool   `json:"won,omitempty"`
	PnL          float64 `json:"pnl,omitempty"`
	ResolvedAt   string  `json:"resolved_at,omitempty"`
}

// dailyRow 是 /api/daily 的一行（按 UTC 日切分，与回测 CSV date/记录文件同日口径；
// 逐日明细弹窗用，列与 02_paper_compare.py 逐日输出对齐）。
type dailyRow struct {
	Date     string  `json:"date"` // YYYY-MM-DD（UTC）；total 行为空
	Obs      int     `json:"obs"`  // 触底观测（含失败）
	Signals  int     `json:"signals"`
	Pending  int     `json:"pending"` // 未结算信号
	Won      int     `json:"won"`
	Lost     int     `json:"lost"`
	WinRate  float64 `json:"win_rate"` // 已结算口径（won/(won+lost)；无结算 = 0）
	PnL      float64 `json:"pnl"`
}

// dailyResp 是 /api/daily 的响应体（rows 时间正序 + 合计行）。
type dailyResp struct {
	Days  []dailyRow `json:"days"`
	Total dailyRow   `json:"total"`
}

// ── Handlers ──

// handleState 返回运行状态与统计汇总。
func (s *State) handleState(w http.ResponseWriter, r *http.Request) {
	live := s.snapshot.Snapshot()
	total, won, lost, pending, winRate, cumPnl := s.signalStats()
	daily, dayPos := s.dailySummary()

	writeJSON(w, stateResponse{
		TS:               s.nowFn().UTC().Format(time.RFC3339),
		Mode:             s.mode,
		UptimeSec:        int64(s.nowFn().Sub(s.startedAt).Seconds()),
		ConditionID:      live.ConditionID,
		Slug:             live.Slug,
		EventStart:       live.EventStart,
		EngineState:      live.EngineState,
		UpBid:            live.UpBid,
		UpAsk:            live.UpAsk,
		DownBid:          live.DownBid,
		DownAsk:          live.DownAsk,
		Remaining:        s.remaining(live),
		BookLatMs:        live.BookLatMs,
		TwapAgeMs:        live.TwapAgeMs,
		SpotPrice:        live.SpotPrice,
		SpotAgeMs:        live.SpotAgeMs,
		ObservationCount: len(s.recorder.Observations()),
		SignalCount:      total,
		WonCount:         won,
		LostCount:        lost,
		PendingCount:     pending,
		WinRate:          winRate,
		CumPnl:           cumPnl,
		DayPnlPos:        dayPos,
		DayTotal:         len(daily),
		MaxDrawdown:      s.recorder.MaxDrawdown(),
	})
}

// handleObservations 返回触底观测列表（成功+失败，时间倒序，?limit= 分页）。
func (s *State) handleObservations(w http.ResponseWriter, r *http.Request) {
	limit := queryLimit(r, 50)
	records := s.recorder.Observations()
	sort.Slice(records, func(i, j int) bool { return records[i].Ts > records[j].Ts })
	if len(records) > limit {
		records = records[:limit]
	}
	writeJSON(w, mapRecords(records))
}

// handleSignals 返回信号列表（ok=true，含 P&L，时间倒序，?limit= 分页）。
func (s *State) handleSignals(w http.ResponseWriter, r *http.Request) {
	limit := queryLimit(r, 200)
	records := s.recorder.Signals()
	sort.Slice(records, func(i, j int) bool { return records[i].Ts > records[j].Ts })
	if len(records) > limit {
		records = records[:limit]
	}
	writeJSON(w, mapRecords(records))
}

// handleDaily 返回逐日盈利明细（UTC 日粒度，供前端弹窗表格）。
func (s *State) handleDaily(w http.ResponseWriter, r *http.Request) {
	days := collectDaily(s.recorder.Observations())
	var total dailyRow
	for i := range days {
		d := &days[i]
		total.Obs += d.Obs
		total.Signals += d.Signals
		total.Pending += d.Pending
		total.Won += d.Won
		total.Lost += d.Lost
		total.PnL += d.PnL
	}
	if total.Won+total.Lost > 0 {
		total.WinRate = float64(total.Won) / float64(total.Won+total.Lost)
	}
	writeJSON(w, dailyResp{Days: days, Total: total})
}

// collectDaily 按记录 date 字段（UTC 日）聚合逐日统计（时间正序输入）。
// 胜率与 P&L 只统计已结算信号（与 /api/state 统计口径一致）。
func collectDaily(recs []*flip.Record) []dailyRow {
	byDay := map[string]*dailyRow{}
	for _, rec := range recs {
		d := byDay[rec.Date]
		if d == nil {
			d = &dailyRow{Date: rec.Date}
			byDay[rec.Date] = d
		}
		d.Obs++
		if !rec.OK {
			continue
		}
		d.Signals++
		switch {
		case rec.Won == nil:
			d.Pending++
		case *rec.Won:
			d.Won++
			d.PnL += rec.PnL
		default:
			d.Lost++
			d.PnL += rec.PnL
		}
	}
	out := make([]dailyRow, 0, len(byDay))
	for _, d := range byDay {
		if d.Won+d.Lost > 0 {
			d.WinRate = float64(d.Won) / float64(d.Won+d.Lost)
		}
		out = append(out, *d)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Date < out[j].Date })
	return out
}

// handleConfig 返回当前策略配置（前端展示标定参数）。
func (s *State) handleConfig(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, map[string]interface{}{
		"trigger_ask_max": s.cfg.TriggerAskMax,
		"crash_min_ask":   s.cfg.CrashMinAsk,
		"crash_window":    s.cfg.CrashWindow,
		"dist_lo_yes":     s.cfg.DistLoYes,
		"dist_lo_no":      s.cfg.DistLoNo,
		"dist_hi":         s.cfg.DistHi,
		"rem_min":         s.cfg.RemMin,
		"stake":           s.cfg.Stake,
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

// signalStats 汇总信号统计: 总数/赢/输/待结算/胜率/累计 P&L。
// 胜率按已结算信号计（待结算不计入分母）。
func (s *State) signalStats() (total, won, lost, pending int, winRate, cumPnl float64) {
	obsCount, sigCount, wonCount := s.recorder.Counts()
	_ = obsCount
	pending = len(s.recorder.PendingSignals())
	total, won = sigCount, wonCount
	lost = sigCount - wonCount - pending
	if resolved := won + lost; resolved > 0 {
		winRate = float64(won) / float64(resolved)
	}
	for _, d := range s.recorder.DailyPnl() {
		cumPnl += d.PnL
	}
	return
}

// dailySummary 返回逐日 P&L 序列与盈利天数。
func (s *State) dailySummary() (daily []flip.DayPnl, dayPos int) {
	daily = s.recorder.DailyPnl()
	for _, d := range daily {
		if d.PnL > 0 {
			dayPos++
		}
	}
	return
}

// mapRecords 将 flip.Record 映射为 API 响应元素。
func mapRecords(records []*flip.Record) []recordResponse {
	out := make([]recordResponse, 0, len(records))
	for _, rec := range records {
		out = append(out, recordResponse{
			Ts:           rec.Ts,
			Date:         rec.Date,
			ConditionID:  rec.ConditionID,
			Slug:         rec.Slug,
			Side:         rec.Side,
			Rem:          rec.Rem,
			Fill:         rec.Fill,
			M20:          rec.M20,
			M30:          rec.M30,
			M45:          rec.M45,
			DistS:        rec.DistS,
			DistT:        rec.DistT,
			OK:           rec.OK,
			RejectReason: rec.RejectReason,
			Shares:       rec.Shares,
			BookLatMs:    rec.BookLatMs,
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
