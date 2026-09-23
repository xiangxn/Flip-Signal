package tail

import (
	"testing"
	"time"

	"github.com/necklace/flip-signal/internal/flip"
)

// fakeExec 记录调用并返回预设结果——用来钉住「帧行永不下单」这类编排红线。
type fakeExec struct {
	calls  int
	lastOb *flip.Observation
	token  string
	stake  float64
	res    flip.ExecResult
}

func (f *fakeExec) Execute(o *flip.Observation, tokenID string, stake float64) *flip.ExecResult {
	f.calls++
	f.lastOb, f.token, f.stake = o, tokenID, stake
	r := f.res
	if r.Status == "" {
		r = flip.ExecResult{Status: flip.ExecStatusFilled, FillPrice: o.Fill, Shares: o.Shares, Cost: stake}
	}
	return &r
}

func newTestExec(t *testing.T, live bool, ex flip.Executor) (*ExecState, *Recorder, string) {
	t.Helper()
	rec, dir := newTestRecorder(t)
	return &ExecState{
		Rec: rec, Ex: ex, Live: live, Stake: 2, MaxDailyLoss: -24,
		Tokens: func() (string, string) { return "up-token", "down-token" },
	}, rec, dir
}

// TestExecStateFrameNeverTrades 帧行只落盘、绝不下单——这是本品最危险的一条红线
// （帧在 rem≤150 就产出, 若走成执行路径就会在价格还没到 0.80 的窗口里下真单）。
func TestExecStateFrameNeverTrades(t *testing.T) {
	ex := &fakeExec{}
	x, rec, _ := newTestExec(t, true, ex)

	frame := &Observation{
		Kind: KindFrame, FrameT: 150, Ts: time.Now().UnixMilli(), Rem: 145,
		Side: flip.SideYes, HotAsk: 0.92, Dev: 100, Sd: 100, Anchor: 100000,
	}
	if got := x.HandleFrame(frame, "0xc", "slug", 1780000000); got == nil {
		t.Fatal("帧行应落盘")
	}
	if ex.calls != 0 {
		t.Fatalf("帧行不得触发下单, Ex 被调用 %d 次", ex.calls)
	}
	if n := len(rec.PendingSignals()); n != 0 {
		t.Fatalf("帧行不入 pending, 得到 %d", n)
	}
	// 重复帧（崩溃重入）去重: 不写第二行。
	if got := x.HandleFrame(frame, "0xc", "slug", 1780000000); got != nil {
		t.Fatal("同窗重复帧应被跳过")
	}
	if n := len(rec.Observations()); n != 1 {
		t.Fatalf("去重后应仍只有 1 行, 得到 %d", n)
	}
	// 误把帧行送进执行入口: 拒绝, 且不落盘不再下单。
	if got := x.HandleObservation(frame, "0xc", "slug", 1780000000); got != nil {
		t.Fatal("帧行进 HandleObservation 必须被拒")
	}
	if ex.calls != 0 || len(rec.Observations()) != 1 {
		t.Fatalf("被拒的帧行不该有副作用: calls=%d rows=%d", ex.calls, len(rec.Observations()))
	}
}

// TestExecStatePaper 纸面: ok 行落盘（无 exec 字段）+ 入 pending; 否决行也落盘。
func TestExecStatePaper(t *testing.T) {
	ex := &fakeExec{}
	x, rec, _ := newTestExec(t, false, ex)

	// 否决行（价格腿不过）: 照记, 不下单。
	loser := okSnap(time.Now().UnixMilli(), flip.SideYes)
	loser.OK, loser.Rules, loser.Shares = false, Rules{Price: false}, 0
	loser.RejectReason = RejectPriceLow
	r1 := x.HandleObservation(loser, "0xloser", "slug-l", 1780000000)
	if r1 == nil || r1.OK || ex.calls != 0 {
		t.Fatalf("否决行应落盘且不下单: rec=%v calls=%d", r1, ex.calls)
	}

	// ok 行: 模拟成交, 行里无 exec 字段（IsFilled 恒真）。
	r2 := x.HandleObservation(okSnap(time.Now().UnixMilli(), flip.SideNo), "0xok", "slug-o", 1780000000)
	if r2 == nil || !r2.IsFilled() || r2.ExecStatus != "" {
		t.Fatalf("纸面行应为「无 exec 字段 + IsFilled」: %+v", r2)
	}
	if ex.calls != 1 {
		t.Fatalf("ok 行应恰好下一次单, 得到 %d", ex.calls)
	}
	if ex.token != "" {
		t.Fatalf("纸面不该传 token（无真实订单）: %q", ex.token)
	}
	if ex.lastOb.Fill != 0.92 || !near(ex.lastOb.Shares, 2/0.92) {
		t.Fatalf("执行器契约应收到 Fill=hot_ask / Shares=目标股数: %+v", ex.lastOb)
	}
	if n := len(rec.PendingSignals()); n != 1 {
		t.Fatalf("纸面 ok 行应入 pending, 得到 %d", n)
	}
}

// TestExecStateLiveTwoPhase live: submitting 先行落盘 → 回填终态; resting 行改由
// FillTracker 定稿（applyFillFinal）后才入 pending。
func TestExecStateLiveTwoPhase(t *testing.T) {
	// ① 即时全额成交（filled）。
	ex := &fakeExec{res: flip.ExecResult{Status: flip.ExecStatusFilled, OrderID: "o1", FillPrice: 0.90, Shares: 2.22, Cost: 2.0}}
	x, rec, _ := newTestExec(t, true, ex)
	r := x.HandleObservation(okSnap(time.Now().UnixMilli(), flip.SideYes), "0xc", "slug", 1780000000)
	if r == nil || r.ExecStatus != flip.ExecStatusFilled || r.OrderID != "o1" {
		t.Fatalf("filled 回填不正确: %+v", r)
	}
	if ex.token != "up-token" {
		t.Fatalf("yes 侧应买 UP token, 得到 %q", ex.token)
	}
	if len(rec.PendingSignals()) != 1 {
		t.Fatal("filled 应入 pending 并注册结算")
	}

	// ② 挂单在簿（resting）: 不入 pending, 等 FillTracker 定稿。
	ex2 := &fakeExec{res: flip.ExecResult{Status: flip.ExecStatusResting, OrderID: "o2"}}
	x2, rec2, _ := newTestExec(t, true, ex2)
	o := okSnap(time.Now().UnixMilli(), flip.SideNo)
	r2 := x2.HandleObservation(o, "0xd", "slug-d", 1780000000)
	if r2 == nil || r2.ExecStatus != flip.ExecStatusResting || r2.OrderID != "o2" {
		t.Fatalf("resting 回填不正确: %+v", r2)
	}
	if ex2.token != "down-token" {
		t.Fatalf("no 侧应买 DOWN token, 得到 %q", ex2.token)
	}
	if len(rec2.PendingSignals()) != 0 {
		t.Fatal("resting 不入 pending（仓位未定）")
	}
	// FillTracker 定稿（闭市撤单后）→ 入 pending。
	got := x2.ApplyFillFinal(flip.FillFinal{ConditionID: "0xd", Status: flip.ExecStatusFilled, Shares: 2.17, Cost: 2.0, Note: "余量已撤"})
	if got == nil || !got.IsFilled() {
		t.Fatalf("定稿后应成交: %+v", got)
	}
	if len(rec2.PendingSignals()) != 1 {
		t.Fatal("定稿后应入 pending（调用方据此注册结算轮询）")
	}
}

// TestExecStateGate 风控闸两模式同判据: paper 被闸行照记照结算（只多 gate_reason）;
// live 被闸行记 rejected 且**不调用执行器**; first_window 只在 live 生效。
func TestExecStateGate(t *testing.T) {
	ts := time.Now().UnixMilli()

	// ① paper: 当日已亏过线 → 被闸, 但行照记、IsFilled 仍真、照常结算。
	ex := &fakeExec{}
	x, rec, _ := newTestExec(t, false, ex)
	if _, err := rec.RecordObservation("0xpre", "s", 0, okSnap(ts-1000, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	rec.Resolve("0xpre", flip.OutcomeDown, time.UnixMilli(ts)) // yes 押 Down = 输 −2U
	// 线是构造参数: 直接压到 −1 比造一笔大额亏损干净（今日已结算 −2 ≤ −1 → 熔断）。
	x.MaxDailyLoss = -1
	g := x.HandleObservation(okSnap(ts, flip.SideNo), "0xgated", "slug-g", 1780000000)
	if g == nil || g.GateReason != GateDailyLoss {
		t.Fatalf("paper 被闸行应带 gate_reason: %+v", g)
	}
	if !g.IsFilled() {
		t.Fatal("paper 被闸行 schema 与正常信号一致（IsFilled 仍真, 照常结算）")
	}
	if ex.calls != 0 {
		t.Fatalf("被闸不得下单, Ex 被调用 %d 次", ex.calls)
	}
	pending := rec.PendingSignals()
	if len(pending) != 1 || pending[0].ConditionID != "0xgated" {
		t.Fatalf("被闸行应照常入 pending（0xpre 已结算, 只剩被闸行）: %+v", pending)
	}
	// 锁存: 被闸行照常结算, 赢下来 P&L 回升过线——无锁存则当日自动复牌。
	if !rec.Resolve("0xgated", flip.OutcomeUp, time.UnixMilli(ts+1000)) {
		t.Fatal("被闸行应在 pending（能结算）")
	}
	if !x.breakerTripped() {
		t.Fatal("P&L 已回升过线仍须锁存（这正是锁存必需的原因）")
	}

	// ② live: 同日被闸 → rejected 行, 不调用执行器; first_window 只在 live 生效。
	ex2 := &fakeExec{}
	x2, rec2, _ := newTestExec(t, true, ex2)
	x2.FirstWindow = true
	r := x2.HandleObservation(okSnap(ts, flip.SideYes), "0xfw", "slug-fw", 1780000000)
	if r == nil || r.ExecStatus != flip.ExecStatusRejected || r.GateReason != GateFirstWindow {
		t.Fatalf("live 首窗应记 rejected + first_window: %+v", r)
	}
	if ex2.calls != 0 {
		t.Fatal("live 首窗禁单不得下单")
	}
	if n := len(rec2.PendingSignals()); n != 0 {
		t.Fatal("rejected 行无持仓, 不入 pending")
	}
	// paper 侧同条件**不**按首窗拦（无真实仓位可撞）——用干净实例, 免得撞上 ① 的
	// 日亏锁存（那会以 daily_loss 而非 first_window 拦下）。
	ex3 := &fakeExec{}
	xp, _, _ := newTestExec(t, false, ex3)
	xp.FirstWindow = true
	if reason, blocked := xp.gate(); blocked {
		t.Fatalf("first_window 只对 live 生效, paper 被 %s 拦下", reason)
	}
}
