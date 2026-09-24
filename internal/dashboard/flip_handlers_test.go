package dashboard

// writePage 分页测试: 时间倒序切片 / total / 越界钳制 / page/limit 参数解析。

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/necklace/flip-signal/internal/flip"
)

// recs 构造 total 条 Ts 从 1000 递增的记录（时间正序，模拟 recorder 返回序）。
func recs(total int) []*flip.Record {
	out := make([]*flip.Record, 0, total)
	for i := 0; i < total; i++ {
		out = append(out, &flip.Record{Observation: flip.Observation{Ts: int64(1000 + i)}})
	}
	return out
}

// resp 发起带 query 的请求并解析 listResp。
func resp(t *testing.T, records []*flip.Record, defSize int, query string) listResp[recordResponse] {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/api/x"+query, nil)
	w := httptest.NewRecorder()
	writePage(w, req, records, recordTs, mapRecord, defSize)
	var r listResp[recordResponse]
	if err := json.Unmarshal(w.Body.Bytes(), &r); err != nil {
		t.Fatalf("解析响应失败: %v", err)
	}
	return r
}

func tsItems(items []recordResponse) []int64 {
	out := make([]int64, 0, len(items))
	for _, it := range items {
		out = append(out, it.Ts)
	}
	return out
}

func eqTs(t *testing.T, got []int64, want ...int64) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("items ts=%v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("items ts=%v, want %v", got, want)
		}
	}
}

func TestWritePage(t *testing.T) {
	cases := []struct {
		name    string
		records int
		defSize int
		query   string
		total   int
		page    int
		size    int
		wantTs  []int64 // 期望 items 的 ts（时间倒序）
	}{
		{name: "空数据", records: 0, defSize: 50, query: "", total: 0, page: 1, size: 50},
		{name: "不足一页", records: 5, defSize: 50, query: "", total: 5, page: 1, size: 50, wantTs: []int64{1004, 1003, 1002, 1001, 1000}},
		{name: "首页", records: 5, defSize: 50, query: "?page=1&limit=2", total: 5, page: 1, size: 2, wantTs: []int64{1004, 1003}},
		{name: "中间页", records: 5, defSize: 50, query: "?page=2&limit=2", total: 5, page: 2, size: 2, wantTs: []int64{1002, 1001}},
		{name: "末页余数", records: 5, defSize: 50, query: "?page=3&limit=2", total: 5, page: 3, size: 2, wantTs: []int64{1000}},
		{name: "越界钳制到末页", records: 5, defSize: 50, query: "?page=99&limit=2", total: 5, page: 3, size: 2, wantTs: []int64{1000}},
		{name: "page 非法回落 1", records: 3, defSize: 50, query: "?page=0&limit=1", total: 3, page: 1, size: 1, wantTs: []int64{1002}},
		{name: "page 非法回落默认", records: 3, defSize: 50, query: "?page=abc&limit=1", total: 3, page: 1, size: 1, wantTs: []int64{1002}},
		{name: "limit 超上限钳 1000", records: 3, defSize: 50, query: "?limit=99999", total: 3, page: 1, size: 1000, wantTs: []int64{1002, 1001, 1000}},
		{name: "信号默认页大 200", records: 3, defSize: 200, query: "", total: 3, page: 1, size: 200, wantTs: []int64{1002, 1001, 1000}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r := resp(t, recs(c.records), c.defSize, c.query)
			if r.Total != c.total || r.Page != c.page || r.Size != c.size {
				t.Fatalf("信封 total/page/size = %d/%d/%d, want %d/%d/%d",
					r.Total, r.Page, r.Size, c.total, c.page, c.size)
			}
			eqTs(t, tsItems(r.Items), c.wantTs...)
		})
	}
}

// ── /api/state: 三源阈值下发 + 本窗 tick 健康度（2026-09-16）──

// fakeSnap 实现 flip.Snapshotter（固定快照）。
type fakeSnap struct{ s flip.LiveSnapshot }

func (f fakeSnap) Snapshot() flip.LiveSnapshot { return f.s }

// TestStateLimitsAndWindowStats 前端标红/诊断行的数据源: limits 原样下发、
// window_stats 随快照透出（含丢信号明细）。
func TestStateLimitsAndWindowStats(t *testing.T) {
	rec, err := flip.NewRecorder(t.TempDir())
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	defer rec.Close()

	stats := flip.WindowStats{
		Ticks: 300, TicksValid: 298, BookStale: 1, BookMissing: 1,
		LostTriggers: []flip.LostTrigger{{
			Ts: 1, Side: flip.SideYes, Rem: 196, Ask: 0.19,
			BookLatMs: 412, Reason: flip.LostReasonStaleBook,
		}},
	}
	limits := SourceLimits{BookLatMs: 300, SpotAgeMs: 2000, TwapAgeMs: 10_000}
	s := NewFlipState(rec, fakeSnap{flip.LiveSnapshot{
		EngineState: "Watching", BookLatMs: 412, Stats: &stats,
	}}, flip.DefaultConfig(), "paper", limits)

	w := httptest.NewRecorder()
	s.handleState(w, httptest.NewRequest(http.MethodGet, "/api/state", nil))
	var got stateResponse
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatalf("解析 /api/state: %v", err)
	}
	if got.Limits != limits {
		t.Fatalf("limits = %+v, 期望 %+v", got.Limits, limits)
	}
	if got.WindowStats == nil {
		t.Fatal("window_stats 缺失（快照有 Stats 时应透出）")
	}
	if got.WindowStats.Ticks != 300 || got.WindowStats.TicksValid != 298 ||
		got.WindowStats.BookStale != 1 || got.WindowStats.BookMissing != 1 {
		t.Fatalf("本窗计数错: %+v", got.WindowStats)
	}
	if len(got.WindowStats.LostTriggers) != 1 ||
		got.WindowStats.LostTriggers[0].BookLatMs != 412 {
		t.Fatalf("丢信号明细未透出: %+v", got.WindowStats.LostTriggers)
	}

	// 窗口间（快照无 Stats）: 字段省略，前端隐藏健康度行
	s2 := NewFlipState(rec, fakeSnap{flip.LiveSnapshot{}}, flip.DefaultConfig(), "paper", limits)
	w2 := httptest.NewRecorder()
	s2.handleState(w2, httptest.NewRequest(http.MethodGet, "/api/state", nil))
	if body := w2.Body.String(); !strings.Contains(body, "\"limits\"") ||
		strings.Contains(body, "\"window_stats\"") {
		t.Fatalf("窗口间应只下发 limits 而不含 window_stats: %s", body)
	}
}

// TestStateTwap 前端当前窗口 TWAP 展示腿（twap / twap_open）: 现值与开盘锚原样
// 下发；锚缺失（0 = 无推送/未就绪/恢复中）也原样下发——前端据此显示「—」并把
// 差值置空，不得回落成别的值。
func TestStateTwap(t *testing.T) {
	rec, err := flip.NewRecorder(t.TempDir())
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	defer rec.Close()

	limits := SourceLimits{TwapAgeMs: 10_000}
	s := NewFlipState(rec, fakeSnap{flip.LiveSnapshot{TwapPrice: 115432.10, TwapOpen: 115400.55}},
		flip.DefaultConfig(), "paper", limits)
	w := httptest.NewRecorder()
	s.handleState(w, httptest.NewRequest(http.MethodGet, "/api/state", nil))
	var got stateResponse
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatalf("解析 /api/state: %v", err)
	}
	if got.TwapPrice != 115432.10 || got.TwapOpen != 115400.55 {
		t.Fatalf("twap 腿 = %.2f/%.2f, want 115432.10/115400.55", got.TwapPrice, got.TwapOpen)
	}

	// 锚缺失窗口（本窗锚未就绪/恢复中）: twap_open=0 原样下发
	s2 := NewFlipState(rec, fakeSnap{flip.LiveSnapshot{TwapPrice: 115432.10}},
		flip.DefaultConfig(), "paper", limits)
	w2 := httptest.NewRecorder()
	s2.handleState(w2, httptest.NewRequest(http.MethodGet, "/api/state", nil))
	var got2 stateResponse
	if err := json.Unmarshal(w2.Body.Bytes(), &got2); err != nil {
		t.Fatalf("解析 /api/state: %v", err)
	}
	if got2.TwapOpen != 0 || got2.TwapPrice != 115432.10 {
		t.Fatalf("锚缺失时 twap 腿 = %.2f/%.2f, want 115432.10/0", got2.TwapPrice, got2.TwapOpen)
	}
}

// ── 无仓位行的分类与可见性（2026-09-24）──

// flipObs 构造一条 ok 触底观测（量级取实测; 本文件只关心分类, 数值不参与断言）。
func flipObs(ts int64, side string, shares float64) *flip.Observation {
	sgn := 1.0
	if side == "no" {
		sgn = -1.0
	}
	const anchor, spot, twap = 83760.41, 83856.25, 83720.00
	return &flip.Observation{
		Ts: ts, Side: side, Rem: 261, Fill: 0.19,
		M20: 0.46, M30: 0.48, M45: 0.50, DistS: -0.97, DistT: -0.23,
		OK: true, Shares: shares, Anchor: anchor, HistBps: 11.75, Spot: spot,
		TwapPrice: twap, // 美元位移按 sgn 口径现算（与引擎同公式）
		DevUSD:     sgn * (spot - anchor),
		TwapDevUSD: sgn * (twap - anchor),
	}
}

// TestFlipNoExecClassification 无仓位行不得被反算成「输」、也不得永远挂在「待结算」。
//
// 起因（用户反馈的线上现象）: live 每次重启的首窗都产生一条 rejected 行（信号成立但
// 风控闸拦下、无仓位）, 它在页面上与「在途待结算」长得一样——结果列永远停在待结算,
// 而 ① 它永远不会被结算, ② 旧的 `lost = sigCount − won − pending` 反算把它计成「负」,
// 既压低胜率又虚增亏损笔数。本测试钉住三件事:
//   - 分类恒等式 signal = won + lost + pending + noexec（每行只落一类）;
//   - paper 方案 A 的被闸行照常结算, 结算后计入赢/输（不因被闸改口径）;
//   - gate_reason/exec_status/exec_note 三个键必须下发（前端结果列全靠它们判态）。
func TestFlipNoExecClassification(t *testing.T) {
	rec, err := flip.NewRecorder(t.TempDir())
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	defer rec.Close()

	const ts = int64(1780000000000) // 固定时间戳: 全部行落同一个 UTC 日
	const slug = "btc-updown-5m"
	const start = int64(1780000000)

	// ① live 首窗禁单（rejected 行, 无仓位）—— 用户日志里那一条
	if _, err := rec.RecordLiveRejected("0xgate1", slug, start, flipObs(ts, flip.SideNo, 10.53),
		2, flip.GateFirstWindow, "重启后首窗禁单"); err != nil {
		t.Fatalf("rejected 行: %v", err)
	}
	// ② live 0 成交（submitting → unfilled）
	if _, err := rec.SubmitLiveObservation("0xunf1", slug, start, flipObs(ts+1000, flip.SideNo, 10.53), 2); err != nil {
		t.Fatalf("submitting 行: %v", err)
	}
	if _, _, err := rec.CompleteExecution("0xunf1", flip.ExecResult{
		Status: flip.ExecStatusUnfilled, OrderID: "0xorder1", FillPrice: 0.19,
	}); err != nil {
		t.Fatalf("unfilled 回填: %v", err)
	}
	// ③ live GTC 挂单未定稿（resting, 仓位未定 ⇒ 不入 pending）
	if _, err := rec.SubmitLiveObservation("0xrest1", slug, start, flipObs(ts+2000, flip.SideNo, 10.53), 2); err != nil {
		t.Fatalf("submitting 行: %v", err)
	}
	if _, _, err := rec.CompleteExecution("0xrest1", flip.ExecResult{
		Status: flip.ExecStatusResting, OrderID: "0xorder2", FillPrice: 0.19,
	}); err != nil {
		t.Fatalf("resting 回填: %v", err)
	}
	// ④ paper 被闸（方案 A: 只标记, 行照常结算）→ 结算输
	if _, err := rec.RecordGatedObservation("0xgate2", slug, start, flipObs(ts+3000, flip.SideNo, 10.53),
		2, flip.GateDailyLoss); err != nil {
		t.Fatalf("paper 被闸行: %v", err)
	}
	if !rec.Resolve("0xgate2", 0, time.Now(), "push") { // outcome 0 = Up, 押 no → 输
		t.Fatal("paper 被闸行未进 pending（方案 A 必须照常结算）")
	}
	// ⑤ paper 正常行（有仓位在途）
	if _, err := rec.RecordObservation("0xok1", slug, start, flipObs(ts+4000, flip.SideNo, 10.53), 2); err != nil {
		t.Fatalf("paper 行: %v", err)
	}
	// ⑥ paper 正常行 → 结算赢
	if _, err := rec.RecordObservation("0xok2", slug, start, flipObs(ts+5000, flip.SideNo, 10.53), 2); err != nil {
		t.Fatalf("paper 行: %v", err)
	}
	if !rec.Resolve("0xok2", 1, time.Now(), "push") { // outcome 1 = Down, 押 no → 赢
		t.Fatal("paper 行未进 pending")
	}

	// 1) 分类计数（live 模式 + 一条 paper 行混排, 与现网重启后的数据同形）
	s := NewFlipState(rec, fakeSnap{flip.LiveSnapshot{}}, flip.DefaultConfig(), "live", SourceLimits{})
	var got stateResponse
	getJSON(t, s.handleState, "/api/state", &got)
	if got.SignalCount != 6 || got.WonCount != 1 || got.LostCount != 1 ||
		got.PendingCount != 1 || got.NoExecCount != 3 {
		t.Fatalf("分类 = 总%d 赢%d 输%d 待%d 未成交%d, 期望 6/1/1/1/3",
			got.SignalCount, got.WonCount, got.LostCount, got.PendingCount, got.NoExecCount)
	}
	if got.SignalCount != got.WonCount+got.LostCount+got.PendingCount+got.NoExecCount {
		t.Fatalf("分类恒等式不成立: 总%d ≠ 赢%d+输%d+待%d+未成交%d", got.SignalCount,
			got.WonCount, got.LostCount, got.PendingCount, got.NoExecCount)
	}
	if got.WinRate != 0.5 { // 胜率分母 = 赢+输; 待结算与无仓位都不进
		t.Fatalf("胜率 = %v, 期望 0.5（未成交不计入分母）", got.WinRate)
	}

	// 2) 逐日行同口径（无仓位行不再永远挂在「待结算」）
	var daily dailyResp
	getJSON(t, s.handleDaily, "/api/daily", &daily)
	if len(daily.Days) != 1 {
		t.Fatalf("日数 = %d, 期望 1（全部行同一 UTC 日）", len(daily.Days))
	}
	d := daily.Days[0]
	if d.Signals != 6 || d.Won != 1 || d.Lost != 1 || d.Pending != 1 || d.NoExec != 3 {
		t.Fatalf("逐日分类 = 信号%d 赢%d 输%d 待%d 未成交%d, 期望 6/1/1/1/3",
			d.Signals, d.Won, d.Lost, d.Pending, d.NoExec)
	}
	if daily.Total.NoExec != 3 || daily.Total.Signals != 6 || daily.Total.Pending != 1 {
		t.Fatalf("合计行 = %+v, 期望 未成交3/信号6/待1", daily.Total)
	}

	// 3) 前端结果列依赖的三个键必须下发（逐行按 condition_id 核对）
	var sigs listResp[recordResponse]
	getJSON(t, s.handleSignals, "/api/signals", &sigs)
	byID := map[string]recordResponse{}
	for _, it := range sigs.Items {
		byID[it.ConditionID] = it
	}
	if r := byID["0xgate1"]; r.GateReason != flip.GateFirstWindow ||
		r.ExecStatus != flip.ExecStatusRejected || r.ExecNote == "" {
		t.Fatalf("rejected 行缺可见性字段: %+v", r)
	}
	if r := byID["0xunf1"]; r.ExecStatus != flip.ExecStatusUnfilled || r.GateReason != "" {
		t.Fatalf("unfilled 行 exec_status 未透传: %+v", r)
	}
	if r := byID["0xrest1"]; r.ExecStatus != flip.ExecStatusResting {
		t.Fatalf("resting 行 exec_status 未透传: %+v", r)
	}
	if r := byID["0xgate2"]; r.GateReason != flip.GateDailyLoss || r.Won == nil || *r.Won {
		t.Fatalf("paper 被闸行应照常结算为「输」且保留 gate_reason: %+v", r)
	}
	if r := byID["0xok1"]; r.GateReason != "" || r.ExecStatus != "" || r.Won != nil {
		t.Fatalf("paper 正常行不得被标闸/标状态: %+v", r)
	}

	// 4) 美元位移两个键必须下发（2026-09-25 追加）: 前端 dev$/twap$ 两列的数据源。
	// 断言非零即可（fixture 的 anchor/spot/twap 三者互不相等），不复制引擎公式。
	if r := byID["0xok1"]; r.DevUSD == 0 || r.TwapDevUSD == 0 {
		t.Fatalf("美元位移未透传: %+v", r)
	}
}
