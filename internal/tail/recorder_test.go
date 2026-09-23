package tail

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/necklace/flip-signal/internal/flip"
)

// okSnap 构造一条 ⑤ 成立、可直接下单的决策快照（hot_ask 0.92, dev +100）。
func okSnap(ts int64, side string) *Observation {
	o := &Observation{
		Kind: KindSnap, FrameT: 60, Ts: ts, Rem: 55,
		YesBid: 0.90, YesAsk: 0.92, NoBid: 0.07, NoAsk: 0.09,
		Spot: 100100, Twap: 100050, Anchor: 100000, HistBps: 10,
		Side: side, HotAsk: 0.92, Dev: 100, Sd: 100,
		BookLatMs: 50, SpotAgeMs: 100, TwapAgeMs: 500,
		Rules: Rules{Price: true, Dev63: true, Sigma: true, SigmaUSD40: true},
		OK:    true, Shares: 2 / 0.92,
	}
	return o
}

func newTestRecorder(t *testing.T) (*Recorder, string) {
	t.Helper()
	dir := t.TempDir()
	r, err := NewRecorder(dir)
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	t.Cleanup(func() { r.Close() })
	return r, dir
}

func readLines(t *testing.T, path string) []string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读 %s: %v", path, err)
	}
	var out []string
	for _, l := range strings.Split(strings.TrimRight(string(b), "\n"), "\n") {
		if l != "" {
			out = append(out, l)
		}
	}
	return out
}

// TestRecorderFrameAndSnapFiles 钉住两件形制: ① 每窗两行落同一个 tail_ 文件且
// kind 各异; ② 帧行不进 pending、不参与结算（stake 键都不该出现）。
func TestRecorderFrameAndSnapFiles(t *testing.T) {
	r, dir := newTestRecorder(t)
	conds := "0xcond1"
	date := utcDate(1780000000000)
	path := filepath.Join(dir, "tail_"+date+".jsonl")

	// 帧行（rem≤150, 只记录）。
	frame := okSnap(1780000000000, flip.SideYes)
	frame.Kind, frame.FrameT, frame.Rem = KindFrame, 150, 145
	frame.Rules, frame.OK, frame.Shares = Rules{}, false, 0
	if _, err := r.RecordObservation(conds, "btc-updown-5m-1780000000", 1780000000, frame, 0); err != nil {
		t.Fatalf("帧行落盘: %v", err)
	}
	// 快照行（rem≤60, 判定 + 成交）。
	if _, err := r.RecordObservation(conds, "btc-updown-5m-1780000000", 1780000000, okSnap(1780000255000, flip.SideYes), 2); err != nil {
		t.Fatalf("快照行落盘: %v", err)
	}

	lines := readLines(t, path)
	if len(lines) != 2 {
		t.Fatalf("一个窗口应两行, 得到 %d 行", len(lines))
	}
	var f, s Record
	json.Unmarshal([]byte(lines[0]), &f)
	json.Unmarshal([]byte(lines[1]), &s)
	if f.Kind != KindFrame || s.Kind != KindSnap {
		t.Fatalf("行序应为 frame→snap, 得到 %s→%s", f.Kind, s.Kind)
	}
	if f.Stake != 0 || strings.Contains(lines[0], `"stake"`) {
		t.Fatalf("帧行不该有 stake（无仓位语义）: %s", lines[0])
	}
	if f.EventType != "tail" {
		t.Fatalf("event_type 应为 tail, 得到 %q", f.EventType)
	}
	if s.Stake != 2 {
		t.Fatalf("快照行 stake 应为 2, 得到 %.2f", s.Stake)
	}
	// 只有快照行进 pending（帧行 OK=false）。
	if got := len(r.PendingSignals()); got != 1 {
		t.Fatalf("帧行不得入 pending, 应只有 1 条快照, 得到 %d", got)
	}
}

// TestRecorderResolvePnl 结算 P&L 三态: 赢 shares−cost / 输 −cost / paper 无 Cost
// 时以 Stake 兜底（与回测 `shares−stake` 恒等）。
func TestRecorderResolvePnl(t *testing.T) {
	cases := []struct {
		name string
		side string
		out  int
		won  bool
		pnl  float64
	}{
		{"yes 侧官方 Up → 赢", flip.SideYes, flip.OutcomeUp, true, 2/0.92 - 2},
		{"yes 侧官方 Down → 输", flip.SideYes, flip.OutcomeDown, false, -2},
		{"no 侧官方 Down → 赢", flip.SideNo, flip.OutcomeDown, true, 2/0.92 - 2},
		{"no 侧官方 Up → 输", flip.SideNo, flip.OutcomeUp, false, -2},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r, dir := newTestRecorder(t)
			o := okSnap(1780000255000, c.side)
			if _, err := r.RecordObservation("0xc", "slug", 1780000000, o, 2); err != nil {
				t.Fatal(err)
			}
			at := time.Unix(1780000300, 0)
			if !r.Resolve("0xc", c.out, at, "") {
				t.Fatal("Resolve 应命中 pending")
			}
			if r.Resolve("0xc", c.out, at, "") {
				t.Fatal("重复结算应返回 false（已出 pending）")
			}
			// 磁盘真相: 原子重写后该行带 won/pnl/resolved_at。
			lines := readLines(t, filepath.Join(dir, "tail_"+utcDate(1780000255000)+".jsonl"))
			if len(lines) != 1 {
				t.Fatalf("重写后应仍是 1 行, 得到 %d", len(lines))
			}
			var rec Record
			json.Unmarshal([]byte(lines[0]), &rec)
			if rec.Won == nil || *rec.Won != c.won {
				t.Fatalf("won 应为 %v, 得到 %v", c.won, rec.Won)
			}
			if !near(rec.PnL, c.pnl) {
				t.Fatalf("pnl 应为 %.6f, 得到 %.6f", c.pnl, rec.PnL)
			}
			if rec.ResolvedAt == "" {
				t.Fatal("resolved_at 应回填")
			}
		})
	}
}

// TestRecorderSettleSrcPersisted 三层结算来源必须逐行落到 JSONL 的 settle_src 字段
// （internal/settle 的 push / official / gamma, 2026-09-24 引入, 与 flip 同口径）,
// 且重启载入后仍在; 未结算行不该出现这个键（omitempty）。
func TestRecorderSettleSrcPersisted(t *testing.T) {
	r, dir := newTestRecorder(t)
	const start int64 = 1780000000
	at := time.Unix(start+300, 0)

	cases := []struct{ cond, src string }{
		{"0xpush", "push"},
		{"0xofficial", "official"},
		{"0xgamma", "gamma"},
	}
	for i, c := range cases {
		win := start + int64(i)*300 // 三个不同窗口（pending 以 conditionID 为键）
		if _, err := r.RecordObservation(c.cond, "slug", win, okSnap(win*1000+255000, flip.SideYes), 2); err != nil {
			t.Fatal(err)
		}
		if !r.Resolve(c.cond, flip.OutcomeUp, at, c.src) {
			t.Fatalf("%s 应命中 pending", c.cond)
		}
	}
	// 一条未结算的帧行——omitempty 的对照
	frame := okSnap(start*1000+145000, flip.SideYes)
	frame.Kind, frame.Rem = KindFrame, 145
	if _, err := r.RecordObservation("0xframe", "slug", start, frame, 0); err != nil {
		t.Fatal(err)
	}

	path := filepath.Join(dir, "tail_"+utcDate(1780000255000)+".jsonl")
	var byCond = map[string]Record{}
	raw := readLines(t, path)
	for _, l := range raw {
		var rec Record
		json.Unmarshal([]byte(l), &rec)
		byCond[rec.ConditionID] = rec
	}
	for _, c := range cases {
		if got := byCond[c.cond].SettleSrc; got != c.src {
			t.Fatalf("%s settle_src = %q, 期望 %q", c.cond, got, c.src)
		}
	}
	if n := strings.Count(strings.Join(raw, "\n"), `"settle_src"`); n != len(cases) {
		t.Fatalf("settle_src 出现 %d 次, 期望 %d（omitempty 失效?）", n, len(cases))
	}

	// 重启（磁盘载入）不得丢字段
	r.Close()
	r2, err := NewRecorder(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer r2.Close()
	for _, c := range cases {
		var rec Record
		found := false
		for _, l := range readLines(t, path) {
			json.Unmarshal([]byte(l), &rec)
			if rec.ConditionID == c.cond {
				found = true
				break
			}
		}
		if !found || rec.SettleSrc != c.src {
			t.Fatalf("重启后 %s settle_src = %q, 期望 %q", c.cond, rec.SettleSrc, c.src)
		}
	}
}

func near(a, b float64) bool { return a-b < 1e-9 && b-a < 1e-9 }

// TestRecorderDayRotation 按 UTC 日切分: 跨日的行进各自的文件。
func TestRecorderDayRotation(t *testing.T) {
	r, dir := newTestRecorder(t)
	day1 := time.Date(2026, 9, 22, 23, 59, 0, 0, time.UTC).UnixMilli()
	day2 := day1 + 2*60*1000 // 跨过 UTC 午夜
	if _, err := r.RecordObservation("0xa", "slug-a", 0, okSnap(day1, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	if _, err := r.RecordObservation("0xb", "slug-b", 0, okSnap(day2, flip.SideNo), 2); err != nil {
		t.Fatal(err)
	}
	for _, date := range []string{"2026-09-22", "2026-09-23"} {
		p := filepath.Join(dir, "tail_"+date+".jsonl")
		if n := len(readLines(t, p)); n != 1 {
			t.Fatalf("%s 应有 1 行, 得到 %d", p, n)
		}
	}
}

// TestRecorderReload 重启扫描: tail_ 行恢复内存态与 pending, tailwin_ 行恢复 σ 源。
func TestRecorderReload(t *testing.T) {
	r, dir := newTestRecorder(t)
	ts := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC).UnixMilli()
	if _, err := r.RecordObservation("0xc", "slug", 1780000000, okSnap(ts, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	// 一个未结算的 ok 信号 + 一条已结算的（不应进 pending）。
	resolved := okSnap(ts+1000, flip.SideNo)
	resolved.OK = true
	if _, err := r.RecordObservation("0xd", "slug-d", 1780000000, resolved, 2); err != nil {
		t.Fatal(err)
	}
	r.Resolve("0xd", flip.OutcomeDown, time.UnixMilli(ts+2000), "")
	if err := r.LogWindowAmplitude(flip.WindowEntry{
		Ts: ts, ConditionID: "0xc", Slug: "slug", Anchor: 100000, Close: 100050, Amp: 50,
	}); err != nil {
		t.Fatal(err)
	}
	if err := r.Close(); err != nil {
		t.Fatal(err)
	}

	r2, err := NewRecorder(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer r2.Close()
	obs := r2.Observations()
	if len(obs) != 2 {
		t.Fatalf("应恢复 2 行, 得到 %d", len(obs))
	}
	pend := r2.PendingSignals()
	if len(pend) != 1 || pend[0].ConditionID != "0xc" {
		t.Fatalf("重启后应只剩未结算的 0xc, 得到 %+v", pend)
	}
	wins := r2.RecentWindows(18)
	if len(wins) != 1 || wins[0].Amp != 50 {
		t.Fatalf("应恢复 1 条窗口振幅行, 得到 %+v", wins)
	}
	if wins[0].Kind != winKindAmp {
		t.Fatalf("振幅行 kind 应为 %q, 得到 %q", winKindAmp, wins[0].Kind)
	}
	// 已结算行的 Won/PnL 也要恢复（不是只恢复计数）。
	for _, o := range obs {
		if o.ConditionID == "0xd" {
			if o.Won == nil || !*o.Won || !near(o.PnL, float64(2)/0.92-2) {
				t.Fatalf("已结算行未正确恢复: won=%v pnl=%.4f", o.Won, o.PnL)
			}
		}
	}
}

// TestRecorderPrefixIsolation 三前缀互不串读（σ 污染红线, CLAUDE.md 决策 #9）:
// 统计行写进 tailstats_、观测行写进 tail_，二者都**不得**进 tailwin_（σ 预热源）。
func TestRecorderPrefixIsolation(t *testing.T) {
	r, dir := newTestRecorder(t)
	ts := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC).UnixMilli()
	if _, err := r.RecordObservation("0xc", "slug", 1780000000, okSnap(ts, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 3; i++ {
		if err := r.LogWindowStats(StatsRow{Ts: ts + int64(i)*1000, Anchor: 100000, HistBps: 9, AnchorExact: true, AnchorSrc: "stream"}); err != nil {
			t.Fatal(err)
		}
	}
	if err := r.LogWindowAmplitude(flip.WindowEntry{Ts: ts, Anchor: 100000, Close: 100050, Amp: 50}); err != nil {
		t.Fatal(err)
	}
	// 三族各自的文件存在且行数正确。
	date := "2026-09-22"
	if n := len(readLines(t, filepath.Join(dir, "tail_"+date+".jsonl"))); n != 1 {
		t.Fatalf("tail_ 应 1 行, 得到 %d", n)
	}
	if n := len(readLines(t, filepath.Join(dir, "tailstats_"+date+".jsonl"))); n != 3 {
		t.Fatalf("tailstats_ 应 3 行, 得到 %d", n)
	}
	if n := len(readLines(t, filepath.Join(dir, "tailwin_"+date+".jsonl"))); n != 1 {
		t.Fatalf("tailwin_ 应 1 行, 得到 %d", n)
	}
	// 重启后 σ 源只认振幅行——即便有人手滑把统计行写进了 tailwin_ 文件。
	r.Close()
	blob := readLines(t, filepath.Join(dir, "tailstats_"+date+".jsonl"))
	extra := strings.Join(blob, "\n") + "\n"
	if err := os.WriteFile(filepath.Join(dir, "tailwin_"+date+".jsonl"), []byte(
		readLines(t, filepath.Join(dir, "tailwin_"+date+".jsonl"))[0]+"\n"+extra), 0o644); err != nil {
		t.Fatal(err)
	}
	r2, err := NewRecorder(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer r2.Close()
	if w := r2.RecentWindows(18); len(w) != 1 {
		t.Fatalf("统计行必须被拒（口径污染会让 σ 以 Amp=0 混入）, 得到 %d 条振幅行", len(w))
	}
}

// TestRecorderHasKind 防重入判据按 kind 分流: 帧行的存在**不得**让快照被跳过
// （帧在 rem≤150、快照在 rem≤60, 中间 90s 的崩溃窗口；按「有帧就整窗跳过」会白丢样本）。
func TestRecorderHasKind(t *testing.T) {
	r, _ := newTestRecorder(t)
	ts := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC).UnixMilli()
	frame := okSnap(ts, flip.SideYes)
	frame.Kind, frame.Rules, frame.OK, frame.Shares = KindFrame, Rules{}, false, 0
	if _, err := r.RecordObservation("0xc", "slug", 1780000000, frame, 0); err != nil {
		t.Fatal(err)
	}
	if !r.HasKind("0xc", KindFrame) {
		t.Fatal("帧行应可查得")
	}
	if r.HasKind("0xc", KindSnap) {
		t.Fatal("帧行不得当成快照——否则崩溃重入会白丢整个尾盘样本")
	}
	if _, err := r.RecordObservation("0xc", "slug", 1780000000, okSnap(ts+40000, flip.SideNo), 2); err != nil {
		t.Fatal(err)
	}
	if !r.HasKind("0xc", KindSnap) {
		t.Fatal("快照行应可查得（重启后据此整窗跳过, 防双开）")
	}
	if r.HasKind("0xother", KindSnap) {
		t.Fatal("按 conditionID 隔离")
	}
}

// TestRecorderGatedLatch 日亏熔断的当日锁存判据（按 UTC 日 + 原因隔离）。
func TestRecorderGatedLatch(t *testing.T) {
	r, _ := newTestRecorder(t)
	ts := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC).UnixMilli()
	if _, err := r.RecordGatedObservation("0xc", "slug", 1780000000, okSnap(ts, flip.SideYes), 2, GateDailyLoss); err != nil {
		t.Fatal(err)
	}
	if !r.GatedOn("2026-09-22", GateDailyLoss) {
		t.Fatal("当日应锁存")
	}
	if r.GatedOn("2026-09-23", GateDailyLoss) {
		t.Fatal("跨日应自动归零")
	}
	if r.GatedOn("2026-09-22", GateFirstWindow) {
		t.Fatal("锁存按原因隔离")
	}
	if n := r.GatedToday("2026-09-22", GateDailyLoss); n != 1 {
		t.Fatalf("被闸计数应为 1, 得到 %d", n)
	}
	// 被闸行照常结算（方案 A）——已结算 P&L 里能看见它。
	r.Resolve("0xc", flip.OutcomeUp, time.UnixMilli(ts+3000), "")
	days := r.DailyPnl()
	if len(days) != 1 || !near(days[0].PnL, 2/0.92-2) {
		t.Fatalf("被闸行应照常结算, 得到 %+v", days)
	}
}

// TestRecorderExecutionPaths live 两条回填路径: submitting → 即时终态（rejected）、
// 以及 GTC 的 resting → FillTracker 定稿（filled/unfilled/仍是 resting）。
func TestRecorderExecutionPaths(t *testing.T) {
	r, dir := newTestRecorder(t)
	ts := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC).UnixMilli()
	date := utcDate(ts)

	// ① rejected（如风控闸或 CLOB 拒单）: 不入 pending。
	if _, err := r.SubmitLiveObservation("0xr", "s", 0, okSnap(ts, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	rec, filled, err := r.CompleteExecution("0xr", flip.ExecResult{Status: flip.ExecStatusRejected, Note: "CLOB 拒绝"})
	if err != nil || filled {
		t.Fatalf("rejected 不应算成交: filled=%v err=%v", filled, err)
	}
	if rec.ExecStatus != flip.ExecStatusRejected || len(r.PendingSignals()) != 0 {
		t.Fatalf("rejected 行不该入 pending: %+v", r.PendingSignals())
	}
	// 重复回填必须报错（不静默）。
	if _, _, err := r.CompleteExecution("0xr", flip.ExecResult{Status: flip.ExecStatusFilled}); err == nil {
		t.Fatal("重复回填应报错")
	}

	// ② resting（GTC 挂单在簿）: **不写** Shares/Cost/FillPrice, 不入 pending。
	if _, err := r.SubmitLiveObservation("0xg", "s", 0, okSnap(ts+1000, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	rec, filled, err = r.CompleteExecution("0xg", flip.ExecResult{Status: flip.ExecStatusResting, OrderID: "order-1"})
	if err != nil || filled {
		t.Fatalf("resting 不应算成交: filled=%v err=%v", filled, err)
	}
	if rec.OrderID != "order-1" || rec.Cost != 0 || rec.FillPrice != 0 {
		t.Fatalf("resting 行不该有成交口径字段: cost=%.2f fill=%.4f", rec.Cost, rec.FillPrice)
	}
	if rec.Shares <= 0 {
		t.Fatalf("resting 行保留目标股数供分析, 得到 %.4f", rec.Shares)
	}
	if len(r.PendingSignals()) != 0 {
		t.Fatal("resting 行不入 pending（仓位未定, 中途结算会按半个仓位记账）")
	}
	// 定稿: 部分成交。
	if _, err := r.CompleteRestingFill(flip.FillFinal{
		ConditionID: "0xg", Status: flip.ExecStatusPartial, Shares: 1.5, Cost: 1.5 * 0.92, Note: "余量已撤",
	}); err != nil {
		t.Fatal(err)
	}
	pend := r.PendingSignals()
	if len(pend) != 1 || pend[0].ConditionID != "0xg" {
		t.Fatalf("定稿为 partial 后应入 pending, 得到 %+v", pend)
	}
	if !near(pend[0].FillPrice, 0.92) {
		t.Fatalf("成交均价应为 cost/shares=0.92, 得到 %.4f", pend[0].FillPrice)
	}
	// 定稿后结算: 赢 = shares − cost（实际股数, 不是目标股数）。
	if !r.Resolve("0xg", flip.OutcomeUp, time.UnixMilli(ts+600000), "") {
		t.Fatal("Resolve 应命中")
	}
	// ⚠️ 必须重新取行: 对外读口返回的是**字段副本**（见 copyRecords）, 上面那份
	// pend[0] 不会跟着 Resolve 变——拿旧副本断言等于测了个假东西。
	var xg *Record
	for _, o := range r.Observations() {
		if o.ConditionID == "0xg" {
			xg = o
		}
	}
	if xg == nil || xg.PnL == 0 {
		t.Fatal("0xg 应已结算并回写 PnL")
	}
	if !near(xg.PnL, 1.5-1.5*0.92) {
		t.Fatalf("赢 P&L 应为 shares−cost=%.4f, 得到 %.4f", 1.5-1.5*0.92, xg.PnL)
	}

	// ③ unfilled（撤单时 0 成交）: 目标股数保留, 不入 pending。
	if _, err := r.SubmitLiveObservation("0xu", "s", 0, okSnap(ts+2000, flip.SideNo), 2); err != nil {
		t.Fatal(err)
	}
	r.CompleteExecution("0xu", flip.ExecResult{Status: flip.ExecStatusResting, OrderID: "order-2"})
	if _, err := r.CompleteRestingFill(flip.FillFinal{ConditionID: "0xu", Status: flip.ExecStatusUnfilled, Note: "余量已撤"}); err != nil {
		t.Fatal(err)
	}
	if len(r.PendingSignals()) != 0 {
		t.Fatal("unfilled 不入 pending")
	}

	// ④ 仍是 resting（从未观测到）: 留行 + 待人工核对。
	if _, err := r.SubmitLiveObservation("0xx", "s", 0, okSnap(ts+3000, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	r.CompleteExecution("0xx", flip.ExecResult{Status: flip.ExecStatusResting, OrderID: "order-3"})
	if _, err := r.CompleteRestingFill(flip.FillFinal{
		ConditionID: "0xx", Status: flip.ExecStatusResting, Note: flip.ExecNoteUnknown + ": 从未观测到该单",
	}); err != nil {
		t.Fatal(err)
	}
	if n := r.NeedsReconcile(); n != 1 {
		t.Fatalf("未确认的挂单应计 1 条待核对, 得到 %d", n)
	}
	// 在途的正常 resting 行（可跟踪）不计——否则告警常亮。
	if _, err := r.SubmitLiveObservation("0xy", "s", 0, okSnap(ts+4000, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	r.CompleteExecution("0xy", flip.ExecResult{Status: flip.ExecStatusResting, OrderID: "order-4"})
	if n := r.NeedsReconcile(); n != 1 {
		t.Fatalf("在途挂单不该计入待核对, 得到 %d", n)
	}

	// 磁盘: 每个终态都原子重写过（行数与内存一致）。
	lines := readLines(t, filepath.Join(dir, "tail_"+date+".jsonl"))
	if len(lines) != len(r.Observations()) {
		t.Fatalf("重写后磁盘行数 %d ≠ 内存 %d", len(lines), len(r.Observations()))
	}
}

// TestRecorderFindSnapNotFrame 执行回填必须落在快照行上——同窗的帧行先落盘,
// 按 conditionID 找第一行会回填到没有 stake 的帧行（那条永远不是 submitting）。
func TestRecorderFindSnapNotFrame(t *testing.T) {
	r, _ := newTestRecorder(t)
	ts := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC).UnixMilli()
	frame := okSnap(ts, flip.SideYes)
	frame.Kind, frame.Rules, frame.OK, frame.Shares = KindFrame, Rules{}, false, 0
	if _, err := r.RecordObservation("0xc", "slug", 0, frame, 0); err != nil {
		t.Fatal(err)
	}
	// live 的两阶段: submitting 行（帧行在它前面, 不能被当成回填目标）。
	if _, err := r.SubmitLiveObservation("0xc", "slug", 0, okSnap(ts+40000, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	rec, _, err := r.CompleteExecution("0xc", flip.ExecResult{Status: flip.ExecStatusRejected, Note: "x"})
	if err != nil {
		t.Fatal(err)
	}
	if rec.Kind != KindSnap || rec.Stake != 2 {
		t.Fatalf("回填应落在快照行（kind=%s stake=%.1f）", rec.Kind, rec.Stake)
	}
	if rec.ExecNote != "x" {
		t.Fatalf("回填应写 ExecNote, 得到 %q", rec.ExecNote)
	}
	// 带 payload 的帧行也必须落盘（不是只写个骨架）。
	obs := r.Observations()
	if len(obs) != 2 || obs[0].HotAsk != 0.92 || obs[0].Anchor != 100000 {
		t.Fatalf("帧行应带完整快照字段: %+v", obs[0])
	}
}

// TestRecorderSignalsCountsDrawdown 三个只读口的口径: 帧行不入任何计数、被闸行
// 计入 Signals（反事实样本, 过滤是消费端的事）、回撤按累计 P&L 峰值差现算。
func TestRecorderSignalsCountsDrawdown(t *testing.T) {
	r, _ := newTestRecorder(t)
	base := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC).UnixMilli()

	// 窗 1: 帧行（不入任何计数）。
	frame := okSnap(base, flip.SideYes)
	frame.Kind, frame.Rules, frame.OK, frame.Shares = KindFrame, Rules{}, false, 0
	if _, err := r.RecordObservation("0xc1", "s", 0, frame, 0); err != nil {
		t.Fatal(err)
	}
	// 窗 2: 否决行（计入 snap, 不计入 ok）。
	rej := okSnap(base+300000, flip.SideYes)
	rej.OK, rej.RejectReason, rej.Shares = false, RejectLegOut, 0
	if _, err := r.RecordObservation("0xc2", "s", 0, rej, 2); err != nil {
		t.Fatal(err)
	}
	// 窗 3: ok 行 → 结算赢（+2/0.92−2 ≈ +0.174）。
	if _, err := r.RecordObservation("0xc3", "s", 0, okSnap(base+600000, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	if !r.Resolve("0xc3", flip.OutcomeUp, time.Unix(0, 0), "") {
		t.Fatal("Resolve 应命中")
	}
	// 窗 4: ok 行 → 结算输（−2）。
	if _, err := r.RecordObservation("0xc4", "s", 0, okSnap(base+900000, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	r.Resolve("0xc4", flip.OutcomeDown, time.Unix(0, 0), "")
	// 窗 5: ok 行 → 被闸（仍计入 Signals / Counts.ok）。
	if _, err := r.RecordGatedObservation("0xc5", "s", 0, okSnap(base+1200000, flip.SideYes), 2, GateDailyLoss); err != nil {
		t.Fatal(err)
	}

	snap, ok, won := r.Counts()
	if snap != 4 || ok != 3 || won != 1 {
		t.Errorf("Counts = (%d, %d, %d), want (4, 3, 1)", snap, ok, won)
	}
	sigs := r.Signals()
	if len(sigs) != 3 {
		t.Fatalf("Signals 应 3 条（含被闸行, 不含帧行/否决行）, 得到 %d", len(sigs))
	}
	for _, s := range sigs {
		if s.Kind != KindSnap || !s.OK {
			t.Errorf("Signals 混入非 ok 快照行: kind=%s ok=%v", s.Kind, s.OK)
		}
	}
	// 回撤: 累计 +0.174 → −2 → 峰 0.174, 谷 −1.826 → dd = −2.0（被闸行未结算, 不入）。
	if dd := r.MaxDrawdown(); dd != -2.0 {
		t.Errorf("MaxDrawdown = %v, want -2", dd)
	}
	// 副本语义: 改返回值不影响内部记录。
	sigs[0].PnL = 999
	if r.Signals()[0].PnL == 999 {
		t.Error("Signals 必须返回副本（结算轮询并发写 Won/PnL）")
	}
}

// TestRecorderScanRowIsolation 钉住监听对账行（KindScan）的四条红线:
//  1. 可落盘、可结算（否则离线无从算它的 P&L）;
//  2. **不进** DailyPnl —— 它没有真实仓位, 混进去会拿一条反事实盈亏去开关熔断闸;
//  3. **不进** Signals/Counts/MaxDrawdown —— 那些是决策快照的统计口径;
//  4. 重启载入时进 pending（未结算的 scan 行要重新注册轮询）。
func TestRecorderScanRowIsolation(t *testing.T) {
	r, dir := newTestRecorder(t)
	base := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC).UnixMilli()

	// 窗 1: 被否决的 snap（不入 pending、不进 P&L）+ 监听行（结算赢）。
	rej := okSnap(base, flip.SideYes)
	rej.OK, rej.RejectReason, rej.Shares = false, RejectLegOut, 0
	if _, err := r.RecordObservation("0xc1", "s", 0, rej, 2); err != nil {
		t.Fatal(err)
	}
	scan := okSnap(base, flip.SideYes)
	scan.Kind, scan.Rem = KindScan, 40 // 监听段晚于快照
	if _, err := r.RecordObservation("0xc1", "s", 0, scan, 2); err != nil {
		t.Fatal(err)
	}
	if !r.Resolve("0xc1", flip.OutcomeUp, time.Unix(0, 0), "") {
		t.Fatal("scan 行应挂结算（isSettlable 必须收 scan）")
	}
	// 窗 2: 真实快照 ok → 结算赢。它才是当日 P&L 的唯一来源。
	if _, err := r.RecordObservation("0xc2", "s", 0, okSnap(base+300000, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	r.Resolve("0xc2", flip.OutcomeUp, time.Unix(0, 0), "")

	// 2+3: 统计口径只认 snap 行（窗 1 的否决行 + 窗 2 的 ok 行 = 2; scan 不得计入）。
	snap, ok, won := r.Counts()
	if snap != 2 || ok != 1 || won != 1 {
		t.Errorf("Counts = (%d, %d, %d), want (2, 1, 1)（scan 不得计入）", snap, ok, won)
	}
	if sigs := r.Signals(); len(sigs) != 1 || sigs[0].ConditionID != "0xc2" {
		t.Errorf("Signals 混入 scan 行: %+v", sigs)
	}
	if dd := r.MaxDrawdown(); dd != 0 {
		t.Errorf("MaxDrawdown = %v, want 0（两笔都赢; scan 不得计入）", dd)
	}
	// 2: DailyPnl 只吃 snap。
	days := r.DailyPnl()
	one := 2/0.92 - 2 // 单笔赢的 P&L
	if len(days) != 1 || days[0].N != 1 || days[0].PnL < one-1e-9 || days[0].PnL > one+1e-9 {
		t.Fatalf("DailyPnl 应按 1 笔 snap 计 %.4f, 得到 %+v（scan 混入即为 bug）", one, days)
	}

	// 1: 两行都落盘且 kind 正确。
	lines := readLines(t, filepath.Join(dir, "tail_2026-09-22.jsonl"))
	if len(lines) != 3 {
		t.Fatalf("应有 3 行（rej snap + scan + ok snap）, 得到 %d", len(lines))
	}
	var kinds []string
	for _, l := range lines {
		var m map[string]any
		if err := json.Unmarshal([]byte(l), &m); err != nil {
			t.Fatal(err)
		}
		kinds = append(kinds, m["kind"].(string))
	}
	want := []string{KindSnap, KindScan, KindSnap}
	for i := range want {
		if kinds[i] != want[i] {
			t.Fatalf("行 kind 序 = %v, want %v", kinds, want)
		}
	}

	// 4: 重启载入——未结算的 scan 行进 pending。
	r2, err := NewRecorder(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer r2.Close()
	if n := len(r2.PendingSignals()); n != 0 {
		t.Errorf("本用例两行都已结算, 重启后 pending 应为 0, 得到 %d", n)
	}

	// 未结算的 scan 行: 重开一个目录单独验证。
	dir3 := t.TempDir()
	r3, err := NewRecorder(dir3)
	if err != nil {
		t.Fatal(err)
	}
	s3 := okSnap(base, flip.SideYes)
	s3.Kind = KindScan
	if _, err := r3.RecordObservation("0xc9", "s", 0, s3, 2); err != nil {
		t.Fatal(err)
	}
	r3.Close()
	r4, err := NewRecorder(dir3)
	if err != nil {
		t.Fatal(err)
	}
	defer r4.Close()
	pend := r4.PendingSignals()
	if len(pend) != 1 || pend[0].ConditionID != "0xc9" || pend[0].Kind != KindScan {
		t.Fatalf("重启后未结算 scan 行应进 pending, 得到 %+v", pend)
	}
}

// TestRecorderTodayStats 健康度读口: 只读当日文件、坏行跳过、无文件返回空。
func TestRecorderTodayStats(t *testing.T) {
	r, dir := newTestRecorder(t)

	// 当日尚无文件。
	rows, err := r.TodayStats()
	if err != nil || len(rows) != 0 {
		t.Fatalf("空目录应返回 (nil/空, nil), 得到 (%d 行, %v)", len(rows), err)
	}

	today := utcToday()
	rowsIn := []StatsRow{
		{Ts: time.Now().UnixMilli(), ConditionID: "0xa", AnchorExact: true, Skip: ""},
		{Ts: time.Now().UnixMilli(), ConditionID: "0xb", AnchorExact: false, Skip: "no_sigma"},
	}
	for _, e := range rowsIn {
		if err := r.LogWindowStats(e); err != nil { // 走真实落盘路径（含 kind/date 派生）
			t.Fatal(err)
		}
	}
	// 手工塞一行坏数据 + 一个空行（进程追写时的半行形态）。
	path := filepath.Join(dir, statsPrefix+today+".jsonl")
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	f.WriteString("{不是 JSON\n\n")
	f.Close()

	rows, err = r.TodayStats()
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 2 {
		t.Fatalf("坏行应跳过, 得到 %d 行", len(rows))
	}
	if rows[0].ConditionID != "0xa" || !rows[0].AnchorExact || rows[0].Kind != winKindStats || rows[0].Date != today {
		t.Errorf("行 schema 不对: %+v", rows[0])
	}
	if rows[1].Skip != "no_sigma" || rows[1].AnchorExact {
		t.Errorf("第 2 行 skip/anchor_exact 不对: %+v", rows[1])
	}
	// 昨日文件不读（读口只认当日）。
	if err := r.LogWindowStats(StatsRow{Ts: time.Now().Add(-25 * time.Hour).UnixMilli(), ConditionID: "0xold"}); err != nil {
		t.Fatal(err)
	}
	rows, _ = r.TodayStats()
	if len(rows) != 2 {
		t.Fatalf("昨日行不该进入今日读口, 得到 %d 行", len(rows))
	}
}
