package tail

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/necklace/flip-signal/internal/flip"
)

// ── 纯函数（官方公式）──

// TestFeeOfficialTable 官方文档对照表（100 股, Crypto rate=0.07, 5 位小数）:
// https://docs.polymarket.com/trading/fees.md 的 100-share 表逐格核对。
func TestFeeOfficialTable(t *testing.T) {
	cases := []struct{ p, want float64 }{
		{0.01, 0.0693},
		{0.05, 0.3325},
		{0.10, 0.63},
		{0.20, 1.12},
		{0.30, 1.47},
		{0.40, 1.68},
		{0.50, 1.75},
		{0.99, 0.0693},
	}
	for _, c := range cases {
		if got := Fee(0.07, 100, c.p); !near(got, c.want) {
			t.Errorf("Fee(0.07, 100, %.2f) = %v, 官方表 %v", c.p, got, c.want)
		}
	}
}

// TestFeeSymmetryAndRounding 对称性（p 与 1−p 同费）与 5 位小数舍入。
func TestFeeSymmetryAndRounding(t *testing.T) {
	for _, p := range []float64{0.01, 0.13, 0.30, 0.47, 0.5, 0.85, 0.99} {
		if a, b := Fee(0.07, 37.5, p), Fee(0.07, 37.5, 1-p); !near(a, b) {
			t.Errorf("对称性: Fee(%.2f)=%v ≠ Fee(%.2f)=%v", p, a, 1-p, b)
		}
	}
	// 13 股 @0.37: raw = 13×0.07×0.37×0.63 = 0.212121 → 第 6 位小数舍去。
	if got := Fee(0.07, 13, 0.37); !near(got, 0.21212) {
		t.Errorf("5 位舍入: got %v, want 0.21212", got)
	}
	// 最小非零手续费 0.00001: 0.005 股 @0.5 → raw 0.0000875 → 0.00009。
	if got := Fee(0.07, 0.005, 0.5); !near(got, 0.00009) {
		t.Errorf("最小档: got %v, want 0.00009", got)
	}
	// 比最小档更小的金额舍到 0（官方: 最小 0.00001 USDC）。
	if got := Fee(0.07, 0.0001, 0.5); got != 0 {
		t.Errorf("量子以下应舍为 0, got %v", got)
	}
}

// TestFeeGuards 非法入参一律 0（「不收费」是安全方向——宁可少记不能错记）。
func TestFeeGuards(t *testing.T) {
	cases := []struct {
		name                string
		rate, shares, price float64
	}{
		{"费率 0（关闭计费）", 0, 100, 0.5},
		{"费率负", -0.07, 100, 0.5},
		{"股数 0", 0.07, 0, 0.5},
		{"股数负", 0.07, -5, 0.5},
		{"价格 0", 0.07, 100, 0},
		{"价格负", 0.07, 100, -0.1},
		{"价格 1（无不确定性即无费）", 0.07, 100, 1},
		{"价格越界", 0.07, 100, 1.2},
	}
	for _, c := range cases {
		if got := Fee(c.rate, c.shares, c.price); got != 0 {
			t.Errorf("%s: got %v, want 0", c.name, got)
		}
	}
}

// ── Recorder 集成（记账口径）──

// recByCond 从对外读口按 conditionID 取行（⚠️ 返回的是**字段副本**——与
// recorder_test.go 的既有告诫一致, 断言前必须重新取, 不能拿旧指针）。
func recByCond(t *testing.T, r *Recorder, cond string) *Record {
	t.Helper()
	for _, o := range r.Observations() {
		if o.ConditionID == cond {
			return o
		}
	}
	t.Fatalf("未找到行 %s", cond)
	return nil
}

// writeLegacyRecords 手写一份「决策 #35 之前」形态的行文件: 直接 marshal Record
// ——TakerShares/TakerPrice/Fee 都是 omitempty, 零值天然不落键, 正是老行的形状。
func writeLegacyRecords(t *testing.T, dir string, recs ...Record) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	for _, rec := range recs {
		b, err := json.Marshal(rec)
		if err != nil {
			t.Fatal(err)
		}
		buf.Write(b)
		buf.WriteByte('\n')
	}
	if err := os.WriteFile(filepath.Join(dir, "tail_2026-09-22.jsonl"), buf.Bytes(), 0o644); err != nil {
		t.Fatal(err)
	}
}

// TestRecorderFeeBuyFilled 买入即时全额成交（taker）: CompleteExecution 落 Fee,
// 结算 P&L 扣费, 三键随落盘。
func TestRecorderFeeBuyFilled(t *testing.T) {
	dir := t.TempDir()
	r, err := NewRecorder(dir, 0.07)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	ts := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC).UnixMilli()

	if _, err := r.SubmitLiveObservation("0xf1", "s", 1780000000, okSnap(ts, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	shares := 2 / 0.92
	rec, filled, err := r.CompleteExecution("0xf1", flip.ExecResult{
		Status: flip.ExecStatusFilled, OrderID: "o1",
		FillPrice: 0.92, Shares: shares, Cost: 2,
		TakerShares: shares, TakerPrice: 0.92, // POST 即时撮合 = taker
	})
	if err != nil || !filled {
		t.Fatalf("filled=%v err=%v", filled, err)
	}
	wantFee := Fee(0.07, shares, 0.92) // = 2×0.07×0.92×0.08 = 0.0112
	if !near(rec.Fee, wantFee) || !near(rec.TakerShares, shares) || rec.TakerPrice != 0.92 {
		t.Fatalf("买入费记账不对: fee=%.6f taker=%.4f@%.4f, want %.6f", rec.Fee, rec.TakerShares, rec.TakerPrice, wantFee)
	}
	// 结算: 赢 = shares − cost − fee（费从 P&L 里扣除, 决策 #35）。
	if !r.Resolve("0xf1", flip.OutcomeUp, time.UnixMilli(ts+600000), "push") {
		t.Fatal("Resolve 应命中")
	}
	got := recByCond(t, r, "0xf1")
	if !near(got.PnL, shares-2-wantFee) {
		t.Fatalf("PnL = %.6f, want %.6f（应扣 %.6f 费）", got.PnL, shares-2-wantFee, wantFee)
	}
	// 磁盘: fee/taker_shares/taker_price 三键落盘（omitempty 只在 0 时省略）。
	var m map[string]any
	for _, l := range readLines(t, filepath.Join(dir, "tail_"+utcDate(ts)+".jsonl")) {
		if err := json.Unmarshal([]byte(l), &m); err != nil {
			t.Fatal(err)
		}
	}
	for _, k := range []string{"fee", "taker_shares", "taker_price"} {
		if _, ok := m[k]; !ok {
			t.Fatalf("落盘缺键 %q: %v", k, m)
		}
	}
}

// TestRecorderFeeRestingPartialThenFinal 挂单型: POST 即时部分成交（taker）当场
// 计费; FillTracker 定稿的累计成交量（含后来的 maker 部分）**不得**重复计费。
func TestRecorderFeeRestingPartialThenFinal(t *testing.T) {
	dir := t.TempDir()
	r, err := NewRecorder(dir, 0.07)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	ts := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC).UnixMilli()

	if _, err := r.SubmitLiveObservation("0xf2", "s", 1780000000, okSnap(ts, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	// POST 即时 1.0 股 @0.92 成交 + 余量挂单在簿（resting: 不写 Shares/Cost）。
	rec, filled, err := r.CompleteExecution("0xf2", flip.ExecResult{
		Status: flip.ExecStatusResting, OrderID: "o2",
		Note:        "GTC 即时成交 1.00/2.17 股 @0.9200, 余量挂单在簿",
		TakerShares: 1.0, TakerPrice: 0.92,
	})
	if err != nil || filled {
		t.Fatalf("resting 不算成交: filled=%v err=%v", filled, err)
	}
	buyFee := Fee(0.07, 1.0, 0.92)
	if rec.Cost != 0 || !near(rec.Fee, buyFee) {
		t.Fatalf("resting 应只记费不记仓位: cost=%.2f fee=%.6f want %.6f", rec.Cost, rec.Fee, buyFee)
	}
	// 定稿: 累计 2.0 股、均价 0.90（含后来在簿被吃到的 maker 部分, 0 费）。
	if _, err := r.CompleteRestingFill(flip.FillFinal{
		ConditionID: "0xf2", Status: flip.ExecStatusPartial, Shares: 2.0, Cost: 1.8, Note: "余量已撤",
	}); err != nil {
		t.Fatal(err)
	}
	got := recByCond(t, r, "0xf2")
	if !near(got.Fee, buyFee) {
		t.Fatalf("定稿不得重复计费: fee=%.6f, want %.6f（maker 部分 0 费）", got.Fee, buyFee)
	}
	if !near(got.Shares, 2.0) || !near(got.Cost, 1.8) {
		t.Fatalf("定稿仓位字段: shares=%.4f cost=%.4f", got.Shares, got.Cost)
	}
	// 结算: 赢 = 2.0 − 1.8 − fee。
	r.Resolve("0xf2", flip.OutcomeUp, time.UnixMilli(ts+600000), "")
	got = recByCond(t, r, "0xf2")
	if !near(got.PnL, 2.0-1.8-buyFee) {
		t.Fatalf("PnL = %.6f, want %.6f", got.PnL, 2.0-1.8-buyFee)
	}
}

// TestRecorderFeeMakerOnlyZero 纯挂单（POST 0 成交）定稿成 filled: 全部为 maker
// ⇒ 费恒 0。
func TestRecorderFeeMakerOnlyZero(t *testing.T) {
	dir := t.TempDir()
	r, err := NewRecorder(dir, 0.07)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	ts := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC).UnixMilli()

	r.SubmitLiveObservation("0xf3", "s", 1780000000, okSnap(ts, flip.SideYes), 2)
	if _, _, err := r.CompleteExecution("0xf3", flip.ExecResult{
		Status: flip.ExecStatusResting, OrderID: "o3", Note: "GTC 挂单在簿（即时 0 成交）",
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := r.CompleteRestingFill(flip.FillFinal{
		ConditionID: "0xf3", Status: flip.ExecStatusFilled, Shares: 2.17, Cost: 2.0, Note: "闭市撤单定稿",
	}); err != nil {
		t.Fatal(err)
	}
	got := recByCond(t, r, "0xf3")
	if got.Fee != 0 || got.TakerShares != 0 {
		t.Fatalf("maker 成交不得计费: fee=%.6f taker=%.4f", got.Fee, got.TakerShares)
	}
}

// TestRecorderFeePaperZero 纸面行没有任何真实订单 ⇒ 买卖都不计费（买入走
// RecordObservation、卖出走 RecordExit 的 paper 分支, 都不能记费）。
func TestRecorderFeePaperZero(t *testing.T) {
	dir := t.TempDir()
	r, err := NewRecorder(dir, 0.07) // 费率开着, 纸面照样 0
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	ts := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC).UnixMilli()

	rec, err := r.RecordObservation("0xf4", "s", 1780000000, okSnap(ts, flip.SideYes), 2)
	if err != nil {
		t.Fatal(err)
	}
	if rec.Fee != 0 || rec.TakerShares != 0 {
		t.Fatalf("纸面买入不得计费: fee=%.6f", rec.Fee)
	}
	// 纸面止损卖出: ExitShares 照记, 费仍 0。
	if _, err := r.RecordExit("0xf4", 1.0, 0.26, 40, ts); err != nil {
		t.Fatal(err)
	}
	got := recByCond(t, r, "0xf4")
	if !near(got.ExitShares, 1.0) || got.Fee != 0 {
		t.Fatalf("纸面卖出应记仓位但不计费: exit=%.4f fee=%.6f", got.ExitShares, got.Fee)
	}
}

// TestRecorderFeeLiveSell 止损卖出的 FAK 成交恒 taker: 卖费累加在买费之上,
// P&L 同时扣两笔。
func TestRecorderFeeLiveSell(t *testing.T) {
	dir := t.TempDir()
	r, err := NewRecorder(dir, 0.07)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	ts := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC).UnixMilli()

	shares := 2 / 0.92
	r.SubmitLiveObservation("0xf5", "s", 1780000000, okSnap(ts, flip.SideYes), 2)
	if _, _, err := r.CompleteExecution("0xf5", flip.ExecResult{
		Status: flip.ExecStatusFilled, OrderID: "o5",
		FillPrice: 0.92, Shares: shares, Cost: 2,
		TakerShares: shares, TakerPrice: 0.92,
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := r.RecordExit("0xf5", 1.0, 0.26, 40, ts); err != nil {
		t.Fatal(err)
	}
	got := recByCond(t, r, "0xf5")
	wantFee := Fee(0.07, shares, 0.92) + Fee(0.07, 1.0, 0.26)
	if !near(got.Fee, wantFee) {
		t.Fatalf("买+卖费 = %.6f, want %.6f", got.Fee, wantFee)
	}
	if !near(got.TakerShares, shares+1.0) {
		t.Fatalf("taker 股数应含卖出: %.4f, want %.4f", got.TakerShares, shares+1.0)
	}
	// 结算（官方 Up, 押 yes ⇒ 赢）: 卖出所得 + 剩余兑付 − cost − 费。
	r.Resolve("0xf5", flip.OutcomeUp, time.UnixMilli(ts+600000), "")
	got = recByCond(t, r, "0xf5")
	want := 1.0*0.26 + (shares - 1.0) - 2 - wantFee
	if !near(got.PnL, want) {
		t.Fatalf("PnL = %.6f, want %.6f", got.PnL, want)
	}
}

// TestRecorderFeeBackfill 老行回填（决策 #35 之前的实盘行没有 fee 键）:
// 只认「filled ∧ note 空 ∧ 未记过 taker」这一种可恢复形态; 挂单定稿行（note 非空）
// 与 paper 行不计; 卖出侧按 exit_shares×exit_price 反推; 幂等、不回写磁盘。
func TestRecorderFeeBackfill(t *testing.T) {
	dir := t.TempDir()
	shares := 2 / 0.92
	won := true

	// a: 即时全额成交的老行 → 回填买入费。
	a := Record{
		Observation: Observation{Kind: KindSnap, Stage: StageT60, Ts: 1780000255000, OK: true, Shares: shares},
		EventType:   recordEventType, Date: "2026-09-22", ConditionID: "0xfa",
		ExecStatus: flip.ExecStatusFilled, FillPrice: 0.92, Cost: 2,
	}
	// b: 挂单定稿成交（note 非空）→ 即时部分已不可恢复, 不计（系统性低估）。
	b := a
	b.ConditionID, b.ExecNote = "0xfb", "闭市撤单定稿（size_matched=2.00）"
	// c: 已结算 + 止损卖出过的老行 → 买费 + 卖 1.0 股 @0.26 的费; PnL 载入时重算。
	c := a
	c.ConditionID, c.Won = "0xfc", &won
	c.ExitShares, c.ExitPrice, c.ExitRem = 1.0, 0.26, 40
	c.PnL = 1.0*0.26 + (shares - 1.0) - 2 // 旧值（未扣费）——载入后应被重算覆盖
	// d: paper 行（无 exec_status）→ 恒 0。
	d := Record{
		Observation: Observation{Kind: KindSnap, Stage: StageT60, Ts: 1780000255000, OK: true, Shares: shares},
		EventType:   recordEventType, Date: "2026-09-22", ConditionID: "0xfd",
		Won: &won,
	}
	d.PnL = shares - 2
	// e: 新格式行（已有 fee, TakerShares 非零）→ 原样保留。
	e := a
	e.ConditionID, e.TakerShares, e.TakerPrice, e.Fee = "0xfe", shares, 0.92, 0.5

	writeLegacyRecords(t, dir, a, b, c, d, e)

	r, err := NewRecorder(dir, 0.07)
	if err != nil {
		t.Fatal(err)
	}
	buyFee := Fee(0.07, shares, 0.92)
	sellFee := Fee(0.07, 1.0, 0.26)

	if got := recByCond(t, r, "0xfa"); !near(got.Fee, buyFee) || !near(got.TakerShares, shares) {
		t.Fatalf("0xfa 回填: fee=%.6f taker=%.4f, want %.6f/%.4f", got.Fee, got.TakerShares, buyFee, shares)
	}
	if got := recByCond(t, r, "0xfb"); got.Fee != 0 {
		t.Fatalf("挂单定稿行（note 非空）不得回填: fee=%.6f", got.Fee)
	}
	got := recByCond(t, r, "0xfc")
	if !near(got.Fee, buyFee+sellFee) {
		t.Fatalf("0xfc 买卖两笔回填: fee=%.6f, want %.6f", got.Fee, buyFee+sellFee)
	}
	if want := 1.0*0.26 + (shares - 1.0) - 2 - (buyFee + sellFee); !near(got.PnL, want) {
		t.Fatalf("0xfc 载入后 PnL 应重算扣费: %.6f, want %.6f", got.PnL, want)
	}
	if got := recByCond(t, r, "0xfd"); got.Fee != 0 {
		t.Fatalf("paper 行不得回填: fee=%.6f", got.Fee)
	}
	if got := recByCond(t, r, "0xfe"); !near(got.Fee, 0.5) {
		t.Fatalf("已带 fee 的新格式行必须原样保留: fee=%.6f", got.Fee)
	}
	// 磁盘未被回写（回填只写内存; 0xfa 行里的 fee 键仍然不存在——0xfe 行本来就带着 fee）。
	for _, l := range readLines(t, filepath.Join(dir, "tail_2026-09-22.jsonl")) {
		var m map[string]any
		if err := json.Unmarshal([]byte(l), &m); err != nil {
			t.Fatal(err)
		}
		if m["condition_id"] == "0xfa" {
			if _, ok := m["fee"]; ok {
				t.Fatalf("回填不得回写磁盘: %v", m)
			}
		}
	}

	// 幂等: 重开一次, 数字不叠加。
	r.Close()
	r2, err := NewRecorder(dir, 0.07)
	if err != nil {
		t.Fatal(err)
	}
	defer r2.Close()
	if got := recByCond(t, r2, "0xfc"); !near(got.Fee, buyFee+sellFee) {
		t.Fatalf("重复载入应幂等: fee=%.6f, want %.6f", got.Fee, buyFee+sellFee)
	}

	// 费率 0 = 关闭计费: 同一份数据回填不出任何费。
	dir2 := t.TempDir()
	writeLegacyRecords(t, dir2, a)
	r3, err := NewRecorder(dir2, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer r3.Close()
	if got := recByCond(t, r3, "0xfa"); got.Fee != 0 {
		t.Fatalf("费率 0 不得回填: fee=%.6f", got.Fee)
	}
}

// TestExecStateLiveFeeWiring 编排接线（决策 #35 的「不散落第二条路径」）: live 分支
// 必须把 ExecResult 的 taker 字段原样带进 Recorder——费率与记账只在 Recorder 一处。
func TestExecStateLiveFeeWiring(t *testing.T) {
	shares := 2 / 0.92
	ex := &fakeExec{res: flip.ExecResult{
		Status: flip.ExecStatusFilled, OrderID: "o-w",
		FillPrice: 0.92, Shares: shares, Cost: 2,
		TakerShares: shares, TakerPrice: 0.92,
	}}
	dir := t.TempDir()
	rec, err := NewRecorder(dir, 0.07)
	if err != nil {
		t.Fatal(err)
	}
	defer rec.Close()
	x := &ExecState{
		Rec: rec, Ex: ex, Live: true, Stake: 2, MaxDailyLoss: -24,
		Tokens: func() (string, string) { return "up-token", "down-token" },
	}
	r := x.HandleDecision(okSnap(time.Now().UnixMilli(), flip.SideYes), "0xw", "slug", 1780000000)
	if r == nil || !near(r.Fee, Fee(0.07, shares, 0.92)) {
		t.Fatalf("live 编排应把 taker 字段带进 Recorder: %+v", r)
	}
}
