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
