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
//   - 触底/浅洞腿换成**热门侧读数**（hot_side/hot_ask/hot_src/dev/sd）;
//   - 「本窗触底观测」换成**三段链的四个闩锁**（t150_sent/t60_sent/listening/
//     signal_sent/anchor_frozen）;
//   - 多一段今日健康度（today_*: 窗数 / skip 分布 / 锚缺失）。
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
	TwapPrice float64 `json:"twap_price"` // Chainlink TWAP-60 流值（判定不用它, 只作诊断）

	// 锚与 σ（锚 = 边界那一秒的 TWAP 推送, 决策 #15）
	Anchor          float64 `json:"anchor"`   // 0 = 本窗尚未取到（本窗一行不产出）
	HistBps         float64 `json:"hist_bps"` // 本窗生效 σ（bps; 0 = 不可用）
	AnchorExact     bool    `json:"anchor_exact"`
	AnchorSrc       string  `json:"anchor_src,omitempty"`
	AnchorArrivedMs int64   `json:"anchor_arrived_ms,omitempty"`

	// 尾盘读数（现算, 与落盘行同源）: 热门侧 = **有效价**高的一侧（每侧 ask 优先、
	// ask 空则退 bid；平局取 yes）
	HotSide string  `json:"hot_side"`
	HotAsk  float64 `json:"hot_ask"`
	HotSrc  string  `json:"hot_src,omitempty"` // ask | bid（bid = 该侧卖单被撤空, 只能按买价挂）
	Dev     float64 `json:"dev"`               // 位移（**美元**, 正 = 朝热门侧方向）
	Sd      float64 `json:"sd"`                // 该窗 1σ 折美元（0 = σ 不可用）

	// 本窗三段链的进度 + 锚冻结（均取自引擎 Latches, 判定路径不读它们）。
	// 语义都是「已定」: t60_sent 蕴含 t150_sent; signal_sent 蕴含整窗已下单（此后 Done）。
	T150Sent     bool `json:"t150_sent"`
	T60Sent      bool `json:"t60_sent"`
	Listening    bool `json:"listening"`
	SignalSent   bool `json:"signal_sent"`
	AnchorFrozen bool `json:"anchor_frozen"`

	// 三源新鲜度阈值（前端按此标红，勿硬编码）
	Limits SourceLimits `json:"limits"`

	// 本窗 tick 健康度（引擎计数器; 窗口间为 nil）
	WindowStats *tail.WindowStats `json:"window_stats,omitempty"`

	// 统计汇总（恒等式 signal_count = won + lost + pending + noexec）
	DecisionCount int     `json:"decision_count"` // 判定行数（t150/t60 未出信号的判定）
	SignalCount   int     `json:"signal_count"`   // 全部信号（含未成交与被闸）
	WonCount      int     `json:"won_count"`      // 已结算且有仓位（按官方 outcome）
	LostCount     int     `json:"lost_count"`
	PendingCount  int     `json:"pending_count"` // 有仓位、等结算回填
	NoExecCount   int     `json:"noexec_count"`  // 未成交: 被闸/被拒/0 成交/挂单未定稿（不入胜率）
	WinRate       float64 `json:"win_rate"`      // 已结算口径（未成交与待结算都不进分母）
	CumPnl        float64 `json:"cumulative_pnl"`
	DayPnlPos     int     `json:"day_pnl_pos"` // 逐日盈利天数（有仓位的已结算日）
	DayTotal      int     `json:"day_total"`
	MaxDrawdown   float64 `json:"max_drawdown"`

	// 今日健康度（辅助闸门: 读当日 tailstats_*.jsonl）
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

// tailRecordResponse 是 tail /api/snaps 与 /api/signals 的元素（两族行共用一套字段
// ——判定行只是 ok=false、无执行字段）。
type tailRecordResponse struct {
	Ts          int64  `json:"ts"`
	Date        string `json:"date"`
	ConditionID string `json:"condition_id"`
	Slug        string `json:"slug"`
	Kind        string `json:"kind"`            // snap（frame/scan 为 legacy 行, 不再产出）
	Stage       string `json:"stage,omitempty"` // t150 | t60 | listen; 空 = legacy 旧行
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
	HotSrc string  `json:"hot_src,omitempty"` // ask | bid（bid = 按买价兜底下单）
	Dev    float64 `json:"dev,omitempty"`
	Sd     float64 `json:"sd,omitempty"`

	// 四条原始腿（前端据此点亮徽章; 判定行的被拒腿一目了然）
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
	// HasPosition 是否有真实仓位（= 实际成交）——前端据此把未成交行的 P&L 显示成「—」
	// （未成交行照显官方结果, 但盈亏恒 0, 见 recorder.recomputePnL）。
	HasPosition bool   `json:"has_position"`
	GateReason  string `json:"gate_reason,omitempty"`

	ExecStatus string  `json:"exec_status,omitempty"`
	OrderID    string  `json:"order_id,omitempty"`
	FillPrice  float64 `json:"avg_fill_price,omitempty"`
	Cost       float64 `json:"cost,omitempty"`
	ExecNote   string  `json:"exec_note,omitempty"`
}

// tailDailyRow 是 tail /api/daily 的一行（= 共用骨架 + 本族的段/未成交细分列）。
//
//	Signals = Won + Lost + Pending + NoExec
//
// 三个段列（T150/T60/Listen）是**信号数按来源分桶**（T150+T60+Listen+legacy 无 stage 行
// = Signals）; Decisions 是判定行数（t150/t60 段没出信号的那些行, OK=false）。
// NoExec 是未成交信号数（无仓位: 被闸/被拒/0 成交/挂单未定稿）。
type tailDailyRow struct {
	dayAgg
	Decisions int `json:"decisions"` // 本日判定行数（未出信号的判定）
	T150      int `json:"t150"`      // 本日第一段（rem≤t150_rem 判⑤）出的信号数
	T60       int `json:"t60"`       // 本日第二段（rem≤t60_rem 判⑤）出的信号数
	Listen    int `json:"listen"`    // 本日监听段（每秒判②）出的信号数
	NoExec    int `json:"noexec"`    // 本日未成交信号数（无仓位, 不入胜率）
}

// tailDailyResp 是 tail /api/daily 的响应体。
type tailDailyResp struct {
	Days  []tailDailyRow `json:"days"`
	Total tailDailyRow   `json:"total"`
}

// ── Handlers ──

// handleState 返回运行状态、统计汇总与今日健康度。
func (s *TailState) handleState(w http.ResponseWriter, r *http.Request) {
	live := s.snapshot.Snapshot()
	t := s.tally()
	decisions := s.decisionCount()
	daily := s.recorder.DailyPnl()
	cumPnl, dayPos := 0.0, 0
	for _, d := range daily {
		cumPnl += d.PnL
		if d.PnL > 0 {
			dayPos++
		}
	}

	// 今日健康度（读当日 tailstats 文件）。读失败不阻断 /api/state
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
		HotSrc:          live.HotSrc,
		Dev:             live.Dev,
		Sd:              live.Sd,
		T150Sent:        live.T150Sent,
		T60Sent:         live.T60Sent,
		Listening:       live.Listening,
		SignalSent:      live.SignalSent,
		AnchorFrozen:    live.AnchorFrozen,
		Limits:          s.limits,
		WindowStats:     live.Stats,
		DecisionCount:   decisions,
		SignalCount:     t.Total,
		WonCount:        t.Won,
		LostCount:       t.Lost,
		PendingCount:    t.Pending,
		NoExecCount:     t.NoExec,
		WinRate:         t.WinRate(),
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

// handleSnaps 返回全部决策行（判定行 + 信号行, 时间倒序分页）。
func (s *TailState) handleSnaps(w http.ResponseWriter, r *http.Request) {
	all := filterKind(s.recorder.Observations(), tail.KindSnap)
	writePage(w, r, all, tailRecTs, mapTailRecord, 50)
}

// handleSignals 返回**信号行**（ok=true: 判定通过、已进执行路径的那些, 时间倒序分页）。
//
// 与 /api/snaps 分开的理由: 判定行（t150/t60 未达标）数量是信号的两三倍, 混在一张表
// 里会把真正下过单的行淹没; 页面的「信号」表要的是「这一笔下没下、成没成、赢没赢」。
func (s *TailState) handleSignals(w http.ResponseWriter, r *http.Request) {
	writePage(w, r, s.recorder.Signals(), tailRecTs, mapTailRecord, 50)
}

// handleDaily 返回逐日明细（UTC 日粒度，含段分布与未成交——判决频率闸的逐日视角）。
func (s *TailState) handleDaily(w http.ResponseWriter, r *http.Request) {
	days := s.collectDaily()
	total := tailDailyRow{dayAgg: sumDaily(daysToAggs(days))}
	for _, d := range days {
		total.Decisions += d.Decisions
		total.T150 += d.T150
		total.T60 += d.T60
		total.Listen += d.Listen
		total.NoExec += d.NoExec
	}
	writeJSON(w, tailDailyResp{Days: days, Total: total})
}

// handleConfig 返回当前策略配置（前端展示标定参数; 键名 = mapstructure tag）。
func (s *TailState) handleConfig(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, map[string]interface{}{
		"t150_rem":        s.cfg.T150Rem,
		"t60_rem":         s.cfg.T60Rem,
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

// mapTailRecord 将 tail.Record 映射为 API 响应元素（判定行与信号行共用一套字段）。
func mapTailRecord(rec *tail.Record) tailRecordResponse {
	return tailRecordResponse{
		Ts:           rec.Ts,
		Date:         rec.Date,
		ConditionID:  rec.ConditionID,
		Slug:         rec.Slug,
		Kind:         rec.Kind,
		Stage:        rec.Stage,
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
		HotSrc:       rec.HotSrc,
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
		HasPosition:  rec.HasPosition(),
		GateReason:   rec.GateReason,
		ExecStatus:   rec.ExecStatus,
		OrderID:      rec.OrderID,
		FillPrice:    rec.FillPrice,
		Cost:         rec.Cost,
		ExecNote:     rec.ExecNote,
	}
}

// tailTally 是信号分类汇总（每行**只落一类**, 恒等式 Total = Won + Lost + Pending + NoExec）。
//
// 判据是 **HasPosition**（实际成交 ∧ 非 legacy 对账行）:
//   - 无仓位（NoExec）= 下单失败/被风控拦/下单后未成交/挂单未定稿——a.md 第 4 条明确
//     「未成交信号不进入胜率计算」, 且它们**照显官方结果**（won 结算后非 nil, 但 P&L 恒 0）;
//   - 有仓位 = 在途（Pending, 等结算编排回填）或已结算（Won/Lost, 胜率分母只有这两类）。
//
// ⚠️ 不能用 `lost = signals − won − pending` 反算: 那会把从未有过仓位的行算成「输」
// （同时虚增亏损笔数与压低胜率）——flip 侧 2026-09-24 修过同一个坑（决策 #20）。
type tailTally struct {
	Total   int
	Won     int
	Lost    int
	Pending int // 有仓位、等结算编排回填
	NoExec  int // 无仓位（被闸/被拒/未成交/挂单未定稿）
}

// WinRate 已结算胜率（分母只含赢+输, 未成交与待结算都不进）。
func (t tailTally) WinRate() float64 {
	if n := t.Won + t.Lost; n > 0 {
		return float64(t.Won) / float64(n)
	}
	return 0
}

// tally 逐行分类全部信号（判据见 tailTally）。
func (s *TailState) tally() tailTally {
	var t tailTally
	for _, rec := range s.recorder.Observations() {
		if !rec.OK {
			continue
		}
		t.Total++
		switch {
		case !rec.HasPosition():
			t.NoExec++
		case rec.Won == nil:
			t.Pending++
		case *rec.Won:
			t.Won++
		default:
			t.Lost++
		}
	}
	return t
}

// decisionCount 本日/累计判定行数（kind=snap ∧ ok=false; t150/t60 段没出信号的那些行）。
func (s *TailState) decisionCount() int {
	n := 0
	for _, rec := range s.recorder.Observations() {
		if rec.Kind == tail.KindSnap && !rec.OK {
			n++
		}
	}
	return n
}

// collectDaily 按记录 date 字段（UTC 日）聚合逐日统计（任意序输入，输出时间正序）。
//
// Obs（骨架字段）= 本日全部行数（2026-09-24 起本族只产 kind=snap 一种行）;
// Decisions = 判定行; 三个段列 = 信号按 stage 分桶; NoExec = 未成交信号。
// ⚠️ legacy 行（frame/scan, 09-23/09-24 两天）: frame 归 Obs 但不属任何段;
// scan 是旧口径的对账行, OK=true 而**无仓位**——它会进 Signals 与 NoExec（判据统一
// 用 HasPosition, 不为它单开一列）。段列合计因此可能小于 Signals, 差额即 legacy 行。
func (s *TailState) collectDaily() []tailDailyRow {
	type acc struct {
		agg                 dayAgg
		decisions, t150     int
		t60, listen, noexec int
	}
	byDay := map[string]*acc{}
	for _, rec := range s.recorder.Observations() {
		a := byDay[rec.Date]
		if a == nil {
			a = &acc{agg: dayAgg{Date: rec.Date}}
			byDay[rec.Date] = a
		}
		a.agg.Obs++
		if !rec.OK {
			if rec.Kind == tail.KindSnap {
				a.decisions++
			}
			continue
		}
		a.agg.Signals++
		switch rec.Stage {
		case tail.StageT150:
			a.t150++
		case tail.StageT60:
			a.t60++
		case tail.StageListen:
			a.listen++
		}
		switch {
		case !rec.HasPosition():
			a.noexec++
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
			dayAgg: g, Decisions: a.decisions, T150: a.t150, T60: a.t60,
			Listen: a.listen, NoExec: a.noexec,
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
