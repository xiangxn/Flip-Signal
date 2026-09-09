package flip

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// testTs 返回 2026-09-02 12:00:00 UTC 的 epoch 毫秒。
func testTs(t *testing.T) (int64, int64, time.Time) {
	t.Helper()
	base := time.Date(2026, 9, 2, 12, 0, 0, 0, time.UTC)
	return base.UnixMilli(), base.UnixMilli() + 30_000, base.Add(5 * time.Minute)
}

func mkObs(ts int64, side string, ok bool, fill float64) *Observation {
	stake := 2.0
	o := &Observation{Ts: ts, Side: side, Rem: 250, Fill: fill, M45: 0.55, DistS: -0.2, OK: ok}
	if ok {
		o.Shares = stake / fill
	}
	return o
}

// readDay 把当日文件读成 map[conditionID]Record（校验落盘内容用）。
func readDay(t *testing.T, dir, date string) map[string]Record {
	t.Helper()
	f, err := os.Open(recordFilePath(dir, date))
	if err != nil {
		t.Fatalf("打开 %s: %v", date, err)
	}
	defer f.Close()
	out := map[string]Record{}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		var rec Record
		if err := json.Unmarshal(sc.Bytes(), &rec); err != nil {
			t.Fatalf("行解析失败: %v", err)
		}
		out[rec.ConditionID] = rec
	}
	return out
}

func approxEq(a, b float64) bool { return a-b < 1e-9 && b-a < 1e-9 }

func TestRecordObservation(t *testing.T) {
	dir := t.TempDir()
	r, err := NewRecorder(dir)
	if err != nil {
		t.Fatal(err)
	}
	ts, _, _ := testTs(t)

	// ok 信号
	okRec, err := r.RecordObservation("cond-a", "btc-updown-5m-0", 1_760_000_000, mkObs(ts, SideYes, true, 0.19), 2)
	if err != nil {
		t.Fatal(err)
	}
	if okRec.EventType != recordEventType || okRec.Date != "2026-09-02" {
		t.Fatalf("记录元信息: %+v", okRec)
	}
	// 失败观测
	if _, err := r.RecordObservation("cond-b", "btc-updown-5m-1", 1_760_000_300, mkObs(ts+1000, SideNo, false, 0.15), 2); err != nil {
		t.Fatal(err)
	}

	if obs, sig, _ := r.Counts(); obs != 2 || sig != 1 {
		t.Fatalf("Counts = %d/%d, 期望 2/1", obs, sig)
	}
	if len(r.PendingSignals()) != 1 {
		t.Fatal("pending 应 1 条")
	}
	if _, err := os.Stat(recordFilePath(dir, "2026-09-02")); err != nil {
		t.Fatalf("日文件未创建: %v", err)
	}
	if err := r.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestHasRecord(t *testing.T) {
	dir := t.TempDir()
	r, err := NewRecorder(dir)
	if err != nil {
		t.Fatal(err)
	}
	ts, _, _ := testTs(t)

	if r.HasRecord("cond-a") {
		t.Fatal("空记录时应 false")
	}
	// ok 与否决观测都算「已有记录」（快速重启防重入判据: 只要该窗触发过即跳窗）
	if _, err := r.RecordObservation("cond-a", "btc-updown-5m-0", ts, mkObs(ts, SideYes, true, 0.19), 2); err != nil {
		t.Fatal(err)
	}
	if _, err := r.RecordObservation("cond-b", "btc-updown-5m-1", ts+1000, mkObs(ts+1000, SideNo, false, 0.15), 2); err != nil {
		t.Fatal(err)
	}
	if !r.HasRecord("cond-a") || !r.HasRecord("cond-b") {
		t.Fatal("已记录窗口应 true")
	}
	if r.HasRecord("cond-c") {
		t.Fatal("未记录窗口应 false")
	}

	// 重启（磁盘恢复）后判据保持
	if err := r.Close(); err != nil {
		t.Fatal(err)
	}
	r2, err := NewRecorder(dir)
	if err != nil {
		t.Fatal(err)
	}
	if !r2.HasRecord("cond-a") || r2.HasRecord("cond-c") {
		t.Fatal("重启后 HasRecord 应与磁盘一致")
	}
}

func TestResolveMapping(t *testing.T) {
	dir := t.TempDir()
	r, _ := NewRecorder(dir)
	tsA, tsB, at := testTs(t)

	// yes 狗 + outcome 0(Up) → 赢
	r.RecordObservation("cond-a", "slug", tsA, mkObs(tsA, SideYes, true, 0.19), 2)
	// no 狗 + outcome 1(Down) → 赢
	r.RecordObservation("cond-b", "slug", tsB, mkObs(tsB, SideNo, true, 0.19), 2)
	// no 狗 + outcome 0(Up) → 输
	r.RecordObservation("cond-c", "slug", tsB+1000, mkObs(tsB+1000, SideNo, true, 0.19), 2)

	if !r.Resolve("cond-a", OutcomeUp, at) {
		t.Fatal("cond-a 应命中")
	}
	if !r.Resolve("cond-b", OutcomeDown, at) {
		t.Fatal("cond-b 应命中")
	}
	if !r.Resolve("cond-c", OutcomeUp, at) {
		t.Fatal("cond-c 应命中")
	}
	if r.Resolve("cond-a", OutcomeDown, at) {
		t.Fatal("重复结算应 false")
	}
	if len(r.PendingSignals()) != 0 {
		t.Fatal("pending 应清空")
	}

	wantWin, wantLose := 2/0.19-2, -2.0
	day := readDay(t, dir, "2026-09-02")
	for _, id := range []string{"cond-a", "cond-b"} {
		rec := day[id]
		if rec.Won == nil || !*rec.Won || !approxEq(rec.PnL, wantWin) || rec.ResolvedAt == "" {
			t.Fatalf("%s 应为赢: %+v", id, rec)
		}
	}
	if rec := day["cond-c"]; rec.Won == nil || *rec.Won || !approxEq(rec.PnL, wantLose) {
		t.Fatalf("cond-c 应为输: %+v", rec)
	}
}

func TestDailyPnlAndDrawdown(t *testing.T) {
	dir := t.TempDir()
	r, _ := NewRecorder(dir)
	tsA, tsB, at := testTs(t)
	// 先输后赢（按 ts 序 → 结算序一致）
	r.RecordObservation("lose", "slug", tsA, mkObs(tsA, SideNo, true, 0.19), 2)
	r.RecordObservation("win", "slug", tsB, mkObs(tsB, SideYes, true, 0.19), 2)
	r.Resolve("lose", OutcomeUp, at) // no 狗 + Up → 输 −2
	r.Resolve("win", OutcomeUp, at)  // yes 狗 + Up → 赢 shares−2

	dp := r.DailyPnl()
	if len(dp) != 1 || dp[0].N != 2 || !approxEq(dp[0].PnL, 2/0.19-4) {
		t.Fatalf("DailyPnl = %+v", dp)
	}
	if dd := r.MaxDrawdown(); !approxEq(dd, -2) {
		t.Fatalf("MaxDrawdown = %v, 期望 −2（先输 −2 后赢回）", dd)
	}
}

func TestRestartRecovery(t *testing.T) {
	dir := t.TempDir()
	ts, _, _ := testTs(t)

	r1, err := NewRecorder(dir)
	if err != nil {
		t.Fatal(err)
	}
	r1.RecordObservation("cond-a", "slug", ts, mkObs(ts, SideYes, true, 0.19), 2)
	r1.Close() // 未结算即"崩溃"退出

	r2, err := NewRecorder(dir) // 重启
	if err != nil {
		t.Fatal(err)
	}
	defer r2.Close()
	if len(r2.Observations()) != 1 || len(r2.PendingSignals()) != 1 {
		t.Fatalf("重启恢复: obs/pending = %d/%d, 期望 1/1",
			len(r2.Observations()), len(r2.PendingSignals()))
	}
	if !r2.Resolve("cond-a", OutcomeUp, ts2time(ts)) {
		t.Fatal("重启后应能结算")
	}
	day := readDay(t, dir, "2026-09-02")
	if rec := day["cond-a"]; rec.Won == nil || !*rec.Won {
		t.Fatalf("重启结算后文件应回填: %+v", rec)
	}
}

func ts2time(ms int64) time.Time { return time.UnixMilli(ms) }

func TestDaySplit(t *testing.T) {
	dir := t.TempDir()
	r, _ := NewRecorder(dir)
	tsA, _, _ := testTs(t)
	nextDay := time.Date(2026, 9, 3, 0, 1, 0, 0, time.UTC).UnixMilli()

	r.RecordObservation("a", "slug", tsA, mkObs(tsA, SideYes, false, 0.19), 2)
	r.RecordObservation("b", "slug", nextDay, mkObs(nextDay, SideYes, false, 0.19), 2)

	for _, d := range []string{"2026-09-02", "2026-09-03"} {
		if _, err := os.Stat(recordFilePath(dir, d)); err != nil {
			t.Fatalf("%s 文件缺失: %v", d, err)
		}
	}
	if len(r.Observations()) != 2 {
		t.Fatalf("跨日记录数 = %d, 期望 2", len(r.Observations()))
	}
}

func TestSchemaGuard(t *testing.T) {
	dir := t.TempDir()
	ts, _, _ := testTs(t)

	// 先放一条旧格式（v3 crosses_ 语义）与一条合法行
	legacy := []byte(`{"event_type":"cross","ts":1,"ok":true,"won":true,"pnl":9}` + "\n")
	valid, _ := json.Marshal(&Record{Observation: *mkObs(ts, SideYes, true, 0.19),
		EventType: recordEventType, Date: "2026-09-02", ConditionID: "c-ok"})
	if err := os.WriteFile(recordFilePath(dir, "2026-09-02"), append(legacy, append(valid, '\n')...), 0o644); err != nil {
		t.Fatal(err)
	}

	r, err := NewRecorder(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	// 旧格式行被跳过（不落 failed 桶、不污染统计）
	obs, sig, won := r.Counts()
	if obs != 1 || sig != 1 || won != 0 {
		t.Fatalf("Counts = %d/%d/%d, 期望 1/1/0（旧格式应跳过）", obs, sig, won)
	}
	if len(r.PendingSignals()) != 1 || len(r.Observations()) != 1 {
		t.Fatal("pending/observations 应只含合法行")
	}
}

func TestWonFor(t *testing.T) {
	cases := []struct {
		side    string
		outcome int
		want    bool
	}{
		{SideYes, OutcomeUp, true},
		{SideYes, OutcomeDown, false},
		{SideNo, OutcomeDown, true},
		{SideNo, OutcomeUp, false},
	}
	for _, c := range cases {
		if got := WonFor(c.side, c.outcome); got != c.want {
			t.Fatalf("WonFor(%s,%d) = %v, 期望 %v", c.side, c.outcome, got, c.want)
		}
	}
}

func TestFileNaming(t *testing.T) {
	dir := t.TempDir()
	_ = dir
	if got := filepath.Base(recordFilePath("/x", "2026-09-02")); got != "touches_2026-09-02.jsonl" {
		t.Fatalf("文件名 = %s", got)
	}
}

// 窗口振幅日志（σ 本地预热数据源）: 跨日切分写盘 + 重启后载入恢复。
func TestWindowLogRoundtrip(t *testing.T) {
	dir := t.TempDir()
	r, err := NewRecorder(dir)
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}

	// 4 窗, 时间正序; 前两窗收在 09-02, 后两窗收在 09-03（验证跨日文件切分）
	ends := []time.Time{
		time.Date(2026, 9, 2, 23, 57, 0, 0, time.UTC),
		time.Date(2026, 9, 2, 23, 59, 0, 0, time.UTC),
		time.Date(2026, 9, 3, 0, 2, 0, 0, time.UTC),
		time.Date(2026, 9, 3, 0, 4, 0, 0, time.UTC),
	}
	for i, end := range ends {
		anchor := 100.0
		close_ := 100.0 + float64(i+1)
		err := r.LogWindowAmplitude("0x"+string(rune('a'+i)), "btc-updown-5m", end.Unix()-300,
			end, anchor, close_, close_-anchor)
		if err != nil {
			t.Fatalf("LogWindowAmplitude[%d]: %v", i, err)
		}
	}

	// 文件切分: 09-02 两行 / 09-03 两行（touches 文件不受影响）
	for date, want := range map[string]int{"2026-09-02": 2, "2026-09-03": 2} {
		n := countLines(t, windowFilePath(dir, date))
		if n != want {
			t.Fatalf("%s 窗口行数 = %d, 期望 %d", date, n, want)
		}
	}
	if n := countLines(t, recordFilePath(dir, "2026-09-02")); n != 0 {
		t.Fatalf("touches 文件不应被窗口日志写入, 行数 = %d", n)
	}

	wins := r.RecentWindows(18)
	if len(wins) != 4 {
		t.Fatalf("RecentWindows(18) = %d, 期望 4", len(wins))
	}
	for i, w := range wins {
		if w.Amp != float64(i+1) || w.Date != utcDate(w.Ts) {
			t.Fatalf("wins[%d].Amp = %v / date=%s, 期望 %v", i, w.Amp, w.Date, float64(i+1))
		}
	}

	// 模拟重启: Close 后重新 NewRecorder, 窗口应从磁盘恢复
	if err := r.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	r2, err := NewRecorder(dir)
	if err != nil {
		t.Fatalf("重启 NewRecorder: %v", err)
	}
	defer r2.Close()
	wins2 := r2.RecentWindows(18)
	if len(wins2) != 4 {
		t.Fatalf("重启后 RecentWindows = %d, 期望 4", len(wins2))
	}
	last := r2.RecentWindows(2)
	if len(last) != 2 || last[0].Amp != 3 || last[1].Amp != 4 {
		t.Fatalf("重启后最近 2 窗 = %+v, 期望 amp [3 4]", last)
	}
}

// ── live 两阶段落盘（2026-09-10 实盘接入）──

func TestLiveTwoPhaseFill(t *testing.T) {
	dir := t.TempDir()
	r, err := NewRecorder(dir)
	if err != nil {
		t.Fatal(err)
	}
	ts, _, _ := testTs(t)

	// 阶段 1: submitting 行落盘, 不入 pending
	sub, err := r.SubmitLiveObservation("cond-live", "slug", ts/1000, mkObs(ts, SideYes, true, 0.19), 2)
	if err != nil {
		t.Fatal(err)
	}
	if sub.ExecStatus != ExecStatusSubmitting || sub.IsFilled() {
		t.Fatalf("submitting 行状态: %+v", sub)
	}
	if len(r.PendingSignals()) != 0 {
		t.Fatal("submitting 行不应入 pending")
	}

	// 阶段 2: 回填 filled（部分成交口径: 实际 10.20 股 @0.19 → cost 1.938）
	res := ExecResult{Status: ExecStatusPartial, OrderID: "ord-1", FillPrice: 0.19, Shares: 10.20, Cost: 1.938}
	rec, filled, err := r.CompleteExecution("cond-live", res)
	if err != nil {
		t.Fatal(err)
	}
	if !filled || rec.ExecStatus != ExecStatusPartial || rec.OrderID != "ord-1" {
		t.Fatalf("回填结果: %+v", rec)
	}
	if !approxEq(rec.Shares, 10.20) || !approxEq(rec.Cost, 1.938) || !approxEq(rec.FillPrice, 0.19) {
		t.Fatalf("实际成交字段: %+v", rec)
	}
	if len(r.PendingSignals()) != 1 {
		t.Fatal("filled 行应入 pending")
	}

	// 磁盘回填: 当日文件行已含 exec 字段
	day := readDay(t, dir, "2026-09-02")
	onDisk := day["cond-live"]
	if onDisk.ExecStatus != ExecStatusPartial || onDisk.OrderID != "ord-1" || !approxEq(onDisk.Cost, 1.938) {
		t.Fatalf("磁盘行未回填: %+v", onDisk)
	}
	if onDisk.Won != nil {
		t.Fatal("磁盘行不应已结算")
	}

	// 结算按真实 cost: 赢 → shares − cost（每股兑 1U）
	if !r.Resolve("cond-live", OutcomeUp, ts2time(ts)) {
		t.Fatal("应能结算")
	}
	if rec.Won == nil || !*rec.Won || !approxEq(rec.PnL, 10.20-1.938) {
		t.Fatalf("cost 口径结算: %+v", rec)
	}
	if err := r.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestLiveUnfilledNoPending(t *testing.T) {
	dir := t.TempDir()
	r, _ := NewRecorder(dir)
	ts, _, _ := testTs(t)

	// unfilled（0 成交）: 不入 pending, 不注册结算; 目标股数保留
	r.SubmitLiveObservation("cond-u", "slug", ts/1000, mkObs(ts, SideYes, true, 0.19), 2)
	rec, filled, err := r.CompleteExecution("cond-u", ExecResult{Status: ExecStatusUnfilled, OrderID: "ord-u", Note: "no liquidity"})
	if err != nil {
		t.Fatal(err)
	}
	if filled || rec.IsFilled() {
		t.Fatal("unfilled 不应成交")
	}
	if len(r.PendingSignals()) != 0 {
		t.Fatal("unfilled 不应入 pending")
	}
	if r.Resolve("cond-u", OutcomeUp, ts2time(ts)) {
		t.Fatal("unfilled 不应能结算")
	}

	// rejected（HTTP 拒单）同理
	r.SubmitLiveObservation("cond-r", "slug", ts/1000, mkObs(ts, SideYes, true, 0.19), 2)
	rec, _, err = r.CompleteExecution("cond-r", ExecResult{Status: ExecStatusRejected, OrderID: "", Note: "balance insufficient"})
	if err != nil {
		t.Fatal(err)
	}
	if rec.IsFilled() || len(r.PendingSignals()) != 0 {
		t.Fatalf("rejected 不应成交/入 pending: %+v", rec)
	}
	// 目标股数保留（stake/fill = 2/0.19）
	if !approxEq(rec.Shares, 2/0.19) {
		t.Fatalf("非成交行应保留目标股数: %v", rec.Shares)
	}

	// 重复回填 / 行缺失 → error
	if _, _, err := r.CompleteExecution("cond-u", ExecResult{Status: ExecStatusFilled, Shares: 1}); err == nil {
		t.Fatal("重复回填应 error")
	}
	if _, _, err := r.CompleteExecution("cond-none", ExecResult{Status: ExecStatusFilled, Shares: 1}); err == nil {
		t.Fatal("行缺失应 error")
	}
}

func TestLiveRiskGateRejectedRow(t *testing.T) {
	dir := t.TempDir()
	r, _ := NewRecorder(dir)
	ts, _, _ := testTs(t)

	// 风控闸拒绝（未发起下单）: 直接落 rejected 行, 不入 pending
	rec, err := r.RecordLiveRejected("cond-g", "slug", ts/1000, mkObs(ts, SideYes, true, 0.19), 2, "日亏熔断")
	if err != nil {
		t.Fatal(err)
	}
	if !rec.OK || rec.ExecStatus != ExecStatusRejected || rec.IsFilled() {
		t.Fatalf("闸拒行: %+v", rec)
	}
	if len(r.PendingSignals()) != 0 {
		t.Fatal("闸拒行不应入 pending")
	}
	obs, sig, _ := r.Counts()
	if obs != 1 || sig != 1 {
		t.Fatalf("闸拒行仍应计入观测/信号口径（09-15 频率对照）: %d/%d", obs, sig)
	}
}

func TestRestartReconcileScan(t *testing.T) {
	dir := t.TempDir()
	ts, _, _ := testTs(t)
	mk := func(condID string, status, note string, ok bool, won *bool) *Record {
		rec := newRecord(condID, "slug", ts/1000, mkObs(ts, SideYes, ok, 0.19), 2)
		rec.ExecStatus = status
		rec.ExecNote = note
		rec.Won = won
		rec.OrderID = "ord-" + condID
		return rec
	}
	wonTrue := true
	lines := []*Record{
		mk("c-sub", ExecStatusSubmitting, "", true, nil),                        // 下单后崩溃 → 核对
		mk("c-unk", ExecStatusRejected, ExecNoteUnknown+": POST 超时", true, nil), // 结果不明 → 核对
		mk("c-rej", ExecStatusRejected, "balance insufficient", true, nil),      // 明确拒单 → 不核对
		mk("c-ok", "", "", true, nil),                                           // paper 待结算 → pending
		mk("c-fill", ExecStatusFilled, "", true, nil),                           // live 成交待结算 → pending
		mk("c-done", ExecStatusFilled, "", true, &wonTrue),                      // 已结算 → 不挂
	}
	var buf []byte
	for _, rec := range lines {
		b, err := json.Marshal(rec)
		if err != nil {
			t.Fatal(err)
		}
		buf = append(buf, b...)
		buf = append(buf, '\n')
	}
	if err := os.WriteFile(recordFilePath(dir, "2026-09-02"), buf, 0o644); err != nil {
		t.Fatal(err)
	}

	r, err := NewRecorder(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()

	if got := r.NeedsReconcile(); got != 2 {
		t.Fatalf("NeedsReconcile = %d, 期望 2（submitting + unknown-rejected）", got)
	}
	pending := r.PendingSignals()
	if len(pending) != 2 {
		t.Fatalf("pending = %d, 期望 2（paper + live filled）", len(pending))
	}
	for _, p := range pending {
		if p.ConditionID != "c-ok" && p.ConditionID != "c-fill" {
			t.Fatalf("pending 意外含 %s", p.ConditionID)
		}
	}
}

// countLines 数一个文件的行数（不存在返回 0）。
func countLines(t *testing.T, path string) int {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return 0
		}
		t.Fatalf("读 %s: %v", path, err)
	}
	if len(b) == 0 {
		return 0
	}
	n := 0
	for _, c := range b {
		if c == '\n' {
			n++
		}
	}
	return n
}
