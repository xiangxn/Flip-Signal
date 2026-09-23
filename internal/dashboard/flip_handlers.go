package dashboard

import (
	"net/http"
	"time"

	"github.com/necklace/flip-signal/internal/flip"
)

// ── Response 类型 ──

// stateResponse 是 flip /api/state 的响应体。
type stateResponse struct {
	TS string `json:"ts"` // 服务器当前时间（RFC3339）

	Mode        string `json:"mode"`
	UptimeSec   int64  `json:"uptime_sec"`
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

	// Chainlink TWAP-60: 最新流值 / 本窗开盘值（0 = 尚无推送或锚缺失, 前端显示「—」）
	TwapPrice float64 `json:"twap_price"`
	TwapOpen  float64 `json:"twap_open"`

	// Binance spot（−1 = 尚无推送）
	SpotPrice float64 `json:"spot_price"`
	SpotAgeMs int64   `json:"spot_age_ms"`

	// 三源新鲜度阈值（前端按此标红 book_lat/twap_age/spot_age，勿硬编码）
	Limits SourceLimits `json:"limits"`

	// 本窗 tick 健康度（引擎计数器; 窗口间为 nil——与 slug/event_start 同生命周期）。
	// lost_triggers = 本会触发但被延迟闸/整簿缺失挡掉的 tick 明细（§1.3 可见性）
	WindowStats *flip.WindowStats `json:"window_stats,omitempty"`

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

	// live 执行摘要（mode=paper 恒 nil, 前端判空隐藏）
	LiveExec *flip.LiveExec `json:"live,omitempty"`

	// 日亏熔断摘要（两模式都填; paper 的 enforced=false = 只标记不拦 POST）
	Risk *flip.RiskSummary `json:"risk,omitempty"`
}

// recordResponse 是 flip /api/observations 与 /api/signals 的元素。
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

// dailyResp 是 flip /api/daily 的响应体（rows 时间正序 + 合计行）。
type dailyResp struct {
	Days  []dayAgg `json:"days"`
	Total dayAgg   `json:"total"`
}

// ── Handlers ──

// handleState 返回运行状态与统计汇总。
func (s *FlipState) handleState(w http.ResponseWriter, r *http.Request) {
	live := s.snapshot.Snapshot()
	total, won, lost, pending, winRate, cumPnl := s.signalStats()
	daily, dayPos := s.dailySummary()

	writeJSON(w, stateResponse{
		TS:               s.nowFn().UTC().Format(time.RFC3339),
		Mode:             s.mode,
		UptimeSec:        int64(s.nowFn().Sub(s.startedAt).Seconds()),
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
		TwapPrice:        live.TwapPrice,
		TwapOpen:         live.TwapOpen,
		SpotPrice:        live.SpotPrice,
		SpotAgeMs:        live.SpotAgeMs,
		Limits:           s.limits,
		WindowStats:      live.Stats,
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
		LiveExec:         live.Live,
		Risk:             live.Risk,
	})
}

// handleObservations 返回触底观测列表（成功+失败，时间倒序分页）。
func (s *FlipState) handleObservations(w http.ResponseWriter, r *http.Request) {
	writePage(w, r, s.recorder.Observations(), recordTs, mapRecord, 50)
}

// handleSignals 返回信号列表（ok=true，含 P&L，时间倒序分页）。
func (s *FlipState) handleSignals(w http.ResponseWriter, r *http.Request) {
	writePage(w, r, s.recorder.Signals(), recordTs, mapRecord, 200)
}

// handleDaily 返回逐日盈利明细（UTC 日粒度，供前端弹窗表格）。
func (s *FlipState) handleDaily(w http.ResponseWriter, r *http.Request) {
	days := collectDaily(s.recorder.Observations())
	writeJSON(w, dailyResp{Days: days, Total: sumDaily(days)})
}

// collectDaily 按记录 date 字段（UTC 日）聚合逐日统计（任意序输入，输出时间正序）。
func collectDaily(recs []*flip.Record) []dayAgg {
	byDay := map[string]*dayAgg{}
	for _, rec := range recs {
		d := byDay[rec.Date]
		if d == nil {
			d = &dayAgg{Date: rec.Date}
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
	return finalizeDaily(byDay)
}

// handleConfig 返回当前策略配置（前端展示标定参数）。
func (s *FlipState) handleConfig(w http.ResponseWriter, r *http.Request) {
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
func (s *FlipState) remaining(live flip.LiveSnapshot) int {
	return remainingSec(live.EventStart, s.nowFn().Unix())
}

// signalStats 汇总信号统计: 总数/赢/输/待结算/胜率/累计 P&L。
// 胜率按已结算信号计（待结算不计入分母）。
func (s *FlipState) signalStats() (total, won, lost, pending int, winRate, cumPnl float64) {
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
func (s *FlipState) dailySummary() (daily []flip.DayPnl, dayPos int) {
	daily = s.recorder.DailyPnl()
	for _, d := range daily {
		if d.PnL > 0 {
			dayPos++
		}
	}
	return
}

// recordTs 取记录的排序时间戳（writePage 用）。
func recordTs(rec *flip.Record) int64 { return rec.Ts }

// mapRecord 将 flip.Record 映射为 API 响应元素。
func mapRecord(rec *flip.Record) recordResponse {
	return recordResponse{
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
	}
}
