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

	// 统计汇总（已结算 + 待结算 + 无仓位; 恒等式 signal = won + lost + pending + noexec）
	ObservationCount int     `json:"observation_count"` // 全部触底观测（含失败）
	SignalCount      int     `json:"signal_count"`
	WonCount         int     `json:"won_count"`
	LostCount        int     `json:"lost_count"`
	PendingCount     int     `json:"pending_count"` // 有仓位、等结算回填
	NoExecCount      int     `json:"noexec_count"`  // 无仓位: 被闸/被拒/未成交/挂单未定稿（不入胜率）
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

	// 执行/风控可见性（2026-09-24 追加）。此前这三个字段不在响应里, 于是被风控闸拦下
	// 或下单失败的行在页面上与「在途待结算」长得一模一样——结果列永远停在「待结算」,
	// 而它们**永远不会**被结算（无仓位）。live 每次重启都产生一条首窗禁单行。
	GateReason string `json:"gate_reason,omitempty"` // 风控闸: first_window | daily_loss
	ExecStatus string `json:"exec_status,omitempty"` // live 执行终态: filled/partial/unfilled/rejected/resting/submitting
	ExecNote   string `json:"exec_note,omitempty"`   // 拒绝/未成交的说明（前端悬停显示）
}

// flipDailyRow 是 flip /api/daily 的一行 = 共用骨架 + 本族专有的「无仓位」细分列。
//
// 为何要拆（2026-09-24）: 共用骨架的 Pending（won == nil）把两类完全不同的行混在
// 一起——有仓位在途（等结算编排回填, 几十秒内必然落定）与**永远不会有结算的行**
// （被风控闸拦下 / 下单被拒 / 0 成交 / GTC 挂单未定稿）。后者算「待结算」会让日表的
// 待结算列永不归零（live 每次重启都留一条首窗禁单行）。故照 tail 的 tailDailyRow
// 同款做法拆出本族专有列:
//
//	Signals = Won + Lost + Pending + NoExec
type flipDailyRow struct {
	dayAgg
	NoExec int `json:"noexec"` // 无仓位行数（被闸/被拒/未成交/挂单未定稿）
}

// dailyResp 是 flip /api/daily 的响应体（rows 时间正序 + 合计行）。
type dailyResp struct {
	Days  []flipDailyRow `json:"days"`
	Total flipDailyRow   `json:"total"`
}

// ── Handlers ──

// handleState 返回运行状态与统计汇总。
func (s *FlipState) handleState(w http.ResponseWriter, r *http.Request) {
	live := s.snapshot.Snapshot()
	t := s.tally()
	daily, dayPos := s.dailySummary()

	// 胜率只按已结算（赢+输）计: 待结算与无仓位行既不入分子也不入分母
	winRate, cumPnl := 0.0, 0.0
	if resolved := t.Won + t.Lost; resolved > 0 {
		winRate = float64(t.Won) / float64(resolved)
	}
	for _, d := range daily {
		cumPnl += d.PnL
	}

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
		SignalCount:      t.Total,
		WonCount:         t.Won,
		LostCount:        t.Lost,
		PendingCount:     t.Pending,
		NoExecCount:      t.NoExec,
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
	days := s.collectDaily()
	writeJSON(w, dailyResp{Days: days, Total: sumFlipDaily(days)})
}

// collectDaily 按记录 date 字段（UTC 日）聚合逐日统计（任意序输入，输出时间正序）。
// 分类口径与 /api/state 的 tally 同源（同一份 pending 集合 + 同一恒等式）:
// 无仓位行记 NoExec, **不再混进 Pending**——否则 live 每天都会留下一条永不归零的
// 「待结算」（见 flipDailyRow 注释）。
func (s *FlipState) collectDaily() []flipDailyRow {
	pending := s.pendingSet()
	byDay := map[string]*dayAgg{}
	noexec := map[string]int{}
	for _, rec := range s.recorder.Observations() {
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
		case rec.Won == nil && pending[rec.ConditionID]:
			d.Pending++
		case rec.Won == nil:
			noexec[rec.Date]++
		case *rec.Won:
			d.Won++
			d.PnL += rec.PnL
		default:
			d.Lost++
			d.PnL += rec.PnL
		}
	}
	// 先复用共用骨架收敛（日期正序 + 胜率），再贴回本族专有列
	base := finalizeDaily(byDay)
	out := make([]flipDailyRow, 0, len(base))
	for _, d := range base {
		out = append(out, flipDailyRow{dayAgg: d, NoExec: noexec[d.Date]})
	}
	return out
}

// sumFlipDaily 累加逐日行（共用骨架合计 + NoExec 列）。
func sumFlipDaily(days []flipDailyRow) flipDailyRow {
	base := make([]dayAgg, 0, len(days))
	var noexec int
	for _, d := range days {
		base = append(base, d.dayAgg)
		noexec += d.NoExec
	}
	return flipDailyRow{dayAgg: sumDaily(base), NoExec: noexec}
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

// signalTally 是信号分类汇总（每行**只落一类**, 恒等式 Total = Won + Lost + Pending + NoExec）。
//
// ⚠️ NoExec 必须独立成类（2026-09-24 修）: 原实现用 `lost = sigCount − won − pending`
// 反算「负」, 于是「被风控闸拦下 / 下单被拒 / 0 成交 / 挂单未定稿」这些**从未有过仓位**的
// 行被算成了「输」——它们既没赢也没输: 混进分母会压低胜率, 混进分子会虚增亏损笔数。
// 这正是用户看到的那条日志（首窗禁单行）在页面上完全消失的原因之一。
type signalTally struct {
	Total   int
	Won     int
	Lost    int
	Pending int // 有仓位、等结算编排回填
	NoExec  int // 无仓位（被闸/被拒/未成交/挂单未定稿）
}

// pendingSet 返回「结算编排正在等回填」的 conditionID 集合。
//
// 判据以 PendingSignals()（= isSettlable 过滤后的真实持仓行）为唯一来源, 不在这里
// 用 IsFilled 复算一遍——两处判据各写一遍必然漂移, 而结算层才是「这行会不会被结算」
// 的权威（决策 #19）。
func (s *FlipState) pendingSet() map[string]bool {
	pending := map[string]bool{}
	for _, rec := range s.recorder.PendingSignals() {
		pending[rec.ConditionID] = true
	}
	return pending
}

// tally 逐行分类全部 ok 信号（观测全量在内存, 无需 Counts 的聚合口径）。
func (s *FlipState) tally() signalTally {
	pending := s.pendingSet()
	var t signalTally
	for _, rec := range s.recorder.Observations() {
		if !rec.OK {
			continue
		}
		t.Total++
		switch {
		case rec.Won != nil && *rec.Won:
			t.Won++
		case rec.Won != nil:
			t.Lost++
		case pending[rec.ConditionID]:
			t.Pending++
		default:
			t.NoExec++
		}
	}
	return t
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
		GateReason:   rec.GateReason,
		ExecStatus:   rec.ExecStatus,
		ExecNote:     rec.ExecNote,
	}
}
