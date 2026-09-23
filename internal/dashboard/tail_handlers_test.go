package dashboard

// tail dashboard 的 handler 测试: /api/state 透传（含今日健康度磁盘读口）、
// 快照表/帧表的分页与 kind 过滤、/api/judge 在样本不足时给「未到判决时点」。
//
// 判决本身的数值口径（MT19937 黄金向量、boot_days 逐位一致、判词表）在
// internal/tail/judge_test.go 里钉; 这里只管 HTTP 接线的行为。

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/necklace/flip-signal/internal/flip"
	"github.com/necklace/flip-signal/internal/tail"
)

// fakeTailSnap 实现 tail.Snapshotter（固定快照）。
type fakeTailSnap struct{ s tail.LiveSnapshot }

func (f fakeTailSnap) Snapshot() tail.LiveSnapshot { return f.s }

// newTailState 建一个 tail dashboard 载体（记录器用临时目录）。
func newTailState(t *testing.T, snap tail.LiveSnapshot, limits SourceLimits) (*TailState, *tail.Recorder, string) {
	t.Helper()
	dir := t.TempDir()
	rec, err := tail.NewRecorder(dir)
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	t.Cleanup(func() { rec.Close() })
	s := NewTailState(rec, fakeTailSnap{snap}, tail.DefaultConfig(), "paper", limits)
	return s, rec, dir
}

// getJSON 发一次 GET 并把响应解到 v。
func getJSON(t *testing.T, h func(http.ResponseWriter, *http.Request), path string, v interface{}) {
	t.Helper()
	w := httptest.NewRecorder()
	h(w, httptest.NewRequest(http.MethodGet, path, nil))
	if w.Code != http.StatusOK {
		t.Fatalf("%s 状态码 %d", path, w.Code)
	}
	if err := json.Unmarshal(w.Body.Bytes(), v); err != nil {
		t.Fatalf("解析 %s: %v（body=%s）", path, err, w.Body.String())
	}
}

// tailObs 构造一条可落盘的行（frame/snap 通用）。
func tailObs(kind string, ts int64) *tail.Observation {
	o := &tail.Observation{
		Kind: kind, FrameT: 60, Ts: ts, Rem: 55,
		YesBid: 0.90, YesAsk: 0.92, NoBid: 0.07, NoAsk: 0.09,
		Spot: 100100, Twap: 100050, Anchor: 100000, HistBps: 10,
		Side: flip.SideYes, HotAsk: 0.92, Dev: 100, Sd: 100,
		BookLatMs: 50, SpotAgeMs: 100, TwapAgeMs: 500,
	}
	if kind == tail.KindSnap {
		o.Rules = tail.Rules{Price: true, Dev63: true, Sigma: true, SigmaUSD40: true}
		o.OK, o.Shares = true, 2/0.92
	}
	return o
}

// ── /api/state ──

// TestTailStateSnapshotPassthrough 前端当前窗口区块的数据源: 热门侧读数、锚/σ、
// 两个闩锁、三源阈值全部原样透出; rem 由服务器现算（快照里不带 rem）。
func TestTailStateSnapshotPassthrough(t *testing.T) {
	stats := tail.WindowStats{Ticks: 300, TicksValid: 297, BookStale: 2, BookMissing: 1, Frames: 1}
	limits := SourceLimits{BookLatMs: 300, SpotAgeMs: 2000, TwapAgeMs: 10_000}
	now := time.Now()
	snap := tail.LiveSnapshot{
		Mode: "paper", ConditionID: "0xabc", Slug: "btc-updown-5m-1780000000",
		EventStart: now.Unix() - 100, EngineState: "Watching",
		Anchor: 100000, HistBps: 9.26, AnchorExact: true,
		AnchorSrc: "stream", AnchorArrivedMs: 2013, // src = feed.AnchorSourceStream（dashboard 不引 feed 包, 用字面量）
		YesBid: 0.90, YesAsk: 0.92, NoBid: 0.07, NoAsk: 0.09,
		BookLatMs: 52, SpotAgeMs: 120, TwapAgeMs: 800,
		SpotPrice: 100123.4, TwapPrice: 100050.1,
		HotSide: flip.SideYes, HotAsk: 0.92, Dev: 63.4, Sd: 92.6,
		FrameSent: true, SnapSent: false, AnchorFrozen: true,
		Stats: &stats,
		Risk:  &flip.RiskSummary{CanTrade: true, Enforced: false},
	}
	s, _, _ := newTailState(t, snap, limits)

	var got tailStateResponse
	getJSON(t, s.handleState, "/api/state", &got)

	if got.Mode != "paper" || got.Slug != snap.Slug || got.EngineState != "Watching" {
		t.Fatalf("头部字段 = %q/%q/%q", got.Mode, got.Slug, got.EngineState)
	}
	if got.Limits != limits {
		t.Fatalf("limits = %+v, 期望 %+v", got.Limits, limits)
	}
	if got.HotSide != flip.SideYes || got.HotAsk != 0.92 || got.Dev != 63.4 || got.Sd != 92.6 {
		t.Fatalf("热门侧读数 = %s/%.2f/dev=%.2f/sd=%.2f", got.HotSide, got.HotAsk, got.Dev, got.Sd)
	}
	if !got.FrameSent || got.SnapSent || !got.AnchorFrozen {
		t.Fatalf("闩锁 = 帧%v 快照%v 冻结%v, 期望 true/false/true", got.FrameSent, got.SnapSent, got.AnchorFrozen)
	}
	if !got.AnchorExact || got.AnchorSrc != "stream" || got.AnchorArrivedMs != 2013 {
		t.Fatalf("锚可见性 = %v/%q/%d", got.AnchorExact, got.AnchorSrc, got.AnchorArrivedMs)
	}
	if got.WindowStats == nil || got.WindowStats.TicksValid != 297 || got.WindowStats.BookStale != 2 {
		t.Fatalf("本窗健康度未透出: %+v", got.WindowStats)
	}
	if got.Risk == nil || !got.Risk.CanTrade || got.Risk.Enforced {
		t.Fatalf("风控摘要 = %+v（paper 应为 CanTrade=true / Enforced=false）", got.Risk)
	}
	if got.Live != nil {
		t.Fatalf("paper 的 live 应为 nil, 得到 %+v", got.Live)
	}
	// rem 现算: 窗口起点在 100s 前 → 200s 左右（轮询抖动容忍 ±3s）
	if got.Remaining < 197 || got.Remaining > 200 {
		t.Fatalf("remaining_sec = %d, 期望 ≈200", got.Remaining)
	}
}

// TestTailStateTodayHealth 判决的辅助闸门 3: 读当日 tailstats_*.jsonl 汇总窗数 /
// skip 分布 / anchor_exact=false 计数; 文件不存在时全 0 且不报错（当日还没跑过窗口）。
func TestTailStateTodayHealth(t *testing.T) {
	limits := SourceLimits{BookLatMs: 300}
	s, _, dir := newTailState(t, tail.LiveSnapshot{
		EventStart: time.Now().Unix() - 10, EngineState: "Watching",
	}, limits)

	// 文件不存在: 今日读数全 0, TodayStatsDay 仍给出查阅的日期（前端据此显示 —）
	var empty tailStateResponse
	getJSON(t, s.handleState, "/api/state", &empty)
	if empty.TodayRows != 0 || empty.TodayWindows != 0 || empty.TodayNoAnchor != 0 {
		t.Fatalf("无文件时应全 0, 得到 rows=%d windows=%d no_anchor=%d",
			empty.TodayRows, empty.TodayWindows, empty.TodayNoAnchor)
	}

	// 写三行: 一窗跳过（no_sigma）+ 两窗采集（其一没取到锚）+ 一行坏行（须跳过）
	today := time.Now().UTC().Format("2006-01-02")
	lines := []string{
		`{"ts":1,"date":"` + today + `","kind":"tailstats","skip":"no_sigma"}`,
		`{"ts":2,"date":"` + today + `","kind":"tailstats","anchor_exact":true,"anchor":100000,"hist_bps":9.26}`,
		`{"ts":3,"date":"` + today + `","kind":"tailstats","anchor_exact":false,"anchor":0,"hist_bps":9.26}`,
		`{"ts":4, 坏行`,
	}
	path := filepath.Join(dir, "tailstats_"+today+".jsonl")
	if err := os.WriteFile(path, []byte(strings.Join(lines, "\n")+"\n"), 0o644); err != nil {
		t.Fatalf("写 %s: %v", path, err)
	}

	var got tailStateResponse
	getJSON(t, s.handleState, "/api/state", &got)
	if got.TodayStatsDay != today {
		t.Fatalf("today_stats_day = %q, 期望 %q", got.TodayStatsDay, today)
	}
	if got.TodayRows != 3 || got.TodayWindows != 2 || got.TodayNoAnchor != 1 {
		t.Fatalf("今日健康度 = rows%d/windows%d/no_anchor%d, 期望 3/2/1",
			got.TodayRows, got.TodayWindows, got.TodayNoAnchor)
	}
	if got.TodaySkips["no_sigma"] != 1 || len(got.TodaySkips) != 1 {
		t.Fatalf("skip 分布 = %v, 期望仅 no_sigma:1", got.TodaySkips)
	}
}

// TestTailStateCounts 统计卡: 待结算 / 已结算 / 胜率 / 累计 P&L 的口径
// （待结算不进胜率分母; 帧行无仓位语义, 不计入 snap_count）。
func TestTailStateCounts(t *testing.T) {
	s, rec, _ := newTailState(t, tail.LiveSnapshot{}, SourceLimits{})

	// 两个窗口各一行帧 + 一行快照; 其一结算赢、其一待结算
	for i, ts := range []int64{1780000000000, 1780000300000} {
		cond := "0xcond" + string(rune('1'+i))
		if _, err := rec.RecordObservation(cond, "btc-updown-5m", 1780000000, tailObs(tail.KindFrame, ts), 0); err != nil {
			t.Fatalf("帧行: %v", err)
		}
		if _, err := rec.RecordObservation(cond, "btc-updown-5m", 1780000000, tailObs(tail.KindSnap, ts+1000), 2); err != nil {
			t.Fatalf("快照行: %v", err)
		}
	}
	rec.Resolve("0xcond1", 0, time.Now(), "") // outcome 0 = Up; 押 yes(UP) → 赢

	var got tailStateResponse
	getJSON(t, s.handleState, "/api/state", &got)

	if got.SnapCount != 2 {
		t.Fatalf("snap_count = %d, 期望 2（帧行不计）", got.SnapCount)
	}
	if got.SignalCount != 2 || got.WonCount != 1 || got.PendingCount != 1 || got.LostCount != 0 {
		t.Fatalf("计数 = sig%d won%d pending%d lost%d, 期望 2/1/1/0",
			got.SignalCount, got.WonCount, got.PendingCount, got.LostCount)
	}
	if got.WinRate != 1 {
		t.Fatalf("胜率 = %v, 期望 1（待结算不进分母）", got.WinRate)
	}
	if got.CumPnl <= 0 || got.DayTotal == 0 {
		t.Fatalf("累计 P&L/日数 = %.4f/%d, 期望为正且至少一日", got.CumPnl, got.DayTotal)
	}
}

// ── /api/snaps 与 /api/frames ──

// TestTailSnapsFramesKindFilterAndPaging 两张表各自只出自己 kind 的行、时间倒序、
// 默认页 50、page 越界钳到末页。
func TestTailSnapsFramesKindFilterAndPaging(t *testing.T) {
	s, rec, _ := newTailState(t, tail.LiveSnapshot{}, SourceLimits{})

	const windows = 60 // 超过默认页 50, 用于验证分页
	for i := 0; i < windows; i++ {
		ts := int64(1780000000000 + i*10000)
		cond := "0x" + string(rune('a'+i%26)) + string(rune('a'+i/26))
		if _, err := rec.RecordObservation(cond, "btc-updown-5m", 1780000000, tailObs(tail.KindFrame, ts), 0); err != nil {
			t.Fatalf("帧行: %v", err)
		}
		if _, err := rec.RecordObservation(cond, "btc-updown-5m", 1780000000, tailObs(tail.KindSnap, ts+1000), 2); err != nil {
			t.Fatalf("快照行: %v", err)
		}
	}

	var snaps listResp[tailRecordResponse]
	getJSON(t, s.handleSnaps, "/api/snaps", &snaps)
	if snaps.Total != windows || snaps.Page != 1 || snaps.Size != 50 {
		t.Fatalf("/api/snaps 信封 = total%d page%d size%d, 期望 %d/1/50",
			snaps.Total, snaps.Page, snaps.Size, windows)
	}
	if len(snaps.Items) != 50 {
		t.Fatalf("/api/snaps 首页条数 = %d, 期望 50", len(snaps.Items))
	}
	for _, it := range snaps.Items {
		if it.Kind != tail.KindSnap {
			t.Fatalf("/api/snaps 混入 kind=%q 的行", it.Kind)
		}
	}
	// 时间倒序（末笔 ts 最大）
	if snaps.Items[0].Ts <= snaps.Items[49].Ts {
		t.Fatalf("未按时间倒序: 首 %d 末 %d", snaps.Items[0].Ts, snaps.Items[49].Ts)
	}
	// 双色: OK 行带判定段, 帧行不带（判据是 kind, 不是 ok）
	if !snaps.Items[0].OK || !snaps.Items[0].RulePrice {
		t.Fatalf("快照行应带判定段: %+v", snaps.Items[0])
	}

	// 越界钳到末页（60 条 / 每页 50 → 末页第 2 页, 余 10 条）
	var last listResp[tailRecordResponse]
	getJSON(t, s.handleSnaps, "/api/snaps?page=99", &last)
	if last.Page != 2 || len(last.Items) != 10 {
		t.Fatalf("越界钳制 = page%d n%d, 期望 page2 n10", last.Page, len(last.Items))
	}

	// 帧表同构, 但帧行不带判定段
	var frames listResp[tailRecordResponse]
	getJSON(t, s.handleFrames, "/api/frames?limit=1000", &frames)
	if frames.Total != windows || len(frames.Items) != windows {
		t.Fatalf("/api/frames = total%d n%d, 期望 %d", frames.Total, len(frames.Items), windows)
	}
	for _, it := range frames.Items {
		if it.Kind != tail.KindFrame {
			t.Fatalf("/api/frames 混入 kind=%q 的行", it.Kind)
		}
		if it.OK || it.RulePrice || it.RuleDev63 || it.RuleSigma || it.RuleSigma40 {
			t.Fatalf("帧行不该带判定段: %+v", it)
		}
	}
}

// ── /api/daily ──

// TestTailDaily 逐日行的字段口径: Obs = 帧 + 快照、NotesPerDay = 当日注数、
// 合计行的 NotesPerDay = 日均注数（判决频率闸的输入形态）。
func TestTailDaily(t *testing.T) {
	s, rec, _ := newTailState(t, tail.LiveSnapshot{}, SourceLimits{})

	// 两个 UTC 日, 每日 1 帧 + 1 快照
	day1 := int64(1780000000000) // 2026-05-29 前后（只要求跨日, 具体日期无关）
	for _, base := range []int64{day1, day1 + 86400000} {
		cond := "0xc" + string(rune('a'+base%7))
		if _, err := rec.RecordObservation(cond, "btc-updown-5m", base/1000, tailObs(tail.KindFrame, base), 0); err != nil {
			t.Fatalf("帧行: %v", err)
		}
		if _, err := rec.RecordObservation(cond, "btc-updown-5m", base/1000, tailObs(tail.KindSnap, base+1000), 2); err != nil {
			t.Fatalf("快照行: %v", err)
		}
	}

	var got tailDailyResp
	getJSON(t, s.handleDaily, "/api/daily", &got)
	if len(got.Days) != 2 {
		t.Fatalf("日数 = %d, 期望 2", len(got.Days))
	}
	for _, d := range got.Days {
		if d.Obs != 2 || d.Frames != 1 || d.Snaps != 1 {
			t.Fatalf("%s: obs%d frames%d snaps%d, 期望 2/1/1", d.Date, d.Obs, d.Frames, d.Snaps)
		}
		if d.Signals != 1 || d.NotesPerDay != 1 {
			t.Fatalf("%s: signals%d notes_per_day%v, 期望 1/1", d.Date, d.Signals, d.NotesPerDay)
		}
	}
	if got.Total.Frames != 2 || got.Total.Snaps != 2 || got.Total.NotesPerDay != 1 {
		t.Fatalf("合计 = frames%d snaps%d notes/day%v, 期望 2/2/1",
			got.Total.Frames, got.Total.Snaps, got.Total.NotesPerDay)
	}
	if got.Days[0].Date >= got.Days[1].Date {
		t.Fatalf("逐日未按时间正序: %s / %s", got.Days[0].Date, got.Days[1].Date)
	}
}

// ── /api/judge ──

// TestTailJudgeInsufficient 样本不足时: 六格齐出（①~⑤ + T=150 对照）, 全部判
// 「未到判决时点」, ready=false, 口径常量随 meta 下发（前端不得自己写死 14/800/2000）。
func TestTailJudgeInsufficient(t *testing.T) {
	s, rec, _ := newTailState(t, tail.LiveSnapshot{}, SourceLimits{})

	// 一个窗口的两行（样本远不够 14 日 / 800 注）; 快照行结算 → 五格与 T=150 各 1 注
	if _, err := rec.RecordObservation("0xc1", "btc-updown-5m", 1780000000, tailObs(tail.KindFrame, 1780000000000), 0); err != nil {
		t.Fatalf("帧行: %v", err)
	}
	if _, err := rec.RecordObservation("0xc1", "btc-updown-5m", 1780000000, tailObs(tail.KindSnap, 1780000001000), 2); err != nil {
		t.Fatalf("快照行: %v", err)
	}
	rec.Resolve("0xc1", flip.OutcomeUp, time.Now(), "") // 押 yes(UP) + outcome Up → 赢

	var got judgeResp
	getJSON(t, s.handleJudge, "/api/judge", &got)

	if len(got.Grids) != 7 {
		t.Fatalf("格数 = %d, 期望 7（①~⑤ + T=150 对照 + 监听增量）", len(got.Grids))
	}
	for _, g := range got.Grids {
		if g.Ready {
			t.Fatalf("格 %s 在样本不足时不该 ready: %+v", g.Rule, g)
		}
		if g.Verdict != tail.VerdictPending {
			t.Fatalf("格 %s 判词 = %q, 期望 %q", g.Rule, g.Verdict, tail.VerdictPending)
		}
		// 四个价格腿在本行全真 → 五格与 T=150 对照格都应收进这一注
		// （T=150 格的结果借自同窗快照行, 见 judge.go 的 outcomeByCond）。
		// 监听增量格**为 0**: 本用例没落 scan 行（snap 达标时引擎本就不产它）。
		want := 1
		if g.Rule == "scan" {
			want = 0
		}
		if g.N != want {
			t.Fatalf("格 %s N = %d, 期望 %d", g.Rule, g.N, want)
		}
	}
	// ⑤ 本尊由 handler 单独摘出来（前端判决卡直取, 不按字符串找）
	if got.Ruler5.Rule != "5" || got.Ruler5.Label == "" {
		t.Fatalf("ruler5 未正确摘出: %+v", got.Ruler5)
	}
	if got.Ruler5.FreqOK || got.Ruler5.NotesPerDay != 1 {
		t.Fatalf("⑤ 频率 = %v 注/日 FreqOK=%v, 期望 1/false（远低于 90~120）",
			got.Ruler5.NotesPerDay, got.Ruler5.FreqOK)
	}
	if got.Meta.MinDays != 14 || got.Meta.MinN != 800 || got.Meta.BootB != 2000 ||
		got.Meta.BootSeed != 42 || got.Meta.FreqLo != 90 || got.Meta.FreqHi != 120 {
		t.Fatalf("判决口径常量 = %+v, 期望 14/800/2000/42/90/120", got.Meta)
	}
}

// TestTailJudgeT150NeedsSettledSnap T=150 对照格的结果只能借自**同窗已结算的快照行**:
// 快照行未结算（否决 / 没成交 / 待结算）时该窗不进对照格, 而五格本来就不吃它。
func TestTailJudgeT150NeedsSettledSnap(t *testing.T) {
	s, rec, _ := newTailState(t, tail.LiveSnapshot{}, SourceLimits{})

	if _, err := rec.RecordObservation("0xc1", "btc-updown-5m", 1780000000, tailObs(tail.KindFrame, 1780000000000), 0); err != nil {
		t.Fatalf("帧行: %v", err)
	}
	if _, err := rec.RecordObservation("0xc1", "btc-updown-5m", 1780000000, tailObs(tail.KindSnap, 1780000001000), 2); err != nil {
		t.Fatalf("快照行: %v", err)
	}
	// 不结算

	var got judgeResp
	getJSON(t, s.handleJudge, "/api/judge", &got)
	for _, g := range got.Grids {
		if g.N != 0 {
			t.Fatalf("格 %s N = %d, 期望 0（无已结算样本）", g.Rule, g.N)
		}
	}
}

// TestTailJudgeSkipsGated 被闸行（gate_reason 非空）不进任何格——映射文档 §2.2 的
// 红线在 dashboard 上也成立（否则判决卡会把「不熔断会怎样」的反事实算成策略样本）。
func TestTailJudgeSkipsGated(t *testing.T) {
	s, rec, _ := newTailState(t, tail.LiveSnapshot{}, SourceLimits{})

	if _, err := rec.RecordGatedObservation("0xc1", "btc-updown-5m", 1780000000,
		tailObs(tail.KindSnap, 1780000000000), 2, flip.GateDailyLoss); err != nil {
		t.Fatalf("被闸行: %v", err)
	}

	var got judgeResp
	getJSON(t, s.handleJudge, "/api/judge", &got)
	for _, g := range got.Grids {
		if g.N != 0 {
			t.Fatalf("格 %s 吃进了被闸行: n=%d", g.Rule, g.N)
		}
	}
	// 被闸行仍在快照表里可见（分析脚本默认过滤, 但页面要能看到它们的存在）
	var snaps listResp[tailRecordResponse]
	getJSON(t, s.handleSnaps, "/api/snaps", &snaps)
	if snaps.Total != 1 || snaps.Items[0].GateReason != flip.GateDailyLoss {
		t.Fatalf("被闸行未在快照表可见: %+v", snaps)
	}
}

// ── /api/config ──

// TestTailConfigKeys 配置口下发 7 个 tail 键（键名与 /api/config 的 flip 版同形,
// 前端据此展示标定参数; 两族键名不重叠）。
func TestTailConfigKeys(t *testing.T) {
	s, _, _ := newTailState(t, tail.LiveSnapshot{}, SourceLimits{})
	var got map[string]float64
	getJSON(t, s.handleConfig, "/api/config", &got)
	want := []string{"rem_start", "frame_rem", "price_min", "dev_min_usd", "sigma_min_usd", "stake", "max_book_lat_ms"}
	for _, k := range want {
		if _, ok := got[k]; !ok {
			t.Fatalf("/api/config 缺键 %q（得到 %v）", k, got)
		}
	}
	cfg := tail.DefaultConfig()
	if got["rem_start"] != float64(cfg.RemStart) || got["frame_rem"] != float64(cfg.FrameRem) ||
		got["price_min"] != cfg.PriceMin || got["dev_min_usd"] != cfg.DevMinUSD ||
		got["sigma_min_usd"] != cfg.SigmaMinUSD || got["stake"] != cfg.Stake {
		t.Fatalf("/api/config 取值与 DefaultConfig 不符: %v vs %+v", got, cfg)
	}
}
