package settle

import (
	"context"
	"testing"
	"time"

	"github.com/necklace/flip-signal/internal/flip"
)

// 词面映射红线: settle 刻意不 import flip（结算层不依赖某条策略），两组常量必须同值
// ——反向写错（Up/Down 对调）会让每一笔结算都反着记账，故在此钉死。
func TestOutcomeVocabularyMatchesFlip(t *testing.T) {
	if outcomeUp != flip.OutcomeUp || outcomeDown != flip.OutcomeDown {
		t.Fatalf("outcome 词面与 flip 不一致: settle{%d,%d} vs flip{%d,%d}",
			outcomeUp, outcomeDown, flip.OutcomeUp, flip.OutcomeDown)
	}
}

func TestOutcome(t *testing.T) {
	cases := []struct {
		name        string
		open, close float64
		want        int
	}{
		{"涨 → Up", 84000.0, 84001.5, outcomeUp},
		{"跌 → Down", 84000.0, 83999.5, outcomeDown},
		{"平局 → Down（与官方严格不等号同口径）", 84000.0, 84000.0, outcomeDown},
		{"极小差（天平窗）也按严格不等号判", 84000.0, 84000.079, outcomeUp},
	}
	for _, c := range cases {
		if got := Outcome(c.open, c.close); got != c.want {
			t.Errorf("%s: Outcome(%v, %v) = %d, want %d", c.name, c.open, c.close, got, c.want)
		}
	}
}

func TestAnchors(t *testing.T) {
	a := NewAnchors()
	// 非法值不入表（推送缺失的占位值）
	a.Put(0, 100)
	a.Put(100, 0)
	a.Put(100, -1)
	if _, ok := a.Get(100); ok {
		t.Fatal("price ≤0 不应入表")
	}
	a.Put(100, 84000)
	if v, ok := a.Get(100); !ok || v != 84000 {
		t.Fatalf("Get(100) = %v,%v, want 84000,true", v, ok)
	}
	// 同一边界重复 Put 覆盖（同一边界只有一条精确推送）
	a.Put(100, 84002)
	if v, _ := a.Get(100); v != 84002 {
		t.Fatalf("重复 Put 应覆盖, got %v", v)
	}

	cases := []struct {
		name        string
		put         []int64
		eventStart  int64
		wantOutcome int
		wantOK      bool
	}{
		{"两个边界都在 → Up", []int64{100, 400}, 100, outcomeUp, true},
		{"缺下一边界 → 不成", []int64{100}, 100, 0, false},
		{"缺本边界 → 不成", []int64{400}, 100, 0, false},
		{"窗口跨度过大 → 不成", []int64{100, 700}, 100, 0, false},
	}
	for _, c := range cases {
		a := NewAnchors()
		for _, s := range c.put {
			a.Put(s, 84000+float64(s)/1000)
		}
		got, ok := a.OutcomeFor(c.eventStart)
		if ok != c.wantOK || (ok && got != c.wantOutcome) {
			t.Errorf("%s: OutcomeFor = %d,%v, want %d,%v", c.name, got, ok, c.wantOutcome, c.wantOK)
		}
	}
	// nil 安全（构造失败/未接线时不应 panic）
	var nilA *Anchors
	nilA.Put(100, 1)
	if _, ok := nilA.Get(100); ok {
		t.Error("nil Anchors 不应命中")
	}
}

// resolverHarness 把 Resolver 的可观测面收成一个测试夹具。
type resolverHarness struct {
	r       *Resolver
	start   int64 // 窗口起点（unix 秒）
	settled []struct {
		row     Row
		outcome int
		src     string
	}
	gaveUp  []Row
	pending []Row
}

func newHarness(t *testing.T, opt Options) *resolverHarness {
	t.Helper()
	h := &resolverHarness{start: 1_700_000_000}
	h.pending = []Row{{ConditionID: "0xwin", Slug: "btc-updown-5m-x", EventStart: h.start}}
	opt.Pending = func() []Row { return h.pending }
	opt.Settle = func(row Row, outcome int, src string) bool {
		h.settled = append(h.settled, struct {
			row     Row
			outcome int
			src     string
		}{row, outcome, src})
		return true
	}
	opt.GiveUp = func(row Row) { h.gaveUp = append(h.gaveUp, row) }
	h.r = New(NewAnchors(), opt)
	return h
}

// 窗口结束时刻（unix 秒）之后的 offset 处（测试统一用它表达扫描时刻）。
func (h *resolverHarness) at(offset time.Duration) time.Time {
	return time.Unix(h.start, 0).Add(windowSpan).Add(offset)
}

// 推送层: 两条边界推送在手 → 闭市 +25s 定案, src=push; 未到点则一动不动。
func TestResolverPushTier(t *testing.T) {
	h := newHarness(t, Options{Fetch: func(context.Context, time.Time, time.Time) (float64, float64) {
		t.Error("推送命中时不该走官方层")
		return 0, 0
	}})
	h.r.anchors.Put(h.start, 84000)
	h.r.anchors.Put(h.start+300, 84002)

	h.r.tick(context.Background(), h.at(0))
	h.r.tick(context.Background(), h.at(24*time.Second))
	if len(h.settled) != 0 {
		t.Fatalf("未到 +25s 不该定案（取锚预算未走完）, 实际结算 %d 条", len(h.settled))
	}
	h.r.tick(context.Background(), h.at(25*time.Second))
	if len(h.settled) != 1 || h.settled[0].src != SrcPush || h.settled[0].outcome != outcomeUp {
		t.Fatalf("+25s 应推送自算结算 Up, got %+v", h.settled)
	}
}

// 官方层: 推送缺失 → 等过收敛点（+45s）才取数, src=official。
func TestResolverOfficialTier(t *testing.T) {
	var fetched []time.Time
	h := newHarness(t, Options{Fetch: func(_ context.Context, ws, we time.Time) (float64, float64) {
		fetched = append(fetched, ws)
		return 84000, 83998
	}})
	h.r.anchors.Put(h.start, 84000) // 只有本窗边界, 缺下一窗 → 推送层不成

	h.r.tick(context.Background(), h.at(30*time.Second))
	if len(fetched) != 0 {
		t.Fatalf("+30s 早于官方收敛点, 不该取数（临时值）")
	}
	h.r.tick(context.Background(), h.at(45*time.Second))
	if len(fetched) != 1 {
		t.Fatalf("+45s 应取一次官方数, got %d", len(fetched))
	}
	if len(h.settled) != 1 || h.settled[0].src != SrcOfficial || h.settled[0].outcome != outcomeDown {
		t.Fatalf("官方层应结算 Down, got %+v", h.settled)
	}
	if want := time.Unix(h.start, 0); !fetched[0].Equal(want) {
		t.Fatalf("取数窗口起点 = %v, want %v", fetched[0], want)
	}
}

// 官方取数未就绪（返回 0）→ 重试; 用尽 → 交 gamma 且只交一次。
func TestResolverGiveUp(t *testing.T) {
	calls := 0
	h := newHarness(t, Options{MaxTries: 3, Fetch: func(context.Context, time.Time, time.Time) (float64, float64) {
		calls++
		return 0, 0
	}})

	for i := 0; i < 6; i++ {
		h.r.tick(context.Background(), h.at(time.Duration(45+i*5)*time.Second))
	}
	if calls != 3 {
		t.Fatalf("官方应恰好试 %d 次, got %d", 3, calls)
	}
	if len(h.gaveUp) != 1 {
		t.Fatalf("应恰好交 gamma 一次, got %d", len(h.gaveUp))
	}
	if len(h.settled) != 0 {
		t.Fatalf("不该结算, got %+v", h.settled)
	}
	// 交出去之后即使行还在 pending（gamma 尚未回填）也不再取数/重复注册
	for i := 0; i < 3; i++ {
		h.r.tick(context.Background(), h.at(time.Duration(90+i*5)*time.Second))
	}
	if calls != 3 || len(h.gaveUp) != 1 {
		t.Fatalf("交出后应静止: calls=%d gaveUp=%d", calls, len(h.gaveUp))
	}
}

// 未接官方层（Fetch=nil）→ 推送缺失时直接交 gamma, 不空转。
func TestResolverNoOfficialLayer(t *testing.T) {
	h := newHarness(t, Options{})
	h.r.tick(context.Background(), h.at(time.Second))
	if len(h.gaveUp) != 0 {
		t.Fatal("未到 +25s 不该交 gamma")
	}
	h.r.tick(context.Background(), h.at(30*time.Second))
	if len(h.gaveUp) != 1 || len(h.settled) != 0 {
		t.Fatalf("缺官方层应直接交 gamma, got settled=%d gaveUp=%d", len(h.settled), len(h.gaveUp))
	}
}

// 窗口未结束的行不碰（信号在盘中就注册）。
func TestResolverIgnoresOpenWindow(t *testing.T) {
	h := newHarness(t, Options{Fetch: func(context.Context, time.Time, time.Time) (float64, float64) {
		t.Error("盘中不该取官方数")
		return 0, 0
	}})
	h.r.anchors.Put(h.start, 84000)
	h.r.anchors.Put(h.start+300, 84002)
	for _, off := range []time.Duration{-300 * time.Second, -1 * time.Second, 0} {
		h.r.tick(context.Background(), h.at(off))
	}
	if len(h.settled) != 0 || len(h.gaveUp) != 0 {
		t.Fatalf("盘中行不该被动: settled=%d gaveUp=%d", len(h.settled), len(h.gaveUp))
	}
}

// 行离开待结算集合后簿记清理（tries/given 不随进程寿命增长）。
func TestResolverPrunesBookkeeping(t *testing.T) {
	h := newHarness(t, Options{MaxTries: 1, Fetch: func(context.Context, time.Time, time.Time) (float64, float64) {
		return 0, 0
	}})
	h.r.tick(context.Background(), h.at(45*time.Second))
	if len(h.r.tries) != 1 || len(h.r.given) != 0 {
		t.Fatalf("首次尝试只记次数: tries=%d given=%d", len(h.r.tries), len(h.r.given))
	}
	h.r.tick(context.Background(), h.at(45*time.Second)) // 次数用尽 → 交 gamma
	if len(h.r.tries) != 1 || len(h.r.given) != 1 {
		t.Fatalf("应有簿记各一条: tries=%d given=%d", len(h.r.tries), len(h.r.given))
	}
	h.pending = nil // 该行已结算/移除
	h.r.tick(context.Background(), h.at(50*time.Second))
	if len(h.r.tries) != 0 || len(h.r.given) != 0 {
		t.Fatalf("行离开后簿记应清空: tries=%d given=%d", len(h.r.tries), len(h.r.given))
	}
}

// 回调缺失（未接线）时 tick 静默返回, 不 panic。
func TestResolverNilCallbacks(t *testing.T) {
	r := New(NewAnchors(), Options{})
	r.tick(context.Background(), time.Now())
}
