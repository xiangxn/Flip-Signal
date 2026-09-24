package dashboard

// tail dashboard 的 handler 测试: /api/state 透传（含今日健康度磁盘读口与信号分类
// 恒等式）、决策表/信号表的分页与过滤、逐日列口径、/api/config 键名。
//
// 2026-09-24: 判决机器（/api/judge + 五格 + bootstrap + MT19937）随 a.md 第 4 条整体
// 下线——相关用例一并删除, 纸面判决改由离线脚本 python/v4/23_tail_integrated.py 做。

import (
	"encoding/json"
	"math"
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
	if err := json.Unmarshal(w.Body.Bytes(), &v); err != nil {
		t.Fatalf("解析 %s: %v（body=%s）", path, err, w.Body.String())
	}
}

// tailSignal 构造一条**信号行**（ok=true: 三段链任一段达标、已进执行路径）。
func tailSignal(stage string, ts int64) *tail.Observation {
	return &tail.Observation{
		Kind: tail.KindSnap, Stage: stage, FrameT: 60, Ts: ts, Rem: 55,
		YesBid: 0.90, YesAsk: 0.92, NoBid: 0.07, NoAsk: 0.09,
		Spot: 100100, Twap: 100050, Anchor: 100000, HistBps: 10,
		Side: flip.SideYes, HotAsk: 0.92, HotSrc: tail.BookSrcAsk, Dev: 100, Sd: 100,
		BookLatMs: 50, SpotAgeMs: 100, TwapAgeMs: 500,
		Rules: tail.Rules{Price: true, Dev63: true, Sigma: true, SigmaUSD40: true},
		OK:    true, Shares: 2 / 0.92,
	}
}

// tailDecision 构造一条**判定行**（ok=false: t150/t60 段判了 ⑤ 但没达标, 只落盘）。
func tailDecision(stage string, ts int64) *tail.Observation {
	o := tailSignal(stage, ts)
	o.Rem, o.OK, o.Shares = 145, false, 0
	o.Rules = tail.Rules{Price: true} // 价格腿过、位移腿没过
	o.RejectReason = tail.RejectLegOut
	return o
}

// ── /api/state ──

// TestTailStateSnapshotPassthrough 前端当前窗口区块的数据源: 热门侧读数（含有效价
// 来源 hot_src）、锚/σ、三段链的五个闩锁、三源阈值全部原样透出; rem 由服务器现算
// （快照里不带 rem）。
func TestTailStateSnapshotPassthrough(t *testing.T) {
	stats := tail.WindowStats{Ticks: 300, TicksValid: 297, BookStale: 2, BookMissing: 1, Rows: 1}
	limits := SourceLimits{BookLatMs: 300, SpotAgeMs: 2000, TwapAgeMs: 10_000}
	now := time.Now()
	snap := tail.LiveSnapshot{
		Mode: "paper", ConditionID: "0xabc", Slug: "btc-updown-5m-1780000000",
		EventStart: now.Unix() - 100, EngineState: "Listening",
		Anchor: 100000, HistBps: 9.26, AnchorExact: true,
		AnchorSrc: "stream", AnchorArrivedMs: 2013, // src = feed.AnchorSourceStream（dashboard 不引 feed 包, 用字面量）
		YesBid: 0.90, YesAsk: 0.92, NoBid: 0.07, NoAsk: 0.09,
		BookLatMs: 52, SpotAgeMs: 120, TwapAgeMs: 800,
		SpotPrice: 100123.4, TwapPrice: 100050.1,
		HotSide: flip.SideYes, HotAsk: 0.92, HotSrc: tail.BookSrcBid, Dev: 63.4, Sd: 92.6,
		T150Sent: true, T60Sent: true, Listening: true, SignalSent: false, AnchorFrozen: true,
		Stats: &stats,
		Risk:  &flip.RiskSummary{CanTrade: true, Enforced: false},
	}
	s, _, _ := newTailState(t, snap, limits)

	var got tailStateResponse
	getJSON(t, s.handleState, "/api/state", &got)

	if got.Mode != "paper" || got.Slug != snap.Slug || got.EngineState != "Listening" {
		t.Fatalf("头部字段 = %q/%q/%q", got.Mode, got.Slug, got.EngineState)
	}
	if got.Limits != limits {
		t.Fatalf("limits = %+v, 期望 %+v", got.Limits, limits)
	}
	if got.HotSide != flip.SideYes || got.HotAsk != 0.92 || got.Dev != 63.4 || got.Sd != 92.6 {
		t.Fatalf("热门侧读数 = %s/%.2f/dev=%.2f/sd=%.2f", got.HotSide, got.HotAsk, got.Dev, got.Sd)
	}
	// 有效价来源必须透出（bid = 该侧卖单被撤空、只能按买价挂单——a.md 第 2 条的可见性）
	if got.HotSrc != tail.BookSrcBid {
		t.Fatalf("hot_src = %q, 期望 %q", got.HotSrc, tail.BookSrcBid)
	}
	// 三段链闩锁: t60 蕴含 t150; 监听中但尚未出信号; 锚已冻结（本窗已产行）
	if !got.T150Sent || !got.T60Sent || !got.Listening || got.SignalSent || !got.AnchorFrozen {
		t.Fatalf("闩锁 = t150%v t60%v listen%v signal%v frozen%v, 期望 true/true/true/false/true",
			got.T150Sent, got.T60Sent, got.Listening, got.SignalSent, got.AnchorFrozen)
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

// TestTailStateTodayHealth 辅助闸门 3: 读当日 tailstats_*.jsonl 汇总窗数 / skip 分布 /
// anchor_exact=false 计数; 文件不存在时全 0 且不报错（当日还没跑过窗口）。
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

// TestTailStateTally 信号分类恒等式: signal_count = won + lost + pending + noexec,
// 且**胜率分母只含 won+lost**（a.md 第 4 条: 未成交信号不进入胜率计算）。
//
// 四类各造一行: 赢（paper 成交 + 结算）/ 输（同）/ 待结算（成交、未回填）/
// 未成交（被风控闸拦 = rejected 行, 无仓位）——判定行（ok=false）不进任何一类。
func TestTailStateTally(t *testing.T) {
	s, rec, _ := newTailState(t, tail.LiveSnapshot{}, SourceLimits{})
	base := int64(1780000000000)

	// 判定行（ok=false）: 进 decision_count, 不进信号分类。
	if _, err := rec.RecordObservation("0xdec", "btc-updown-5m", 1780000000, tailDecision(tail.StageT150, base), 2); err != nil {
		t.Fatalf("判定行: %v", err)
	}
	// 赢
	if _, err := rec.RecordObservation("0xwin", "btc-updown-5m", 1780000000, tailSignal(tail.StageT150, base+1000), 2); err != nil {
		t.Fatalf("win 行: %v", err)
	}
	rec.Resolve("0xwin", flip.OutcomeUp, time.Now(), "") // 押 yes(UP) + outcome Up → 赢
	// 输
	lose := tailSignal(tail.StageT60, base+2000)
	lose.Side, lose.HotAsk = flip.SideNo, 0.91
	if _, err := rec.RecordObservation("0xlose", "btc-updown-5m", 1780000000, lose, 2); err != nil {
		t.Fatalf("lose 行: %v", err)
	}
	rec.Resolve("0xlose", flip.OutcomeUp, time.Now(), "") // 押 no(DOWN) + outcome Up → 输
	// 待结算（有仓位、未回填）
	if _, err := rec.RecordObservation("0xpend", "btc-updown-5m", 1780000000, tailSignal(tail.StageListen, base+3000), 2); err != nil {
		t.Fatalf("pending 行: %v", err)
	}
	// 未成交（被风控闸拦 → rejected, 无仓位; 仍注册结算以便显示官方结果）
	gated := tailSignal(tail.StageListen, base+4000)
	gatedRec, err := rec.RecordRejected("0xgate", "btc-updown-5m", 1780000000, gated, 2, tail.GateDailyLoss, "日亏熔断")
	if err != nil {
		t.Fatalf("被闸行: %v", err)
	}
	rec.Resolve("0xgate", flip.OutcomeUp, time.Now(), "")

	var got tailStateResponse
	getJSON(t, s.handleState, "/api/state", &got)

	if got.SignalCount != 4 {
		t.Fatalf("signal_count = %d, 期望 4（判定行不算信号）", got.SignalCount)
	}
	if got.DecisionCount != 1 {
		t.Fatalf("decision_count = %d, 期望 1", got.DecisionCount)
	}
	if got.WonCount != 1 || got.LostCount != 1 || got.PendingCount != 1 || got.NoExecCount != 1 {
		t.Fatalf("分类 = won%d lost%d pending%d noexec%d, 期望 1/1/1/1",
			got.WonCount, got.LostCount, got.PendingCount, got.NoExecCount)
	}
	if n := got.WonCount + got.LostCount + got.PendingCount + got.NoExecCount; n != got.SignalCount {
		t.Fatalf("恒等式被破坏: %d ≠ signal_count %d", n, got.SignalCount)
	}
	// 胜率 = 1/(1+1) — 待结算与未成交都不进分母（若把未成交算进分母会是 1/3）。
	if got.WinRate != 0.5 {
		t.Fatalf("win_rate = %v, 期望 0.5（分母只含赢+输）", got.WinRate)
	}
	// 累计 P&L 只由**有仓位**的两行构成: 赢 2/0.92−2 = +0.1739, 输 −2 ⇒ −1.8261。
	// 被闸行（无仓位）照显结果但 P&L 恒 0, 待结算行未回填——两者都不进这里。
	if want := 2/0.92 - 2 - 2; math.Abs(got.CumPnl-want) > 1e-6 {
		t.Fatalf("累计 P&L = %.4f, 期望 %.4f（未成交行 P&L 恒 0）", got.CumPnl, want)
	}
	if got.DayTotal != 1 || got.DayPnlPos != 0 {
		t.Fatalf("逐日盈亏天数 = %d（正 %d）, 期望 1/0（当日净亏）", got.DayTotal, got.DayPnlPos)
	}
	// 被闸行进锁存（当日不再复牌）——dashboard 的统计口径不改变风控事实。
	// ⚠️ 锁存按**行的 UTC 日**（磁盘真相）, 与进程当前时刻无关。
	if gatedRec.GateReason != tail.GateDailyLoss || gatedRec.HasPosition() {
		t.Fatalf("被闸行应带 gate_reason 且无仓位: %+v", gatedRec)
	}
	if !rec.GatedOn(gatedRec.Date, tail.GateDailyLoss) {
		t.Fatalf("被闸行应落 gate_reason 供当日（%s）锁存", gatedRec.Date)
	}
}

// ── /api/snaps 与 /api/signals ──

// TestTailSnapsAndSignalsPaging 两张表: 决策表出全部 snap 行（判定 + 信号）, 信号表
// **只出 ok=true** 的行; 两者都时间倒序、默认页 50、page 越界钳到末页。
func TestTailSnapsAndSignalsPaging(t *testing.T) {
	s, rec, _ := newTailState(t, tail.LiveSnapshot{}, SourceLimits{})

	const windows = 60 // 超过默认页 50, 用于验证分页
	for i := 0; i < windows; i++ {
		ts := int64(1780000000000 + i*10000)
		cond := "0x" + string(rune('a'+i%26)) + string(rune('a'+i/26))
		// 每窗一条判定行（t150 未达标）+ 一条信号行（t60 达标）
		if _, err := rec.RecordObservation(cond, "btc-updown-5m", 1780000000, tailDecision(tail.StageT150, ts), 2); err != nil {
			t.Fatalf("判定行: %v", err)
		}
		if _, err := rec.RecordObservation(cond, "btc-updown-5m", 1780000000, tailSignal(tail.StageT60, ts+1000), 2); err != nil {
			t.Fatalf("信号行: %v", err)
		}
	}

	var snaps listResp[tailRecordResponse]
	getJSON(t, s.handleSnaps, "/api/snaps", &snaps)
	if snaps.Total != windows*2 || snaps.Page != 1 || snaps.Size != 50 {
		t.Fatalf("/api/snaps 信封 = total%d page%d size%d, 期望 %d/1/50",
			snaps.Total, snaps.Page, snaps.Size, windows*2)
	}
	if len(snaps.Items) != 50 {
		t.Fatalf("/api/snaps 首页条数 = %d, 期望 50", len(snaps.Items))
	}
	// 时间倒序（末笔 ts 最大）
	if snaps.Items[0].Ts <= snaps.Items[49].Ts {
		t.Fatalf("未按时间倒序: 首 %d 末 %d", snaps.Items[0].Ts, snaps.Items[49].Ts)
	}
	// 决策表**两类行都在**（判定行 ok=false 带 reject_reason、信号行 ok=true 带判定段）,
	// 各占一半 —— 这正是「决策表」与「信号表」的区别。
	if !snaps.Items[0].OK || !snaps.Items[0].RulePrice || snaps.Items[0].RejectReason != "" {
		t.Fatalf("信号行应带判定段且无 reject_reason: %+v", snaps.Items[0])
	}
	if snaps.Items[1].OK || snaps.Items[1].RejectReason == "" {
		t.Fatalf("判定行应 ok=false 且带 reject_reason: %+v", snaps.Items[1])
	}
	var full listResp[tailRecordResponse]
	getJSON(t, s.handleSnaps, "/api/snaps?limit=1000", &full)
	okN := 0
	for _, it := range full.Items {
		if it.OK {
			okN++
		}
	}
	if okN != windows {
		t.Fatalf("决策表里信号行 = %d, 期望 %d（另 %d 条为判定行）", okN, windows, windows)
	}

	// 越界钳到末页（120 条 / 每页 50 → 末页第 3 页, 余 20 条）
	var last listResp[tailRecordResponse]
	getJSON(t, s.handleSnaps, "/api/snaps?page=99", &last)
	if last.Page != 3 || len(last.Items) != 20 {
		t.Fatalf("越界钳制 = page%d n%d, 期望 page3 n20", last.Page, len(last.Items))
	}

	// 信号表: 只出 ok=true 的行（判定行被过滤掉）, 总数 = 窗数
	var sigs listResp[tailRecordResponse]
	getJSON(t, s.handleSignals, "/api/signals?limit=1000", &sigs)
	if sigs.Total != windows || len(sigs.Items) != windows {
		t.Fatalf("/api/signals = total%d n%d, 期望 %d", sigs.Total, len(sigs.Items), windows)
	}
	for _, it := range sigs.Items {
		if !it.OK || it.RejectReason != "" {
			t.Fatalf("/api/signals 混入非信号行: %+v", it)
		}
		if !it.HasPosition {
			t.Fatalf("paper 信号行应有仓位语义（P&L 参与结算）: %+v", it)
		}
	}
}

// ── /api/daily ──

// TestTailDaily 逐日行的列口径: Obs = 全部行（判定 + 信号）, Decisions = 判定行,
// 三个段列 = 信号按 stage 分桶, NoExec = 未成交信号; 恒等式
// Signals = Won + Lost + Pending + NoExec; 合计行的胜率与 P&L 由共用骨架累加。
func TestTailDaily(t *testing.T) {
	s, rec, _ := newTailState(t, tail.LiveSnapshot{}, SourceLimits{})

	// 两个 UTC 日, 每日 1 判定行 + 3 信号行（t150/t60/listen 各一）
	day1 := int64(1780000000000) // 2026-05-29 前后（只要求跨日, 具体日期无关）
	for _, base := range []int64{day1, day1 + 86400000} {
		cond := "0xc" + string(rune('a'+base%7))
		if _, err := rec.RecordObservation(cond, "btc-updown-5m", base/1000, tailDecision(tail.StageT150, base), 2); err != nil {
			t.Fatalf("判定行: %v", err)
		}
		for j, stage := range []string{tail.StageT150, tail.StageT60, tail.StageListen} {
			if _, err := rec.RecordObservation(cond, "btc-updown-5m", base/1000, tailSignal(stage, base+int64(1000*(j+1))), 2); err != nil {
				t.Fatalf("信号行: %v", err)
			}
		}
	}
	// 逐日表口径: 全部 3 条信号都有仓位（paper 模拟成交）⇒ 未成交列为 0、全部待结算。

	var got tailDailyResp
	getJSON(t, s.handleDaily, "/api/daily", &got)
	if len(got.Days) != 2 {
		t.Fatalf("日数 = %d, 期望 2", len(got.Days))
	}
	for _, d := range got.Days {
		if d.Obs != 4 || d.Decisions != 1 {
			t.Fatalf("%s: obs%d decisions%d, 期望 4/1", d.Date, d.Obs, d.Decisions)
		}
		if d.Signals != 3 || d.T150 != 1 || d.T60 != 1 || d.Listen != 1 {
			t.Fatalf("%s: signals%d t150%d t60%d listen%d, 期望 3/1/1/1",
				d.Date, d.Signals, d.T150, d.T60, d.Listen)
		}
		if d.NoExec != 0 || d.Pending != 3 || d.Won+d.Lost != 0 {
			t.Fatalf("%s: noexec%d pending%d won%d lost%d, 期望 0/3/0/0",
				d.Date, d.NoExec, d.Pending, d.Won, d.Lost)
		}
		if n := d.Won + d.Lost + d.Pending + d.NoExec; n != d.Signals {
			t.Fatalf("%s: 恒等式被破坏 %d ≠ signals %d", d.Date, n, d.Signals)
		}
	}
	if got.Total.Decisions != 2 || got.Total.Signals != 6 || got.Total.T150 != 2 ||
		got.Total.T60 != 2 || got.Total.Listen != 2 || got.Total.Pending != 6 {
		t.Fatalf("合计 = %+v, 期望 decisions2 signals6 t150/t60/listen 各 2 pending6", got.Total)
	}
	if got.Days[0].Date >= got.Days[1].Date {
		t.Fatalf("逐日未按时间正序: %s / %s", got.Days[0].Date, got.Days[1].Date)
	}
}

// ── /api/config ──

// TestTailConfigKeys 配置口下发 7 个 tail 键（键名 = mapstructure tag, 与
// v4.config.yaml 的 tail 节逐字相同; 两族键名不重叠）。
func TestTailConfigKeys(t *testing.T) {
	s, _, _ := newTailState(t, tail.LiveSnapshot{}, SourceLimits{})
	var got map[string]float64
	getJSON(t, s.handleConfig, "/api/config", &got)
	want := []string{"t150_rem", "t60_rem", "price_min", "dev_min_usd", "sigma_min_usd", "stake", "max_book_lat_ms"}
	for _, k := range want {
		if _, ok := got[k]; !ok {
			t.Fatalf("/api/config 缺键 %q（得到 %v）", k, got)
		}
	}
	cfg := tail.DefaultConfig()
	if got["t150_rem"] != float64(cfg.T150Rem) || got["t60_rem"] != float64(cfg.T60Rem) ||
		got["price_min"] != cfg.PriceMin || got["dev_min_usd"] != cfg.DevMinUSD ||
		got["sigma_min_usd"] != cfg.SigmaMinUSD || got["stake"] != cfg.Stake {
		t.Fatalf("/api/config 取值与 DefaultConfig 不符: %v vs %+v", got, cfg)
	}
}
