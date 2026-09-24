package tail

import (
	"testing"
	"time"

	"github.com/necklace/flip-signal/internal/flip"
)

// fakeExec 记录调用并返回预设结果——用来钉住「判定行永不下单」这类编排红线。
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

// TestExecStateLegacyKindNeverTrades legacy 行（帧/对账行）绝不能进执行路径——这是本品
// 最危险的一条红线（它们没有 exec 语义, 下成真单就是凭一条不存在的东西开仓）。
// 三段链后引擎不再产出它们, 但本层防线保留（调用点写错/未来加第二条路径时兜住）。
func TestExecStateLegacyKindNeverTrades(t *testing.T) {
	ex := &fakeExec{}
	x, rec, _ := newTestExec(t, true, ex)

	for _, kind := range []string{KindFrame, KindScan, ""} {
		legacy := &Observation{
			Kind: kind, FrameT: 150, Ts: time.Now().UnixMilli(), Rem: 145,
			Side: flip.SideYes, HotAsk: 0.92, Dev: 100, Sd: 100, Anchor: 100000,
			OK: true, // 即便带上 ok=true 也不得执行
		}
		if got := x.HandleDecision(legacy, "0x"+kind, "slug", 1780000000); got != nil {
			t.Fatalf("kind=%q 的行必须被拒（不落盘、不下单）, 得到 %+v", kind, got)
		}
	}
	if ex.calls != 0 {
		t.Fatalf("legacy 行不得触发下单, Ex 被调用 %d 次", ex.calls)
	}
	if n := len(rec.Observations()); n != 0 {
		t.Fatalf("被拒的行不该有副作用, 得到 %d 行", n)
	}
}

// TestExecStatePaper 纸面: 判定行（ok=false）落盘不下单; 信号行模拟成交落盘（无 exec
// 字段）+ 入 pending。三段链任一 stage 的信号行形态一致。
func TestExecStatePaper(t *testing.T) {
	ex := &fakeExec{}
	x, rec, _ := newTestExec(t, false, ex)

	// 第一段判定行被拒（价格腿不过）: 照记, 不下单。
	loser := okSnap(time.Now().UnixMilli(), flip.SideYes)
	loser.Stage, loser.FrameT, loser.Rem = StageT150, 150, 145
	loser.OK, loser.Rules, loser.Shares = false, Rules{Price: false}, 0
	loser.RejectReason = RejectPriceLow
	r1 := x.HandleDecision(loser, "0xloser", "slug-l", 1780000000)
	if r1 == nil || r1.OK || ex.calls != 0 {
		t.Fatalf("判定行应落盘且不下单: rec=%v calls=%d", r1, ex.calls)
	}
	if n := len(rec.PendingSignals()); n != 0 {
		t.Fatalf("判定行不入 pending, 得到 %d", n)
	}

	// 第二段信号行: 模拟成交, 行里无 exec 字段（IsFilled 恒真）。
	r2 := x.HandleDecision(okSnap(time.Now().UnixMilli(), flip.SideNo), "0xok", "slug-o", 1780000000)
	if r2 == nil || !r2.IsFilled() || r2.ExecStatus != "" {
		t.Fatalf("纸面行应为「无 exec 字段 + IsFilled」: %+v", r2)
	}
	if ex.calls != 1 {
		t.Fatalf("信号行应恰好下一次单, 得到 %d", ex.calls)
	}
	if ex.token != "" {
		t.Fatalf("纸面不该传 token（无真实订单）: %q", ex.token)
	}
	if ex.lastOb.Fill != 0.92 || !near(ex.lastOb.Shares, 2/0.92) {
		t.Fatalf("执行器契约应收到 Fill=hot_ask / Shares=目标股数: %+v", ex.lastOb)
	}
	if n := len(rec.PendingSignals()); n != 1 {
		t.Fatalf("纸面信号行应入 pending, 得到 %d", n)
	}

	// 监听段的信号行是**真下单**（不再是对账行）: 走同一条路径。
	// 用另一个窗口, 否则撞上 0xok 的「本窗已出信号」守卫。
	lis := okSnap(time.Now().UnixMilli(), flip.SideYes)
	lis.Stage, lis.FrameT, lis.Rem = StageListen, 60, 40
	lis.Rules = Rules{Price: true, Dev63: true} // ② 达标
	r3 := x.HandleDecision(lis, "0xlisten", "slug-li", 1780000000)
	if r3 == nil || !r3.OK || r3.Stage != StageListen {
		t.Fatalf("监听段信号应照常下单落盘: %+v", r3)
	}
	if ex.calls != 2 {
		t.Fatalf("监听段信号应下第二单, 得到 %d 次调用", ex.calls)
	}
}

// TestExecStateDuplicateOKRejected 落盘层的防双单守卫: 同窗第二条 OK 行必须被拒
// （三段链「出信号即 Done」已保证不会出现, 这里是重启重入/未来改动的冗余防线）。
func TestExecStateDuplicateOKRejected(t *testing.T) {
	ex := &fakeExec{}
	x, rec, _ := newTestExec(t, false, ex)

	if got := x.HandleDecision(okSnap(time.Now().UnixMilli(), flip.SideYes), "0xc", "slug", 1780000000); got == nil {
		t.Fatal("第一条信号行应落盘")
	}
	second := okSnap(time.Now().UnixMilli(), flip.SideYes)
	second.Stage, second.FrameT, second.Rem = StageListen, 60, 30
	if got := x.HandleDecision(second, "0xc", "slug", 1780000000); got != nil {
		t.Fatalf("同窗第二条 OK 行必须被拒, 得到 %+v", got)
	}
	if ex.calls != 1 {
		t.Fatalf("被拒的重复信号不得下单, Ex 被调用 %d 次", ex.calls)
	}
	if n := len(rec.Observations()); n != 1 {
		t.Fatalf("被拒的重复信号不该落盘, 得到 %d 行", n)
	}
	// 判定行（ok=false）不受该守卫影响: 第一段判定行先于信号行落盘, 是正常形态。
	d := okSnap(time.Now().UnixMilli(), flip.SideYes)
	d.Stage, d.FrameT, d.Rem = StageT150, 150, 145
	d.OK, d.Shares, d.RejectReason = false, 0, RejectLegOut
	if got := x.HandleDecision(d, "0xc", "slug", 1780000000); got == nil {
		t.Fatal("判定行不该被防双单守卫拦下（它没有 OK 语义）")
	}
}

// TestExecStateLiveTwoPhase live: submitting 先行落盘 → 回填终态; resting 行改由
// FillTracker 定稿（ApplyFillFinal）后才入 pending。
func TestExecStateLiveTwoPhase(t *testing.T) {
	// ① 即时全额成交（filled）。
	ex := &fakeExec{res: flip.ExecResult{Status: flip.ExecStatusFilled, OrderID: "o1", FillPrice: 0.90, Shares: 2.22, Cost: 2.0}}
	x, rec, _ := newTestExec(t, true, ex)
	r := x.HandleDecision(okSnap(time.Now().UnixMilli(), flip.SideYes), "0xc", "slug", 1780000000)
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
	r2 := x2.HandleDecision(o, "0xd", "slug-d", 1780000000)
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

	// ③ 0 成交（unfilled）: 无仓位, 但**照常注册结算**（页面要显示官方结果）。
	ex3 := &fakeExec{res: flip.ExecResult{Status: flip.ExecStatusUnfilled, OrderID: "o3", Note: "余量已撤"}}
	x3, rec3, _ := newTestExec(t, true, ex3)
	if r := x3.HandleDecision(okSnap(time.Now().UnixMilli(), flip.SideYes), "0xe", "slug-e", 1780000000); r == nil {
		t.Fatal("unfilled 行应落盘")
	}
	if n := len(rec3.PendingSignals()); n != 1 {
		t.Fatalf("0 成交行应注册结算（只为显示结果）, 得到 %d", n)
	}
	if !rec3.Resolve("0xe", flip.OutcomeUp, time.UnixMilli(time.Now().UnixMilli()), "") {
		t.Fatal("0 成交行应能结算")
	}
	for _, o := range rec3.Observations() {
		if o.ConditionID != "0xe" {
			continue
		}
		if o.Won == nil || !*o.Won {
			t.Fatalf("0 成交行结果照显（押 yes 遇 Up = 赢）: %+v", o.Won)
		}
		if o.PnL != 0 {
			t.Fatalf("0 成交 ⇒ P&L 恒 0, 得到 %.4f", o.PnL)
		}
	}
}

// TestExecStateGate 风控闸两模式**同一后果**（2026-09-24 起）: 被闸 = rejected 行
// = 未成交（不计 P&L / 不进胜率）, 但仍注册结算以便页面显示官方结果;
// 且被闸行进 GatedOn 锁存 —— 当日不再复牌。first_window 只在 live 生效。
func TestExecStateGate(t *testing.T) {
	ts := time.Now().UnixMilli()

	// ① paper: 当日已亏过线 → 被闸行 = rejected（无仓位）。
	ex := &fakeExec{}
	x, rec, _ := newTestExec(t, false, ex)
	if _, err := rec.RecordObservation("0xpre", "s", 0, okSnap(ts-3000, flip.SideYes), 2); err != nil {
		t.Fatal(err)
	}
	rec.Resolve("0xpre", flip.OutcomeDown, time.UnixMilli(ts), "") // yes 押 Down = 输 −2U
	// 线是构造参数: 直接压到 −1 比造一笔大额亏损干净（今日已结算 −2 ≤ −1 → 熔断）。
	x.MaxDailyLoss = -1
	g := x.HandleDecision(okSnap(ts, flip.SideNo), "0xgated", "slug-g", 1780000000)
	if g == nil || g.GateReason != GateDailyLoss {
		t.Fatalf("被闸行应带 gate_reason: %+v", g)
	}
	if g.ExecStatus != flip.ExecStatusRejected || g.IsFilled() || g.HasPosition() {
		t.Fatalf("被闸 = 未成交（a.md 第 4 条: 被风控拦不算成交）: %+v", g)
	}
	if ex.calls != 0 {
		t.Fatalf("被闸不得下单, Ex 被调用 %d 次", ex.calls)
	}
	// 锁存: 磁盘上已有被闸行 ⇒ 当日不再复牌（判据取磁盘真相）。
	if !rec.GatedOn(utcToday(), GateDailyLoss) {
		t.Fatal("被闸行应落 gate_reason 供当日锁存")
	}
	if !x.breakerTripped() {
		t.Fatal("当日已有被闸行 ⇒ 锁存, 不得复牌")
	}
	// 仍注册结算: 页面要显示这一笔的官方结果。
	pending := rec.PendingSignals()
	if len(pending) != 1 || pending[0].ConditionID != "0xgated" {
		t.Fatalf("被闸行应注册结算（0xpre 已结算, 只剩被闸行）: %+v", pending)
	}
	if !rec.Resolve("0xgated", flip.OutcomeUp, time.UnixMilli(ts+1000), "") {
		t.Fatal("被闸行应在 pending（能结算）")
	}
	for _, o := range rec.Observations() {
		if o.ConditionID != "0xgated" {
			continue
		}
		if o.Won == nil || *o.Won {
			t.Fatalf("结果照显（押 no 遇 Up = 输）: %+v", o.Won)
		}
		if o.PnL != 0 {
			t.Fatalf("被闸行无仓位 ⇒ P&L 恒 0, 得到 %.4f", o.PnL)
		}
	}
	// 熔断输入不受被闸行影响（P&L 恒 0 已被 HasPosition 过滤掉）。
	if !x.breakerTripped() {
		t.Fatal("结算回填后仍须锁存（这正是锁存必需的原因）")
	}

	// ② live: 首窗禁单 → rejected 行, 不调用执行器。
	ex2 := &fakeExec{}
	x2, rec2, _ := newTestExec(t, true, ex2)
	x2.FirstWindow = true
	r := x2.HandleDecision(okSnap(ts, flip.SideYes), "0xfw", "slug-fw", 1780000000)
	if r == nil || r.ExecStatus != flip.ExecStatusRejected || r.GateReason != GateFirstWindow {
		t.Fatalf("live 首窗应记 rejected + first_window: %+v", r)
	}
	if ex2.calls != 0 {
		t.Fatal("live 首窗禁单不得下单")
	}
	if n := len(rec2.PendingSignals()); n != 1 {
		t.Fatalf("被闸行照常注册结算, 得到 %d", n)
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
