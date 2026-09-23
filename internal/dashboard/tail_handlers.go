package dashboard

import (
	"net/http"
	"sort"
	"time"

	"github.com/necklace/flip-signal/internal/flip"
	"github.com/necklace/flip-signal/internal/tail"
)

// ── Response 类型 ──

// tailStateResponse 是 tail /api/state 的响应体。
//
// 与 flip 的 stateResponse 的形制差异（两族是两条独立策略线, 字段本就不同）:
//   - 触底/浅洞腿换成**热门侧读数**（hot_side/hot_ask/dev/sd）;
//   - 「本窗触底观测」换成**两个一次性闩锁**（frame_sent/snap_sent/anchor_frozen）;
//   - 多一段今日健康度（today_*: 判决的辅助闸门 3——窗数 / skip 分布 / 锚缺失）。
type tailStateResponse struct {
	TS string `json:"ts"` // 服务器当前时间（RFC3339）

	Mode        string `json:"mode"`
	UptimeSec   int64  `json:"uptime_sec"`
	Slug        string `json:"slug"`
	EventStart  int64  `json:"event_start"`
	EngineState string `json:"engine_state"`

	// 当前窗口盘口（实时诊断; yes=UP 侧, no=DOWN 侧）
	YesBid    float64 `json:"yes_bid"`
	YesAsk    float64 `json:"yes_ask"`
	NoBid     float64 `json:"no_bid"`
	NoAsk     float64 `json:"no_ask"`
	Remaining int     `json:"remaining_sec"` // 窗口剩余秒（0 = 窗口间）
	BookLatMs int64   `json:"book_latency_ms"`
	TwapAgeMs int64   `json:"twap_age_ms"`
	SpotAgeMs int64   `json:"spot_age_ms"` // Binance spot 距本地接收毫秒（−1 = 尚无推送）
	SpotPrice float64 `json:"spot_price"`
	TwapPrice float64 `json:"twap_price"` // Chainlink TWAP-60 流值（⑤ 判定不用它, 只作诊断）

	// 锚与 σ（锚 = 边界那一秒的 TWAP 推送, 决策 #15）
	Anchor          float64 `json:"anchor"`   // 0 = 本窗尚未取到（本窗一行不产出）
	HistBps         float64 `json:"hist_bps"` // 本窗生效 σ（bps; 0 = 不可用）
	AnchorExact     bool    `json:"anchor_exact"`
	AnchorSrc       string  `json:"anchor_src,omitempty"`
	AnchorArrivedMs int64   `json:"anchor_arrived_ms,omitempty"`

	// 尾盘读数（现算, 与落盘行同源）: 热门侧 = ask 高的一侧（平局取 yes）
	HotSide string  `json:"hot_side"`
	HotAsk  float64 `json:"hot_ask"`
	Dev     float64 `json:"dev"` // 位移（**美元**, 正 = 朝热门侧方向）
	Sd      float64 `json:"sd"`  // 该窗 1σ 折美元（0 = σ 不可用）

	// 本窗两个闩锁（帧 rem≤150 / 决策快照 rem≤60）+ 锚冻结
	FrameSent    bool `json:"frame_sent"`
	SnapSent     bool `json:"snap_sent"`
	AnchorFrozen bool `json:"anchor_frozen"`

	// 三源新鲜度阈值（前端按此标红，勿硬编码）
	Limits SourceLimits `json:"limits"`

	// 本窗 tick 健康度（引擎计数器; 窗口间为 nil）
	WindowStats *tail.WindowStats `json:"window_stats,omitempty"`

	// 统计汇总（已结算 + 待结算）——snap 行口径, 帧行不计（无仓位语义）
	SnapCount    int     `json:"snap_count"` // 全部决策快照行（含否决与被闸）
	SignalCount  int     `json:"signal_count"`
	WonCount     int     `json:"won_count"`
	LostCount    int     `json:"lost_count"`
	PendingCount int     `json:"pending_count"`
	WinRate      float64 `json:"win_rate"` // 已结算口径（待结算不计入分母）
	CumPnl       float64 `json:"cumulative_pnl"`
	DayPnlPos    int     `json:"day_pnl_pos"`
	DayTotal     int     `json:"day_total"`
	MaxDrawdown  float64 `json:"max_drawdown"`

	// 今日健康度（判决的辅助闸门 3; 读当日 tailstats_*.jsonl）
	TodayStatsDay string         `json:"today_stats_day,omitempty"` // 读的是哪个 UTC 日的文件
	TodayWindows  int            `json:"today_windows"`             // 本日实际采集窗数（Skip 为空）
	TodayRows     int            `json:"today_rows"`                // 本日健康度行数（≈288 即主循环跑满）
	TodaySkips    map[string]int `json:"today_skips,omitempty"`     // skip 原因分布
	TodayNoAnchor int            `json:"today_no_anchor"`           // 采集了但没取到锚的窗数

	// live 执行摘要（mode=paper 恒 nil, 前端判空隐藏）
	Live *flip.LiveExec `json:"live,omitempty"`
	// 日亏熔断摘要（两模式都填; paper 的 enforced=false = 只标记不拦 POST）
	Risk *flip.RiskSummary `json:"risk,omitempty"`
}

// tailRecordResponse 是 tail /api/snaps 与 /api/frames 的元素（两族行共用一套字段
// ——帧行只是判定段为空）。
type tailRecordResponse struct {
	Ts          int64  `json:"ts"`
	Date        string `json:"date"`
	ConditionID string `json:"condition_id"`
	Slug        string `json:"slug"`
	Kind        string `json:"kind"` // snap | frame
	FrameT      int    `json:"frame_t"`
	Rem         int    `json:"rem"`

	YesBid    float64 `json:"yes_bid"`
	YesAsk    float64 `json:"yes_ask"`
	NoBid     float64 `json:"no_bid"`
	NoAsk     float64 `json:"no_ask"`
	Spot      float64 `json:"spot"`
	Twap      float64 `json:"twap"`
	Anchor    float64 `json:"anchor"`
	HistBps   float64 `json:"hist_bps"`
	BookLatMs int64   `json:"book_latency_ms"`
	SpotAgeMs int64   `json:"spot_age_ms,omitempty"`
	TwapAgeMs int64   `json:"twap_age_ms,omitempty"`

	Side   string  `json:"side"` // 热门侧: yes | no
	HotAsk float64 `json:"hot_ask"`
	Dev    float64 `json:"dev,omitempty"`
	Sd     float64 `json:"sd,omitempty"`

	// 四条原始腿（前端据此点亮五格徽章; 帧行恒 false 全灭）
	RulePrice   bool `json:"rule_price"`
	RuleDev63   bool `json:"rule_dev63"`
	RuleSigma   bool `json:"rule_sigma"`
	RuleSigma40 bool `json:"rule_sigma_usd40"`

	OK           bool    `json:"ok"`
	RejectReason string  `json:"reject_reason,omitempty"`
	Shares       float64 `json:"shares,omitempty"`

	Stake      float64 `json:"stake,omitempty"`
	Won        *bool   `json:"won,omitempty"`
	PnL        float64 `json:"pnl,omitempty"`
	ResolvedAt string  `json:"resolved_at,omitempty"`
	GateReason string  `json:"gate_reason,omitempty"`

	ExecStatus string  `json:"exec_status,omitempty"`
	OrderID    string  `json:"order_id,omitempty"`
	FillPrice  float64 `json:"avg_fill_price,omitempty"`
	Cost       float64 `json:"cost,omitempty"`
	ExecNote   string  `json:"exec_note,omitempty"`
}

// tailDailyRow 是 tail /api/daily 的一行（= 共用骨架 + 帧/注数细分）。
//
// Obs（骨架里 = 全部行数）在 tail 里 = Frames + Snaps; NotesPerDay 是判决频率闸的
// 输入（文档 §5.2: ⑤ 应落 90~120 注/日）, 放在这里让「哪一天频率异常」一眼可见。
type tailDailyRow struct {
	dayAgg
	Frames      int     `json:"frames"`        // 本日帧行数（rem≤150 的原始快照）
	Snaps       int     `json:"snaps"`         // 本日决策快照行数（rem≤60）
	NotesPerDay float64 `json:"notes_per_day"` // 注/日（= Signals; 与 90~120 对照）
}

// tailDailyResp 是 tail /api/daily 的响应体。
type tailDailyResp struct {
	Days  []tailDailyRow `json:"days"`
	Total tailDailyRow   `json:"total"`
}

// judgeResp 是 tail /api/judge 的响应体（判决卡的全部数据源）。
type judgeResp struct {
	Meta   tail.JudgeMeta `json:"meta"`
	Grids  []tail.Grid    `json:"grids"`
	Ruler5 tail.Grid      `json:"ruler5"` // ⑤ 本尊（前端判决卡直取, 免得按字符串找）
}

// ── Handlers ──

// handleState 返回运行状态、统计汇总与今日健康度。
func (s *TailState) handleState(w http.ResponseWriter, r *http.Request) {
	live := s.snapshot.Snapshot()
	snap, sig, won := s.recorder.Counts()
	pending := len(s.recorder.PendingSignals())
	lost := sig - won - pending
	winRate := 0.0
	if resolved := won + lost; resolved > 0 {
		winRate = float64(won) / float64(resolved)
	}
	daily := s.recorder.DailyPnl()
	cumPnl, dayPos := 0.0, 0
	for _, d := range daily {
		cumPnl += d.PnL
		if d.PnL > 0 {
			dayPos++
		}
	}

	// 今日健康度（辅助闸门 3）: 读当日 tailstats 文件。读失败不阻断 /api/state
	// ——状态页其余部分仍要能看（错误只反映在窗数恒 0）。
	today := s.nowFn().UTC().Format("2006-01-02")
	rows, err := s.recorder.TodayStats()
	if err != nil {
		today = ""
	}
	todayRows, todayWindows, todayNoAnchor := 0, 0, 0
	skips := map[string]int{}
	for _, e := range rows {
		todayRows++
		if e.Skip != "" {
			skips[e.Skip]++
			continue
		}
		todayWindows++
		if !e.AnchorExact {
			todayNoAnchor++
		}
	}

	writeJSON(w, tailStateResponse{
		TS:              s.nowFn().UTC().Format(time.RFC3339),
		Mode:            s.mode,
		UptimeSec:       int64(s.nowFn().Sub(s.startedAt).Seconds()),
		Slug:            live.Slug,
		EventStart:      live.EventStart,
		EngineState:     live.EngineState,
		YesBid:          live.YesBid,
		YesAsk:          live.YesAsk,
		NoBid:           live.NoBid,
		NoAsk:           live.NoAsk,
		Remaining:       remainingSec(live.EventStart, s.nowFn().Unix()),
		BookLatMs:       live.BookLatMs,
		TwapAgeMs:       live.TwapAgeMs,
		SpotAgeMs:       live.SpotAgeMs,
		SpotPrice:       live.SpotPrice,
		TwapPrice:       live.TwapPrice,
		Anchor:          live.Anchor,
		HistBps:         live.HistBps,
		AnchorExact:     live.AnchorExact,
		AnchorSrc:       live.AnchorSrc,
		AnchorArrivedMs: live.AnchorArrivedMs,
		HotSide:         live.HotSide,
		HotAsk:          live.HotAsk,
		Dev:             live.Dev,
		Sd:              live.Sd,
		FrameSent:       live.FrameSent,
		SnapSent:        live.SnapSent,
		AnchorFrozen:    live.AnchorFrozen,
		Limits:          s.limits,
		WindowStats:     live.Stats,
		SnapCount:       snap,
		SignalCount:     sig,
		WonCount:        won,
		LostCount:       lost,
		PendingCount:    pending,
		WinRate:         winRate,
		CumPnl:          cumPnl,
		DayPnlPos:       dayPos,
		DayTotal:        len(daily),
		MaxDrawdown:     s.recorder.MaxDrawdown(),
		TodayStatsDay:   today,
		TodayWindows:    todayWindows,
		TodayRows:       todayRows,
		TodaySkips:      skips,
		TodayNoAnchor:   todayNoAnchor,
		Live:            live.Live,
		Risk:            live.Risk,
	})
}

// handleSnaps 返回决策快照行（成功+否决+被闸，时间倒序分页）。
func (s *TailState) handleSnaps(w http.ResponseWriter, r *http.Request) {
	all := filterKind(s.recorder.Observations(), tail.KindSnap)
	writePage(w, r, all, tailRecTs, mapTailRecord, 50)
}

// handleFrames 返回原始帧行（rem≤150 快照，时间倒序分页）。
//
// 与 snap 表分开的理由: 帧行是「那一刻市场长什么样」的原稿, 数量与快照行同阶
// （每窗各至多一条）, 混在一张表里只会让两种语义的行互相淹没。
func (s *TailState) handleFrames(w http.ResponseWriter, r *http.Request) {
	all := filterKind(s.recorder.Observations(), tail.KindFrame)
	writePage(w, r, all, tailRecTs, mapTailRecord, 50)
}

// handleDaily 返回逐日明细（UTC 日粒度，含注数频率——判决频率闸的逐日视角）。
func (s *TailState) handleDaily(w http.ResponseWriter, r *http.Request) {
	days := s.collectDaily()
	total := tailDailyRow{dayAgg: sumDaily(daysToAggs(days))}
	for _, d := range days {
		total.Frames += d.Frames
		total.Snaps += d.Snaps
	}
	if len(days) > 0 {
		total.NotesPerDay = float64(total.Signals) / float64(len(days)) // 合计行 = 日均注数
	}
	writeJSON(w, tailDailyResp{Days: days, Total: total})
}

// handleJudge 返回判决速览（五格 + T=150 对照格 + 日级 bootstrap 区间 + 判词）。
//
// ⚠️ 每次请求**现算**（全量行 → 6 格 × 2000 次重采样）。量级: 万行级别 + 12 万次
// 抽取, 毫秒级; 不做缓存是因为缓存失效判据（新结算行到达）要么漏要么复杂, 而
// 判决口径本身就是「按需重算」的纯函数（tail.Judge 只读副本）。
func (s *TailState) handleJudge(w http.ResponseWriter, r *http.Request) {
	grids := tail.Judge(s.recorder.Observations(), s.cfg)
	resp := judgeResp{Meta: tail.Meta(), Grids: grids}
	for _, g := range grids {
		if g.Rule == "5" {
			resp.Ruler5 = g
		}
	}
	writeJSON(w, resp)
}

// handleConfig 返回当前策略配置（前端展示标定参数; 键名 = mapstructure tag）。
func (s *TailState) handleConfig(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, map[string]interface{}{
		"rem_start":       s.cfg.RemStart,
		"frame_rem":       s.cfg.FrameRem,
		"price_min":       s.cfg.PriceMin,
		"dev_min_usd":     s.cfg.DevMinUSD,
		"sigma_min_usd":   s.cfg.SigmaMinUSD,
		"stake":           s.cfg.Stake,
		"max_book_lat_ms": s.cfg.MaxBookLatMs,
	})
}

// ── 内部 ──

// filterKind 取指定 kind 的行（副本切片, writePage 会原地排序）。
func filterKind(recs []*tail.Record, kind string) []*tail.Record {
	out := make([]*tail.Record, 0, len(recs))
	for _, rec := range recs {
		if rec.Kind == kind {
			out = append(out, rec)
		}
	}
	return out
}

// tailRecTs 取记录的排序时间戳（writePage 用）。
func tailRecTs(rec *tail.Record) int64 { return rec.Ts }

// mapTailRecord 将 tail.Record 映射为 API 响应元素（snap 与 frame 共用一套字段）。
func mapTailRecord(rec *tail.Record) tailRecordResponse {
	return tailRecordResponse{
		Ts:           rec.Ts,
		Date:         rec.Date,
		ConditionID:  rec.ConditionID,
		Slug:         rec.Slug,
		Kind:         rec.Kind,
		FrameT:       rec.FrameT,
		Rem:          rec.Rem,
		YesBid:       rec.YesBid,
		YesAsk:       rec.YesAsk,
		NoBid:        rec.NoBid,
		NoAsk:        rec.NoAsk,
		Spot:         rec.Spot,
		Twap:         rec.Twap,
		Anchor:       rec.Anchor,
		HistBps:      rec.HistBps,
		BookLatMs:    rec.BookLatMs,
		SpotAgeMs:    rec.SpotAgeMs,
		TwapAgeMs:    rec.TwapAgeMs,
		Side:         rec.Side,
		HotAsk:       rec.HotAsk,
		Dev:          rec.Dev,
		Sd:           rec.Sd,
		RulePrice:    rec.Rules.Price,
		RuleDev63:    rec.Rules.Dev63,
		RuleSigma:    rec.Rules.Sigma,
		RuleSigma40:  rec.Rules.SigmaUSD40,
		OK:           rec.OK,
		RejectReason: rec.RejectReason,
		Shares:       rec.Shares,
		Stake:        rec.Stake,
		Won:          rec.Won,
		PnL:          rec.PnL,
		ResolvedAt:   rec.ResolvedAt,
		GateReason:   rec.GateReason,
		ExecStatus:   rec.ExecStatus,
		OrderID:      rec.OrderID,
		FillPrice:    rec.FillPrice,
		Cost:         rec.Cost,
		ExecNote:     rec.ExecNote,
	}
}

// collectDaily 按记录 date 字段（UTC 日）聚合逐日统计（任意序输入，输出时间正序）。
//
// Obs（骨架字段）= 帧行 + 快照行——它回答的是「这一天引擎写了多少行」;
// Frames/Snaps 拆开看, NotesPerDay = 当日 ok 信号数（逐日就是「注/日」本身, 与
// 判决的频率闸 90~120 同量纲）。
func (s *TailState) collectDaily() []tailDailyRow {
	type acc struct {
		agg          dayAgg
		frames, snap int
	}
	byDay := map[string]*acc{}
	for _, rec := range s.recorder.Observations() {
		a := byDay[rec.Date]
		if a == nil {
			a = &acc{agg: dayAgg{Date: rec.Date}}
			byDay[rec.Date] = a
		}
		a.agg.Obs++
		switch rec.Kind {
		case tail.KindFrame:
			a.frames++
			continue // 帧行没有判定/仓位段
		case tail.KindSnap:
			a.snap++
		}
		if !rec.OK {
			continue
		}
		a.agg.Signals++
		switch {
		case rec.Won == nil:
			a.agg.Pending++
		case *rec.Won:
			a.agg.Won++
			a.agg.PnL += rec.PnL
		default:
			a.agg.Lost++
			a.agg.PnL += rec.PnL
		}
	}

	out := make([]tailDailyRow, 0, len(byDay))
	for _, a := range byDay {
		g := a.agg
		if n := g.Won + g.Lost; n > 0 {
			g.WinRate = float64(g.Won) / float64(n)
		}
		out = append(out, tailDailyRow{
			dayAgg: g, Frames: a.frames, Snaps: a.snap,
			NotesPerDay: float64(g.Signals),
		})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Date < out[j].Date })
	return out
}

// daysToAggs 把扩展行降回共用骨架（合计行用 sumDaily）。
func daysToAggs(rows []tailDailyRow) []dayAgg {
	out := make([]dayAgg, 0, len(rows))
	for _, r := range rows {
		out = append(out, r.dayAgg)
	}
	return out
}
