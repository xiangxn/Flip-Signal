package trading

import (
	"errors"
	"math"
	"strings"
	"testing"
	"time"

	"github.com/xiangxn/go-polymarket-sdk/orders"

	"github.com/necklace/flip-signal/internal/flip"
)

// ── FillTracker（GTC 挂单终态跟踪 + rem≤RemMin 撤单）──
//
// 全部测试直接调 pollOne/pollAll（同包）并显式传时刻, 不起 goroutine、不睡表——
// 终态判据是纯逻辑（查询结果 + 时刻 → 是否定稿）, 这样测才可复现。

const testEventStart = int64(1_780_000_000) // 窗口起点（unix 秒）

// testCancelLead = 策略时间腿 flip.Config.RemMin（cmd/flip 从配置传入的真值）。
const testCancelLead = 180 * time.Second

// mkRestingRec 构造一条 resting 落盘行（eventStart+300 = 闭市, 投入 2U @0.20 → 目标 10 股）。
func mkRestingRec() *flip.Record {
	return &flip.Record{
		Observation: flip.Observation{Ts: testEventStart * 1000, Side: flip.SideYes, Fill: 0.20, OK: true},
		ConditionID: "cond-1",
		Slug:        "btc-updown-5m-1780000000",
		EventStart:  testEventStart,
		Stake:       2,
		OrderID:     "order-1",
		ExecStatus:  flip.ExecStatusResting,
	}
}

func testWindowEnd() time.Time { return time.Unix(testEventStart, 0).Add(300 * time.Second) }

// atRem 窗口内 rem 秒时刻（rem = 闭市 − 现在）: 撤单点 = 闭市 − testCancelLead = atRem(180)。
func atRem(rem int) time.Time { return testWindowEnd().Add(-time.Duration(rem) * time.Second) }

// newTestTracker 起一台挂单跟踪器（撤单提前量 = 策略时间腿 180s）。
func newTestTracker(fc *fakeClient, onFinal func(flip.FillFinal)) *FillTracker {
	return NewFillTracker(fc, testCancelLead, onFinal)
}

// near 浮点容差比较（cost = shares×limit 会有 1e-16 级尾差, 如 3×0.2=0.6000000000000001）。
func near(a, b float64) bool { return math.Abs(a-b) < 1e-9 }

// only 取出跟踪表中唯一一笔挂单（测试内直调 pollOne 用）。
func only(t *testing.T, ft *FillTracker) *trackedOrder {
	t.Helper()
	ft.mu.Lock()
	defer ft.mu.Unlock()
	if len(ft.orders) != 1 {
		t.Fatalf("跟踪表应有 1 笔, 实际 %d", len(ft.orders))
	}
	for _, o := range ft.orders {
		return o
	}
	return nil
}

// mkOrder 造一条 GetOpenOrders 返回的挂单（matched = 累计成交股数）。
func mkOrder(status string, matched float64) []orders.OpenOrder {
	return []orders.OpenOrder{{Id: "order-1", Status: status, OriginalSize: 10, SizeMatched: matched, Price: 0.20}}
}

func TestFillTrackerFullFill(t *testing.T) {
	fc := &fakeClient{ooResp: mkOrder("LIVE", 10)}
	var got []flip.FillFinal
	ft := newTestTracker(fc, func(f flip.FillFinal) { got = append(got, f) })
	ft.Register(mkRestingRec(), testWindowEnd(), false)

	ft.pollAll()

	if len(got) != 1 {
		t.Fatalf("应定稿 1 笔, 实际 %d（%+v）", len(got), got)
	}
	f := got[0]
	if f.Status != flip.ExecStatusFilled || !near(f.Shares, 10) || !near(f.Cost, 2) {
		t.Fatalf("终态 = %+v, 期望 filled 10 股 / 2U", f)
	}
	if f.ConditionID != "cond-1" || strings.HasPrefix(f.Note, flip.ExecNoteUnknown) {
		t.Fatalf("定稿元信息: %+v", f)
	}
	if ft.Count() != 0 {
		t.Fatalf("定稿后应移出跟踪表, 实际剩 %d", ft.Count())
	}
	if fc.cnlCalls != 0 {
		t.Fatalf("满额成交无余量可撤, 不应发撤单请求（实际 %d 次）", fc.cnlCalls)
	}
}

// 满额成交发生在撤单点之后也一样: 撤单请求只在「本轮查到仍有未成交余量」时发。
func TestFillTrackerFullFillAfterCancelPoint(t *testing.T) {
	fc := &fakeClient{ooResp: mkOrder("LIVE", 10)}
	var got []flip.FillFinal
	ft := newTestTracker(fc, func(f flip.FillFinal) { got = append(got, f) })
	ft.Register(mkRestingRec(), testWindowEnd(), false)

	if fin, done := ft.pollOne(only(t, ft), atRem(178)); !done || fin.Status != flip.ExecStatusFilled {
		t.Fatalf("定稿 = %+v done=%v, 期望 filled", fin, done)
	}
	if fc.cnlCalls != 0 {
		t.Fatalf("已满额不应撤单, 实际 %d 次", fc.cnlCalls)
	}
}

// ── 撤单: rem ≤ RemMin 把未成交余量撤走（2026-09-19 口径）──

func TestFillTrackerCancelAtRemMin(t *testing.T) {
	fc := &fakeClient{ooResp: mkOrder("LIVE", 0)}
	var got []flip.FillFinal
	ft := newTestTracker(fc, func(f flip.FillFinal) { got = append(got, f) })
	ft.Register(mkRestingRec(), testWindowEnd(), false)
	o := only(t, ft)

	// 撤单点之前（rem 240）: 挂单继续等对手方, 不撤
	if _, done := ft.pollOne(o, atRem(240)); done {
		t.Fatalf("窗口早段不应定稿")
	}
	if fc.cnlCalls != 0 {
		t.Fatalf("未到撤单点不应撤单, 实际 %d 次", fc.cnlCalls)
	}

	// 到 rem 180（边界, 含）: 撤
	if _, done := ft.pollOne(o, atRem(180)); done {
		t.Fatalf("撤单刚发出不应定稿（撤销生效前成交量仍可能变）")
	}
	if fc.cnlCalls != 1 || len(fc.cancelled) != 1 || fc.cancelled[0] != "order-1" {
		t.Fatalf("应撤单 1 次 order-1, 实际 calls=%d %v", fc.cnlCalls, fc.cancelled)
	}

	// 幂等: 已撤过不再重复发（每 2s 一发会刷爆 CLOB）
	ft.pollOne(o, atRem(178))
	if fc.cnlCalls != 1 {
		t.Fatalf("已撤单不应重复撤, 实际 %d 次", fc.cnlCalls)
	}

	// 撤单生效（挂单表已无此单）→ 末次观测即终值
	fc.ooResp = nil
	fin, done := ft.pollOne(o, atRem(176))
	if !done || fin.Status != flip.ExecStatusUnfilled || fin.Shares != 0 || fin.Cost != 0 {
		t.Fatalf("定稿 = %+v done=%v, 期望 unfilled（0 成交）", fin, done)
	}
	if !strings.Contains(fin.Note, "撤单生效") || !strings.Contains(fin.Note, "未成交余量已撤") {
		t.Fatalf("note 应写明撤单口径: %q", fin.Note)
	}
	if strings.HasPrefix(fin.Note, flip.ExecNoteUnknown) {
		t.Fatalf("见过该单就不算未确认: %q", fin.Note)
	}
}

func TestFillTrackerCancelKeepsPartial(t *testing.T) {
	// 撤单时已吃到 4 股: 那 4 股照记（成本按限价）, 余量随撤单作废
	fc := &fakeClient{ooResp: mkOrder("LIVE", 4)}
	var got []flip.FillFinal
	ft := newTestTracker(fc, func(f flip.FillFinal) { got = append(got, f) })
	ft.Register(mkRestingRec(), testWindowEnd(), false)
	o := only(t, ft)

	ft.pollOne(o, atRem(200)) // 撤单点前先观测到 4 股
	if _, done := ft.pollOne(o, atRem(178)); done {
		t.Fatalf("撤单刚发出不应定稿")
	}
	fc.ooResp = nil
	fin, done := ft.pollOne(o, atRem(175))
	if !done || fin.Status != flip.ExecStatusPartial || !near(fin.Shares, 4) || !near(fin.Cost, 0.8) {
		t.Fatalf("定稿 = %+v done=%v, 期望 partial 4 股 / 0.8U", fin, done)
	}
	if !strings.Contains(fin.Note, "4.00/10.00") || !strings.Contains(fin.Note, "未成交余量已撤") {
		t.Fatalf("note 应含成交量/下单量与撤单口径: %q", fin.Note)
	}
	if fc.cnlCalls != 1 {
		t.Fatalf("应撤单 1 次, 实际 %d", fc.cnlCalls)
	}
}

func TestFillTrackerCancelRetryUntilSuccess(t *testing.T) {
	// 撤单失败必须重试: 活着的挂单会继续吃进 rem ≤ RemMin 之后的成交（策略不要那批）
	fc := &fakeClient{ooResp: mkOrder("LIVE", 2), cancelErr: errors.New("boom")}
	var got []flip.FillFinal
	ft := newTestTracker(fc, func(f flip.FillFinal) { got = append(got, f) })
	ft.Register(mkRestingRec(), testWindowEnd(), false)
	o := only(t, ft)

	if _, done := ft.pollOne(o, atRem(179)); done {
		t.Fatalf("撤单失败不应定稿")
	}
	ft.pollOne(o, atRem(177))
	if fc.cnlCalls != 2 {
		t.Fatalf("撤单失败应每轮重试, 实际 %d 次", fc.cnlCalls)
	}

	// 重试成功 → 挂单消失 → 按末次观测定稿
	fc.cancelErr = nil
	ft.pollOne(o, atRem(175))
	if fc.cnlCalls != 3 || !o.cancelSent {
		t.Fatalf("重试应成功, calls=%d sent=%v", fc.cnlCalls, o.cancelSent)
	}
	fc.ooResp = nil
	fin, done := ft.pollOne(o, atRem(173))
	if !done || fin.Status != flip.ExecStatusPartial || !near(fin.Shares, 2) {
		t.Fatalf("定稿 = %+v done=%v, 期望 partial 2 股", fin, done)
	}
}

func TestFillTrackerCancelThenStillListed(t *testing.T) {
	// 撤单成功但 CLOB 还没把它移出挂单表: 给确认宽限, 用尽即按末次观测定稿
	fc := &fakeClient{ooResp: mkOrder("LIVE", 1)}
	var got []flip.FillFinal
	ft := newTestTracker(fc, func(f flip.FillFinal) { got = append(got, f) })
	ft.Register(mkRestingRec(), testWindowEnd(), false)
	o := only(t, ft)

	ft.pollOne(o, atRem(180)) // 撤单发出
	if _, done := ft.pollOne(o, atRem(170)); done {
		t.Fatalf("撤单确认宽限内不应定稿")
	}
	fin, done := ft.pollOne(o, atRem(164)) // 撤单后 16s > 15s 宽限
	if !done || fin.Status != flip.ExecStatusPartial || !near(fin.Shares, 1) {
		t.Fatalf("宽限用尽定稿 = %+v done=%v, 期望 partial 1 股", fin, done)
	}
	if !strings.Contains(fin.Note, "撤单后仍见挂单") {
		t.Fatalf("note 应标注撤单未确认: %q", fin.Note)
	}
}

func TestFillTrackerPartialThenClosed(t *testing.T) {
	// 撤单后仍见该单的另一种收尾: 闭市宽限用尽（撤单失败的兜底路径）
	fc := &fakeClient{ooResp: mkOrder("LIVE", 3), cancelErr: errors.New("boom")}
	var got []flip.FillFinal
	ft := newTestTracker(fc, func(f flip.FillFinal) { got = append(got, f) })
	ft.Register(mkRestingRec(), testWindowEnd(), false)
	o := only(t, ft)

	// 窗口内只吃到 3 股且没撤掉 → 未定稿（仓位还会长）
	if _, done := ft.pollOne(o, atRem(60)); done {
		t.Fatalf("未满额且撤单未成功, 不应定稿")
	}
	// 硬截止（闭市 + 宽限）→ 取末次观测
	fin, done := ft.pollOne(o, testWindowEnd().Add(fillCloseGrace))
	if !done || fin.Status != flip.ExecStatusPartial || !near(fin.Shares, 3) || !near(fin.Cost, 0.6) {
		t.Fatalf("闭市定稿 = %+v done=%v, 期望 partial 3 股 / 0.6U", fin, done)
	}
	if strings.Contains(fin.Note, "未成交余量已撤") {
		t.Fatalf("撤单没成功, note 不应写「已撤」: %q", fin.Note)
	}
}

func TestFillTrackerNeverObservedKeepsResting(t *testing.T) {
	// 从未查到该单（POST 后索引延迟 / 接口故障）: 「查不到」不能推定 0 成交
	fc := &fakeClient{}
	var got []flip.FillFinal
	ft := newTestTracker(fc, func(f flip.FillFinal) { got = append(got, f) })
	ft.Register(mkRestingRec(), testWindowEnd(), false)
	o := only(t, ft)

	if _, done := ft.pollOne(o, atRem(60)); done {
		t.Fatalf("从没观测到过该单, 窗口内不应定稿")
	}
	// 到硬截止仍没见过 → 保持 resting（未确认）, 交人工核对
	fin, done := ft.pollOne(o, testWindowEnd().Add(fillCloseGrace))
	if !done || fin.Status != flip.ExecStatusResting {
		t.Fatalf("截止定稿 = %+v done=%v, 期望 resting 未确认", fin, done)
	}
	if !strings.HasPrefix(fin.Note, flip.ExecNoteUnknown) {
		t.Fatalf("未确认行必须带人工核对标记: %q", fin.Note)
	}
}

func TestFillTrackerAdoptedUnseen(t *testing.T) {
	// 重启接管: 无本进程 POST 背书, 「查不到」不能推定闭市撤回（可能早在崩溃前就成交了）
	fc := &fakeClient{}
	var got []flip.FillFinal
	ft := newTestTracker(fc, func(f flip.FillFinal) { got = append(got, f) })
	ft.Register(mkRestingRec(), testWindowEnd(), true)
	o := only(t, ft)

	fin, done := ft.pollOne(o, time.Unix(testEventStart, 0).Add(10*time.Minute))
	if !done || fin.Status != flip.ExecStatusResting || !strings.HasPrefix(fin.Note, flip.ExecNoteUnknown) {
		t.Fatalf("接管且查不到 = %+v done=%v, 期望 resting 未确认", fin, done)
	}
	if fc.cnlCalls != 0 {
		t.Fatalf("挂单表已无此单, 不该白发撤单请求（实际 %d 次）", fc.cnlCalls)
	}

	// 接管但查得到（端点返回已终态订单）→ 正常定稿
	got = nil
	fc.ooResp = mkOrder("CANCELED", 10)
	ft2 := newTestTracker(fc, func(f flip.FillFinal) { got = append(got, f) })
	ft2.Register(mkRestingRec(), testWindowEnd(), true)
	if fin, done := ft2.pollOne(only(t, ft2), time.Unix(testEventStart, 0).Add(10*time.Minute)); !done || fin.Status != flip.ExecStatusFilled {
		t.Fatalf("接管后查到终态 = %+v done=%v, 期望 filled", fin, done)
	}
}

// 接管到一张还活着的遗留挂单（崩溃前挂的, 窗口早已过去）: 撤单点早过 → 立即撤下来
func TestFillTrackerAdoptedStillLiveCancelled(t *testing.T) {
	fc := &fakeClient{ooResp: mkOrder("LIVE", 0)}
	var got []flip.FillFinal
	ft := newTestTracker(fc, func(f flip.FillFinal) { got = append(got, f) })
	ft.Register(mkRestingRec(), testWindowEnd(), true)

	fin, done := ft.pollOne(only(t, ft), time.Unix(testEventStart, 0).Add(10*time.Minute))
	if !done || fin.Status != flip.ExecStatusUnfilled {
		t.Fatalf("定稿 = %+v done=%v, 期望 unfilled", fin, done)
	}
	if len(fc.cancelled) != 1 {
		t.Fatalf("遗留活单应立即撤掉, 实际 %v", fc.cancelled)
	}
}

func TestFillTrackerBaseUnitsAndSuspect(t *testing.T) {
	// size_matched 为 1e6 基单位（5 股 → 5e6）: 超请求股数即判基单位除回
	fc := &fakeClient{ooResp: mkOrder("CANCELED", 5_000_000)}
	var got []flip.FillFinal
	ft := newTestTracker(fc, func(f flip.FillFinal) { got = append(got, f) })
	ft.Register(mkRestingRec(), testWindowEnd(), false)
	if fin, done := ft.pollOne(only(t, ft), testWindowEnd()); !done || !near(fin.Shares, 5) || fin.Status != flip.ExecStatusPartial {
		t.Fatalf("基单位归一 = %+v done=%v, 期望 5 股 partial", fin, done)
	}

	// 除回后仍越界（语义不明）: 保持 resting 交人工, 绝不截断造仓位
	got = nil
	fc.ooResp = mkOrder("LIVE", 1e9)
	ft2 := newTestTracker(fc, func(f flip.FillFinal) { got = append(got, f) })
	ft2.Register(mkRestingRec(), testWindowEnd(), false)
	fin, done := ft2.pollOne(only(t, ft2), atRem(10))
	if !done || fin.Status != flip.ExecStatusResting || !strings.HasPrefix(fin.Note, flip.ExecNoteUnknown) {
		t.Fatalf("越界值 = %+v done=%v, 期望 resting 未确认", fin, done)
	}
}

// 语义可疑的值在撤单点之前不急着交人工: 定稿即移出跟踪表, 而这张单还活在簿上
// （会继续吃进我们不认的成交）——留到撤单点把它从簿上摘下来再交人工。
func TestFillTrackerSuspectHeldUntilCancelPoint(t *testing.T) {
	fc := &fakeClient{ooResp: mkOrder("LIVE", 1e9)}
	var got []flip.FillFinal
	ft := newTestTracker(fc, func(f flip.FillFinal) { got = append(got, f) })
	ft.Register(mkRestingRec(), testWindowEnd(), false)
	o := only(t, ft)

	if _, done := ft.pollOne(o, atRem(240)); done {
		t.Fatalf("撤单点前不应放弃跟踪（定稿即不再撤单）")
	}
	if ft.Count() != 1 {
		t.Fatalf("应仍在跟踪表, 实际 %d", ft.Count())
	}
	if _, done := ft.pollOne(o, atRem(179)); !done { // 撤单点: 先撤, 再交人工
		t.Fatalf("撤单点后应交人工定稿")
	}
	if len(fc.cancelled) != 1 {
		t.Fatalf("可疑挂单也必须撤下来, 实际 %v", fc.cancelled)
	}
}

func TestFillTrackerZeroFillOnClose(t *testing.T) {
	// 观测到过该单（LIVE 0 成交）→ 查不到 = 真的没吃到 → unfilled（0 成交是可信结论）
	fc := &fakeClient{ooResp: mkOrder("LIVE", 0)}
	var got []flip.FillFinal
	ft := newTestTracker(fc, func(f flip.FillFinal) { got = append(got, f) })
	ft.Register(mkRestingRec(), testWindowEnd(), false)
	o := only(t, ft)
	if _, done := ft.pollOne(o, atRem(60)); done {
		t.Fatalf("挂单在簿不应定稿")
	}
	fc.ooResp = nil
	fin, done := ft.pollOne(o, testWindowEnd().Add(30*time.Second))
	if !done || fin.Status != flip.ExecStatusUnfilled || fin.Shares != 0 || fin.Cost != 0 {
		t.Fatalf("零成交定稿 = %+v done=%v, 期望 unfilled", fin, done)
	}
	if strings.HasPrefix(fin.Note, flip.ExecNoteUnknown) {
		t.Fatalf("见过该单就不算未确认: %q", fin.Note)
	}
}

func TestFillTrackerQueryErrorThrottled(t *testing.T) {
	fc := &fakeClient{ooErr: errors.New("boom")}
	ft := newTestTracker(fc, func(flip.FillFinal) { t.Fatalf("查询失败不应定稿") })
	ft.Register(mkRestingRec(), testWindowEnd(), false)
	o := only(t, ft)

	if _, done := ft.pollOne(o, atRem(60)); done {
		t.Fatalf("查询失败且未到截止不应定稿")
	}
	if o.sighted {
		t.Fatalf("查询失败不应记为已观测")
	}
	if fc.cnlCalls != 1 {
		t.Fatalf("查询失败也必须把撤单发出去（漏撤 = 余量继续吃策略不要的成交）, 实际 %d 次", fc.cnlCalls)
	}
	// 到截止仍失败 → 未确认 resting
	fin, done := ft.pollOne(o, testWindowEnd().Add(fillCloseGrace))
	if !done || fin.Status != flip.ExecStatusResting {
		t.Fatalf("截止定稿 = %+v done=%v", fin, done)
	}
}

func TestFillTrackerRegisterGuards(t *testing.T) {
	ft := newTestTracker(&fakeClient{}, func(flip.FillFinal) {})
	ft.Register(nil, testWindowEnd(), false) // nil 记录
	rec := mkRestingRec()
	rec.OrderID = ""
	ft.Register(rec, testWindowEnd(), false) // 缺 order_id
	if ft.Count() != 0 {
		t.Fatalf("信息不全不应入跟踪表, 实际 %d", ft.Count())
	}
	// 幂等: 同一 orderID 重复登记不重复计
	ft.Register(mkRestingRec(), testWindowEnd(), false)
	ft.Register(mkRestingRec(), testWindowEnd(), false)
	if ft.Count() != 1 {
		t.Fatalf("重复登记应幂等, 实际 %d", ft.Count())
	}
	// 撤单提前量非正 → 回退默认（不让配置失误变成「永不撤单」）
	ft2 := NewFillTracker(&fakeClient{}, 0, func(flip.FillFinal) {})
	if ft2.cancelLead != fillCancelLead {
		t.Fatalf("撤单提前量应回退 %v, 实际 %v", fillCancelLead, ft2.cancelLead)
	}
}
