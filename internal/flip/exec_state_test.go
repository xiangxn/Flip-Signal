package flip

import (
	"strings"
	"testing"
	"time"
)

// scriptedExecutor 脚本化 Executor（代替 cmd/flip 里对 trading.LiveExecutor 的
// 集成测试——本包零外部依赖, live 实现本身的 SDK 解析由 trading 包测试覆盖;
// 编排层只依赖 Executor 契约, 返回预置终态即可全路径验证闸/两阶段/回填）。
type scriptedExecutor struct {
	res   *ExecResult
	calls int    // Execute 调用计数（闸拦截断言用）
	last  string // 最近一次收到的 tokenID（token 取侧断言用）
}

func (s *scriptedExecutor) Execute(o *Observation, tokenID string, stake float64) *ExecResult {
	s.calls++
	s.last = tokenID
	return s.res
}

// mkFilled 构造全额成交的脚本终态（fill=0.2 → stake/fill=10 股整数, 对照零摩擦）。
func mkFilled(orderID string) *ExecResult {
	return &ExecResult{Status: ExecStatusFilled, OrderID: orderID, FillPrice: 0.2, Shares: 10, Cost: 2}
}

// mkExecObs 构造 ok 观测（fill=0.2 → stake/fill=10 股整数, paper/live 对照零摩擦）。
// Ts = 当前 UTC 毫秒: 记录 Date=今日——日亏熔断测试按今日结算现算口径断言。
func mkExecObs(side string) *Observation {
	return &Observation{
		Ts: time.Now().UTC().UnixMilli(), Side: side, Fill: 0.2, OK: true, Rem: 200,
		M20: 0.45, M30: 0.5, M45: 0.6, DistS: -0.3, DistT: -0.2,
		Shares: 10, Anchor: 60_000, HistBps: 20, Spot: 60_010,
	}
}

// mkExecRec 构造临时 recorder + ExecState。
// live 时 ex 为脚本 executor（免真实 SDK 网调）; FirstWindow 由测试自设。
func mkExecRec(t *testing.T, ex Executor, live bool, maxDailyLoss float64) (*ExecState, *Recorder) {
	t.Helper()
	rec, err := NewRecorder(t.TempDir())
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	x := &ExecState{
		Rec:          rec,
		Ex:           ex,
		Live:         live,
		Stake:        2,
		MaxDailyLoss: maxDailyLoss,
		Tokens:       func() (string, string) { return "TOK_UP", "TOK_DN" },
	}
	return x, rec
}

// ── paper 单步（无闸、无两阶段、无 exec 字段）──

func TestExecPaperOK(t *testing.T) {
	x, rec := mkExecRec(t, PaperExecutor{}, false, 0)
	o := mkExecObs(SideYes)
	got := x.HandleObservation(o, "condP", "slug", 1_800_000_000_000)
	if got == nil || !got.OK || got.ExecStatus != "" || !got.IsFilled() {
		t.Fatalf("paper ok 行不符: got=%+v", got)
	}
	if got.Shares != o.Shares || got.Fill != o.Fill {
		t.Fatalf("paper 行 fill/shares 应为引擎目标: fill=%v shares=%v", got.Fill, got.Shares)
	}
	if n := len(rec.Observations()); n != 1 {
		t.Fatalf("paper ok 应落 1 行, 实 %d", n)
	}
}

func TestExecPaperRejectedObsNoSideEffect(t *testing.T) {
	x, rec := mkExecRec(t, PaperExecutor{}, false, 0)
	o := mkExecObs(SideYes)
	o.OK = false
	o.RejectReason = "no_crash"
	got := x.HandleObservation(o, "condR", "slug", 1_800_000_000_000)
	if got == nil || got.OK || got.RejectReason != "no_crash" {
		t.Fatalf("否决观测行不符: %+v", got)
	}
	if n := len(rec.Observations()); n != 1 {
		t.Fatalf("否决观测应落 1 行, 实 %d", n)
	}
}

// ── live 全流程（闸 → submitting → 统一 Execute → 回填）──

func TestExecLiveFilled(t *testing.T) {
	sc := &scriptedExecutor{res: mkFilled("o1")}
	x, rec := mkExecRec(t, sc, true, -20)
	x.FirstWindow = false

	got := x.HandleObservation(mkExecObs(SideNo), "condL", "slug", 1_800_000_000_000)
	if got == nil || !got.OK || !got.IsFilled() {
		t.Fatalf("live filled 行不符: %+v", got)
	}
	if got.ExecStatus != ExecStatusFilled || got.OrderID != "o1" {
		t.Fatalf("终态/订单号不符: exec=%s order=%s", got.ExecStatus, got.OrderID)
	}
	if got.Shares != 10 || got.Cost != 2 {
		t.Fatalf("成交回填不符: shares=%v cost=%v", got.Shares, got.Cost)
	}
	// 狗侧 token: side=no → DOWN token
	if sc.calls != 1 || sc.last != "TOK_DN" {
		t.Fatalf("Execute 应恰好 1 次且 token=TOK_DN, 实 %d 次 token=%s", sc.calls, sc.last)
	}
	if n := len(rec.PendingSignals()); n != 1 {
		t.Fatalf("filled 应入 pending, 实 %d", n)
	}
}

func TestExecLiveUnmatchedNoPending(t *testing.T) {
	sc := &scriptedExecutor{res: &ExecResult{Status: ExecStatusUnfilled, OrderID: "o2"}}
	x, rec := mkExecRec(t, sc, true, -20)

	got := x.HandleObservation(mkExecObs(SideYes), "condU", "slug", 1_800_000_000_000)
	if got == nil || !got.OK || got.IsFilled() {
		t.Fatalf("unmatched 行应 ok 且未成交: %+v", got)
	}
	if got.ExecStatus != ExecStatusUnfilled {
		t.Fatalf("终态应为 unfilled: %s", got.ExecStatus)
	}
	if n := len(rec.PendingSignals()); n != 0 {
		t.Fatalf("unfilled 不应入 pending, 实 %d", n)
	}
}

func TestExecLiveFirstWindowGate(t *testing.T) {
	sc := &scriptedExecutor{res: mkFilled("o3")}
	x, _ := mkExecRec(t, sc, true, -20)
	x.FirstWindow = true

	got := x.HandleObservation(mkExecObs(SideYes), "condG", "slug", 1_800_000_000_000)
	if got == nil || got.IsFilled() || got.ExecStatus != ExecStatusRejected {
		t.Fatalf("首窗应拦为 rejected: %+v", got)
	}
	if !strings.Contains(got.ExecNote, "首窗禁单") {
		t.Fatalf("note 应为首窗禁单: %s", got.ExecNote)
	}
	if sc.calls != 0 {
		t.Fatalf("首窗禁单不得执行, 实 Execute %d 次", sc.calls)
	}
}

func TestExecLiveDailyLossBreaker(t *testing.T) {
	sc := &scriptedExecutor{res: mkFilled("o4")}
	x, rec := mkExecRec(t, sc, true, -0.5) // 熔断线 -0.5U

	// 先成交一笔并结算为输（side=yes 买 UP, outcome=1 → −2U 已结算）
	x.HandleObservation(mkExecObs(SideYes), "condA", "slug", 1_800_000_000_000)
	if !rec.Resolve("condA", 1, time.Now()) {
		t.Fatal("首笔结算失败")
	}
	// 第二笔应被熔断拦截（今日已结算 −2 ≤ −0.5）
	got := x.HandleObservation(mkExecObs(SideYes), "condB", "slug", 1_800_000_000_000)
	if got == nil || got.IsFilled() || got.ExecStatus != ExecStatusRejected {
		t.Fatalf("熔断应拦为 rejected: %+v", got)
	}
	if !strings.Contains(got.ExecNote, "日亏熔断") {
		t.Fatalf("note 应为日亏熔断: %s", got.ExecNote)
	}
	if sc.calls != 1 {
		t.Fatalf("熔断后不得再执行, 实 Execute %d 次（应 1: 仅首笔）", sc.calls)
	}
}

func TestExecLiveSummaryPaperNil(t *testing.T) {
	x, _ := mkExecRec(t, PaperExecutor{}, false, 0)
	if s := x.LiveSummary(); s != nil {
		t.Fatalf("paper LiveSummary 应 nil: %+v", s)
	}
}

// ── paper/live 同源对照（同一观测, 两实现终态一致, live 仅多 exec 字段）──

func TestExecPaperLiveParity(t *testing.T) {
	sc := &scriptedExecutor{res: mkFilled("o5")}
	obs := mkExecObs(SideYes)

	// paper 路
	xp, recP := mkExecRec(t, PaperExecutor{}, false, 0)
	rp := xp.HandleObservation(obs, "condP1", "slug", 1_800_000_000_000)
	// live 路（脚本全额 @ 同价）
	xl, recL := mkExecRec(t, sc, true, -20)
	rl := xl.HandleObservation(obs, "condL1", "slug", 1_800_000_000_000)

	if rp == nil || rl == nil {
		t.Fatalf("两路均应有行: paper=%+v live=%+v", rp, rl)
	}
	// 同源断言: 判定字段/成交股数/价格一致——live 仅多 exec 字段
	if rp.Side != rl.Side || rp.Fill != rl.Fill || rp.Shares != rl.Shares {
		t.Fatalf("paper/live 成交口径分叉: paper(fill=%v sh=%v) live(fill=%v sh=%v)",
			rp.Fill, rp.Shares, rl.Fill, rl.Shares)
	}
	if rl.ExecStatus == "" || rp.ExecStatus != "" {
		t.Fatalf("exec 字段应只属于 live: paper=%q live=%q", rp.ExecStatus, rl.ExecStatus)
	}
	if rp.Cost != 0 || rl.Cost != 2 {
		t.Fatalf("cost 应只属于 live: paper=%v live=%v", rp.Cost, rl.Cost)
	}
	// 文件行数: 各 1 行
	if len(recP.Observations()) != 1 || len(recL.Observations()) != 1 {
		t.Fatalf("各应 1 行: paper=%d live=%d", len(recP.Observations()), len(recL.Observations()))
	}
}
