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

// okSnap 构造一条 ⑤ 成立、可直接下单的信号行（hot_ask 0.92, dev +100, 第二段 T=60）。
func okSnap(ts int64, side string) *Observation {
	o := &Observation{
		Kind: KindSnap, Stage: StageT60, FrameT: 60, Ts: ts, Rem: 55,
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

// TestRecorderDecisionAndSignalFiles 钉住两件形制: ① 一个窗口的多行（判定行 + 信号行）
// 都落同一个 tail_ 文件、kind 都是 snap、由 stage 区分; ② 判定行（ok=false）不进
// pending、不参与结算。
func TestRecorderDecisionAndSignalFiles(t *testing.T) {
	r, dir := newTestRecorder(t)
	conds := "0xcond1"
	date := utcDate(1780000000000)
	path := filepath.Join(dir, "tail_"+date+".jsonl")

	// 第一段判定行（rem≤150, 被位移腿拒）。
	d := okSnap(1780000000000, flip.SideYes)
	d.Stage, d.FrameT, d.Rem = StageT150, 150, 145
	d.Rules, d.OK, d.Shares, d.RejectReason = Rules{Price: true}, false, 0, RejectLegOut
	if _, err := r.RecordObservation(conds, "btc-updown-5m-1780000000", 1780000000, d, 2); err != nil {
		t.Fatalf("判定行落盘: %v", err)
	}
	// 第二段信号行（rem≤60, 判定通过 + 成交）。
	if _, err := r.RecordObservation(conds, "btc-updown-5m-1780000000", 1780000000, okSnap(1780000255000, flip.SideYes), 2); err != nil {
		t.Fatalf("信号行落盘: %v", err)
	}

	lines := readLines(t, path)
	if len(lines) != 2 {
		t.Fatalf("一个窗口应两行, 得到 %d 行", len(lines))
	}
	var f, s Record
	json.Unmarshal([]byte(lines[0]), &f)
	json.Unmarshal([]byte(lines[1]), &s)
	if f.Kind != KindSnap || s.Kind != KindSnap {
		t.Fatalf("两行 kind 都应为 snap, 得到 %s→%s", f.Kind, s.Kind)
	}
	if f.Stage != StageT150 || s.Stage != StageT60 {
		t.Fatalf("行序 stage 应为 t150→t60, 得到 %s→%s", f.Stage, s.Stage)
	}
	if f.FrameT != 150 || s.FrameT != 60 {
		t.Fatalf("FrameT 应随段: 150/60, 得到 %d/%d", f.FrameT, s.FrameT)
	}
	if f.EventType != "tail" {
		t.Fatalf("event_type 应为 tail, 得到 %q", f.EventType)
	}
	if s.Stake != 2 || !s.OK {
		t.Fatalf("信号行应带 stake=2 且 ok: %+v", s)
	}
	// 只有信号行进 pending（判定行 OK=false）。
	pend := r.PendingSignals()
	if len(pend) != 1 || pend[0].Stage != StageT60 {
		t.Fatalf("判定行不得入 pending, 应只有信号行, 得到 %+v", pend)
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

// TestRecorderHasKind legacy 查询口按 kind 分流: 帧行的存在**不得**让快照被跳过。
//
// ⚠️ 2026-09-24 起防重入判据已换成 HasSignal / HasStage（见 TestRecorderHasSignalAndStage）
// ——「有行就跳过」会把「只做了第一段判定」的窗口误判成整窗完成, 在新三段链下是错的。
// 本函数保留给 legacy 行查询, 故这里只钉住它自己的分流语义。
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

// TestRecorderGatedLatch 日亏熔断的当日锁存判据（按 UTC 日 + 原因隔离），以及
// **被闸 = 未成交**：行照常结算以便页面显示官方结果, 但 P&L 恒 0、不进熔断输入。
func TestRecorderGatedLatch(t *testing.T) {
	r, _ := newTestRecorder(t)
	ts := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC).UnixMilli()
	if _, err := r.RecordRejected("0xc", "slug", 1780000000, okSnap(ts, flip.SideYes), 2, GateDailyLoss, "日亏熔断(锁存)"); err != nil {
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
	// 被闸行**照常注册结算**（a.md 第 3 条: 每一笔信号都要显示官方结果）。
	if n := len(r.PendingSignals()); n != 1 {
		t.Fatalf("被闸行应入 pending（只为显示结果）, 得到 %d", n)
	}
	if !r.Resolve("0xc", flip.OutcomeUp, time.UnixMilli(ts+3000), "") {
		t.Fatal("被闸行应能结算")
	}
	var rec *Record
	for _, o := range r.Observations() {
		rec = o
	}
	if rec.Won == nil || !*rec.Won {
		t.Fatalf("结果照显（押 yes 遇 Up = 赢）: %+v", rec.Won)
	}
	if rec.PnL != 0 {
		t.Fatalf("被闸行无仓位 ⇒ P&L 恒 0, 得到 %.4f", rec.PnL)
	}
	if days := r.DailyPnl(); len(days) != 0 {
		t.Fatalf("被闸行不得进熔断输入（否则不存在的盈亏会去开关闸）: %+v", days)
	}
	if dd := r.MaxDrawdown(); dd != 0 {
		t.Fatalf("被闸行不得进回撤, 得到 %.4f", dd)
	}
}

// TestRecorderExecutionPaths live 两条回填路径 + **结算注册面**（2026-09-24 起）:
// submitting/resting 两个未定稿态不注册, 其余终态（含 rejected/unfilled）**全部注册**
// ——a.md 第 3 条要求每一笔信号都显示官方结果, 而 P&L 由 HasPosition 归零。
func TestRecorderExecutionPaths(t *testing.T) {
	r, dir := newTestRecorder(t)
	ts := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC).UnixMilli()
	date := utcDate(ts)

	// ① rejected（如 CLOB 拒单）: 无仓位, 但**照常注册**（页面要显示结果）。
	if _, err := r.SubmitLiveObservation("0xr", "s", 0, okSnap(ts, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	if len(r.PendingSignals()) != 0 {
		t.Fatal("submitting 行不入 pending（结果未知）")
	}
	rec, filled, err := r.CompleteExecution("0xr", flip.ExecResult{Status: flip.ExecStatusRejected, Note: "CLOB 拒绝"})
	if err != nil || filled {
		t.Fatalf("rejected 不应算成交: filled=%v err=%v", filled, err)
	}
	if rec.ExecStatus != flip.ExecStatusRejected {
		t.Fatalf("状态应为 rejected, 得到 %q", rec.ExecStatus)
	}
	if n := len(r.PendingSignals()); n != 1 {
		t.Fatalf("rejected 行应注册结算（只为显示结果）, 得到 %d 条", n)
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
	// 此刻 pending 只应剩 ① 的 rejected 行——resting 未定稿不得注册（否则会按
	// 半个仓位记账）。
	if pend := r.PendingSignals(); len(pend) != 1 || pend[0].ConditionID != "0xr" {
		t.Fatalf("resting 行不入 pending, 得到 %+v", pend)
	}
	// 定稿: 部分成交 → 此刻才注册。
	if _, err := r.CompleteRestingFill(flip.FillFinal{
		ConditionID: "0xg", Status: flip.ExecStatusPartial, Shares: 1.5, Cost: 1.5 * 0.92, Note: "余量已撤",
	}); err != nil {
		t.Fatal(err)
	}
	pend := r.PendingSignals()
	if len(pend) != 2 {
		t.Fatalf("定稿为 partial 后应共 2 条待结算（0xr rejected + 0xg partial）, 得到 %+v", pend)
	}
	var pg *Record
	for _, p := range pend {
		if p.ConditionID == "0xg" {
			pg = p
		}
	}
	if pg == nil || !near(pg.FillPrice, 0.92) {
		t.Fatalf("成交均价应为 cost/shares=0.92, 得到 %+v", pg)
	}
	// 定稿后结算: 赢 = shares − cost（实际股数, 不是目标股数）。
	if !r.Resolve("0xg", flip.OutcomeUp, time.UnixMilli(ts+600000), "") {
		t.Fatal("Resolve 应命中")
	}
	if !r.Resolve("0xr", flip.OutcomeUp, time.UnixMilli(ts+600000), "") {
		t.Fatal("rejected 行应能结算")
	}
	// ⚠️ 必须重新取行: 对外读口返回的是**字段副本**（见 copyRecords）, 上面那份
	// pend 里的行不会跟着 Resolve 变——拿旧副本断言等于测了个假东西。
	byCond := map[string]*Record{}
	for _, o := range r.Observations() {
		byCond[o.ConditionID] = o
	}
	xg := byCond["0xg"]
	if xg == nil || xg.PnL == 0 {
		t.Fatal("0xg 应已结算并回写 PnL")
	}
	if !near(xg.PnL, 1.5-1.5*0.92) {
		t.Fatalf("赢 P&L 应为 shares−cost=%.4f, 得到 %.4f", 1.5-1.5*0.92, xg.PnL)
	}
	if xr := byCond["0xr"]; xr == nil || xr.Won == nil || !*xr.Won || xr.PnL != 0 {
		t.Fatalf("rejected 行应显示「赢」但 P&L 恒 0: %+v", xr)
	}

	// ③ unfilled（撤单时 0 成交）: 目标股数保留, **注册结算**但无仓位。
	if _, err := r.SubmitLiveObservation("0xu", "s", 0, okSnap(ts+2000, flip.SideNo), 2); err != nil {
		t.Fatal(err)
	}
	r.CompleteExecution("0xu", flip.ExecResult{Status: flip.ExecStatusResting, OrderID: "order-2"})
	if _, err := r.CompleteRestingFill(flip.FillFinal{ConditionID: "0xu", Status: flip.ExecStatusUnfilled, Note: "余量已撤"}); err != nil {
		t.Fatal(err)
	}
	if !r.Resolve("0xu", flip.OutcomeDown, time.UnixMilli(ts+600000), "") {
		t.Fatal("unfilled 行应注册结算")
	}
	byCond = map[string]*Record{}
	for _, o := range r.Observations() {
		byCond[o.ConditionID] = o
	}
	if xu := byCond["0xu"]; xu.PnL != 0 {
		t.Fatalf("0 成交 ⇒ P&L 恒 0, 得到 %.4f", xu.PnL)
	}

	// ④ 仍是 resting（从未观测到）: 留行 + 待人工核对 + **不注册结算**。
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
	for _, p := range r.PendingSignals() {
		if p.ConditionID == "0xx" {
			t.Fatal("成交未知的行不得注册结算（会按目标股数记账）")
		}
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

// TestRecorderFindSnapSkipsDecision 执行回填必须落在**信号行**上——三段链下同一
// 窗口本来就有多条 KindSnap 行（t150 判定行、t60 判定行、信号行）, 判定行没有 exec
// 语义（那条永远不会是 submitting）, 按 conditionID 找第一行就会打错靶。
func TestRecorderFindSnapSkipsDecision(t *testing.T) {
	r, _ := newTestRecorder(t)
	ts := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC).UnixMilli()
	// 第一段判定行（被拒, ok=false, 带完整快照字段）。
	d := okSnap(ts, flip.SideYes)
	d.Stage, d.FrameT, d.Rem = StageT150, 150, 145
	d.Rules, d.OK, d.Shares, d.RejectReason = Rules{Price: true}, false, 0, RejectLegOut
	if _, err := r.RecordObservation("0xc", "slug", 0, d, 0); err != nil {
		t.Fatal(err)
	}
	// live 的两阶段: submitting 行（判定行在它前面, 不能被当成回填目标）。
	if _, err := r.SubmitLiveObservation("0xc", "slug", 0, okSnap(ts+40000, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	rec, _, err := r.CompleteExecution("0xc", flip.ExecResult{Status: flip.ExecStatusRejected, Note: "x"})
	if err != nil {
		t.Fatal(err)
	}
	if rec.Stage != StageT60 || rec.Stake != 2 {
		t.Fatalf("回填应落在信号行（stage=%s stake=%.1f）", rec.Stage, rec.Stake)
	}
	if rec.ExecNote != "x" {
		t.Fatalf("回填应写 ExecNote, 得到 %q", rec.ExecNote)
	}
	// 判定行必须原样保留（判定行也要带完整快照字段, 不是只写个骨架）。
	obs := r.Observations()
	if len(obs) != 2 || obs[0].Stage != StageT150 || obs[0].HotAsk != 0.92 || obs[0].Anchor != 100000 {
		t.Fatalf("判定行应带完整快照字段: %+v", obs[0])
	}
	if obs[0].ExecStatus != "" || obs[0].Stake != 0 {
		t.Fatalf("回填不得污染判定行: %+v", obs[0])
	}
}

// TestRecorderSignalsAndDrawdown 只读口的口径: 判定行不入 Signals、被闸行**计入**
// Signals（页面要显示它, 分类是消费端 tally 的事）、回撤按累计 P&L 峰值差现算且
// 只吃有仓位的行。
func TestRecorderSignalsAndDrawdown(t *testing.T) {
	r, _ := newTestRecorder(t)
	base := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC).UnixMilli()

	// 窗 1: 判定行（不入 Signals）。
	d := okSnap(base, flip.SideYes)
	d.OK, d.Rules, d.Shares, d.RejectReason = false, Rules{}, 0, RejectPriceLow
	if _, err := r.RecordObservation("0xc1", "s", 0, d, 2); err != nil {
		t.Fatal(err)
	}
	// 窗 2: 判定行被拒（同上, 不入 Signals）。
	rej := okSnap(base+300000, flip.SideYes)
	rej.OK, rej.RejectReason, rej.Shares = false, RejectLegOut, 0
	if _, err := r.RecordObservation("0xc2", "s", 0, rej, 2); err != nil {
		t.Fatal(err)
	}
	// 窗 3: 信号行 → 结算赢（+2/0.92−2 ≈ +0.174）。
	if _, err := r.RecordObservation("0xc3", "s", 0, okSnap(base+600000, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	if !r.Resolve("0xc3", flip.OutcomeUp, time.Unix(0, 0), "") {
		t.Fatal("Resolve 应命中")
	}
	// 窗 4: 信号行 → 结算输（−2）。
	if _, err := r.RecordObservation("0xc4", "s", 0, okSnap(base+900000, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	r.Resolve("0xc4", flip.OutcomeDown, time.Unix(0, 0), "")
	// 窗 5: 信号行 → 被闸（计入 Signals, 但无仓位）。
	if _, err := r.RecordRejected("0xc5", "s", 0, okSnap(base+1200000, flip.SideYes), 2, GateDailyLoss, "熔断"); err != nil {
		t.Fatal(err)
	}

	sigs := r.Signals()
	if len(sigs) != 3 {
		t.Fatalf("Signals 应 3 条（含被闸行, 不含两条判定行）, 得到 %d", len(sigs))
	}
	for _, s := range sigs {
		if s.Kind != KindSnap || !s.OK {
			t.Errorf("Signals 混入非信号行: kind=%s stage=%s ok=%v", s.Kind, s.Stage, s.OK)
		}
	}
	// 回撤: 累计 +0.174 → −2 → 峰 0.174, 谷 −1.826 → dd = −2.0（被闸行无仓位, 不入）。
	if dd := r.MaxDrawdown(); dd != -2.0 {
		t.Errorf("MaxDrawdown = %v, want -2", dd)
	}
	// 副本语义: 改返回值不影响内部记录。
	sigs[0].PnL = 999
	if r.Signals()[0].PnL == 999 {
		t.Error("Signals 必须返回副本（结算轮询并发写 Won/PnL）")
	}
}

// TestRecorderHasSignalAndStage 防重入的两个判据（cmd/tail 重启重入用）:
// HasSignal = 本窗是否已出过信号（有 OK 行就整窗跳过）; HasStage = 某段判定是否已做。
func TestRecorderHasSignalAndStage(t *testing.T) {
	r, _ := newTestRecorder(t)
	base := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC).UnixMilli()

	// 只做了第一段判定（被拒）: 有 t150 段、无信号。
	d := okSnap(base, flip.SideYes)
	d.Stage, d.FrameT, d.Rem = StageT150, 150, 145
	d.Rules, d.OK, d.Shares, d.RejectReason = Rules{}, false, 0, RejectLegOut
	if _, err := r.RecordObservation("0xc", "s", 0, d, 2); err != nil {
		t.Fatal(err)
	}
	if r.HasSignal("0xc") {
		t.Fatal("只有被拒的判定行 ⇒ 本窗尚未出信号")
	}
	if !r.HasStage("0xc", StageT150) {
		t.Fatal("第一段判定已落盘")
	}
	if r.HasStage("0xc", StageT60) {
		t.Fatal("第二段尚未做")
	}
	// 出了信号: HasSignal 转真, 且按 conditionID 隔离。
	if _, err := r.RecordObservation("0xc", "s", 0, okSnap(base+250000, flip.SideNo), 2); err != nil {
		t.Fatal(err)
	}
	if !r.HasSignal("0xc") || !r.HasStage("0xc", StageT60) {
		t.Fatal("信号行应同时满足两个判据")
	}
	if r.HasSignal("0xother") || r.HasStage("0xother", StageT150) {
		t.Fatal("两个判据都必须按 conditionID 隔离")
	}
	// 被闸行也算「已出过信号」——重启后同样不该再下一单。
	if _, err := r.RecordRejected("0xg", "s", 0, okSnap(base, flip.SideYes), 2, GateFirstWindow, "首窗"); err != nil {
		t.Fatal(err)
	}
	if !r.HasSignal("0xg") {
		t.Fatal("被闸行 = 已产出信号（不该再下单）")
	}
}

// TestRecomputePnL P&L 口径的纯函数三态: 未结算不动 / 无仓位恒 0 / 有仓位
// 赢 shares−cost、输 −cost（cost 缺省回退 Stake = paper 行）。
func TestRecomputePnL(t *testing.T) {
	// 未结算: 不动。
	rec := &Record{Observation: Observation{OK: true, Shares: 2.2, HotAsk: 0.9}, Stake: 2, PnL: 7}
	recomputePnL(rec)
	if rec.PnL != 7 {
		t.Fatalf("未结算不该动 P&L, 得到 %.4f", rec.PnL)
	}
	won, lost := true, false
	cases := []struct {
		name   string
		exec   string
		kind   string
		won    *bool
		shares float64
		cost   float64
		want   float64
	}{
		{"paper 赢", "", KindSnap, &won, 2 / 0.92, 0, 2/0.92 - 2},
		{"paper 输", "", KindSnap, &lost, 2 / 0.92, 0, -2},
		{"filled 赢（用实际成本）", flip.ExecStatusFilled, KindSnap, &won, 1.5, 1.5 * 0.92, 1.5 - 1.5*0.92},
		{"partial 输（用实际成本）", flip.ExecStatusPartial, KindSnap, &lost, 1.5, 1.5 * 0.92, -1.5 * 0.92},
		{"unfilled 赢 ⇒ 0", flip.ExecStatusUnfilled, KindSnap, &won, 2 / 0.92, 0, 0},
		{"rejected 赢 ⇒ 0", flip.ExecStatusRejected, KindSnap, &won, 2 / 0.92, 0, 0},
		{"legacy scan 赢 ⇒ 0", "", KindScan, &won, 2 / 0.92, 0, 0},
		{"submitting 赢 ⇒ 0", flip.ExecStatusSubmitting, KindSnap, &won, 2 / 0.92, 0, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rec := &Record{
				Observation: Observation{Kind: c.kind, OK: true, Shares: c.shares, HotAsk: 0.92},
				Stake:       2, ExecStatus: c.exec, Won: c.won, Cost: c.cost,
			}
			recomputePnL(rec)
			if !near(rec.PnL, c.want) {
				t.Fatalf("PnL = %.6f, want %.6f", rec.PnL, c.want)
			}
		})
	}
}

// TestRecorderLegacyScanRowIsolation legacy 监听对账行（KindScan, 2026-09-24 前落盘的
// 反事实样本）的四条红线——引擎已不再产出它, 但 data/v4-tail/ 里那些行必须照旧可载入、
// 可结算, 且**绝不**混进任何仓位口径:
//  1. 可落盘、可载入、可结算（否则离线无从复算）;
//  2. **不进** Signals —— 那是信号表的输入, scan 行从未下单;
//  3. 结算后 Won 照写但 **PnL 恒 0**（HasPosition 假）, 因而不进 DailyPnl / MaxDrawdown
//     —— 混进去会拿一条不存在的仓位去开关日亏熔断闸;
//  4. 重启载入时未结算的行进 pending（照常注册结算）。
func TestRecorderLegacyScanRowIsolation(t *testing.T) {
	r, dir := newTestRecorder(t)
	base := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC).UnixMilli()

	// 窗 1: 被否决的判定行（不入 pending、不进 P&L）+ legacy scan 行（结算赢）。
	rej := okSnap(base, flip.SideYes)
	rej.OK, rej.RejectReason, rej.Shares = false, RejectLegOut, 0
	if _, err := r.RecordObservation("0xc1", "s", 0, rej, 2); err != nil {
		t.Fatal(err)
	}
	scan := okSnap(base, flip.SideYes)
	scan.Kind, scan.Rem = KindScan, 40
	if _, err := r.RecordObservation("0xc1", "s", 0, scan, 2); err != nil {
		t.Fatal(err)
	}
	if !r.Resolve("0xc1", flip.OutcomeUp, time.Unix(0, 0), "") {
		t.Fatal("scan 行应挂结算（isSettlable 必须收 scan）")
	}
	// 窗 2: 真实信号行 → 结算赢。它才是当日 P&L 的唯一来源。
	if _, err := r.RecordObservation("0xc2", "s", 0, okSnap(base+300000, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	r.Resolve("0xc2", flip.OutcomeUp, time.Unix(0, 0), "")

	// 2: Signals 只认 KindSnap 的 OK 行。
	if sigs := r.Signals(); len(sigs) != 1 || sigs[0].ConditionID != "0xc2" {
		t.Errorf("Signals 混入 scan 行: %+v", sigs)
	}
	// 3: scan 结算后 Won 有值、PnL 归零; 两个仓位口径都只吃窗 2。
	byCond := map[string]*Record{}
	for _, o := range r.Observations() {
		byCond[o.ConditionID] = o
	}
	if sc := byCond["0xc1"]; sc.Won == nil || !*sc.Won || sc.PnL != 0 {
		t.Fatalf("scan 行应显示赢但 P&L 恒 0: won=%v pnl=%.4f", sc.Won, sc.PnL)
	}
	if dd := r.MaxDrawdown(); dd != 0 {
		t.Errorf("MaxDrawdown = %v, want 0（两笔都赢; scan 无仓位不得计入）", dd)
	}
	days := r.DailyPnl()
	one := 2/0.92 - 2 // 单笔赢的 P&L
	if len(days) != 1 || days[0].N != 1 || days[0].PnL < one-1e-9 || days[0].PnL > one+1e-9 {
		t.Fatalf("DailyPnl 应按 1 笔信号计 %.4f, 得到 %+v（scan 混入即为 bug）", one, days)
	}

	// 1: 三行都落盘且 kind 正确（scan 必须仍被 isKnownKind 接受, 否则旧数据载不进来）。
	lines := readLines(t, filepath.Join(dir, "tail_2026-09-22.jsonl"))
	if len(lines) != 3 {
		t.Fatalf("应有 3 行（rej 判定行 + scan + ok 信号行）, 得到 %d", len(lines))
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

	// 4: 重启载入——已结算的不再进 pending。
	r2, err := NewRecorder(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer r2.Close()
	if n := len(r2.PendingSignals()); n != 0 {
		t.Errorf("本用例两行都已结算, 重启后 pending 应为 0, 得到 %d", n)
	}
	if n := len(r2.Observations()); n != 3 {
		t.Errorf("legacy scan 行必须能载入（isKnownKind 收它）, 得到 %d 行", n)
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
