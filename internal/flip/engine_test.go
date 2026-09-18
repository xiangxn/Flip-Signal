package flip

import (
	"math"
	"testing"
)

// 测试常量口径: anchor=100000, σ=7bps（dist = Δ价/70）
// 例: spot 99_986 → Δ −14 → −1.4bp/7 = −0.2σ（浅洞带内, ok 行用）
//
//	spot 100_010 → +1bp/7 ≈ +0.1429σ（带外, dist_out）
//	spot 99_930 → −7bp/7 = −1.0σ（带外）
const (
	tAnchor = 100_000.0
	tHist   = 7.0
)

func cfgOK() Config {
	return DefaultConfig() // 与回测标定一致
}

// newEng 造一个已 BeginWindow 的引擎（默认输入齐备）。
func newEng(cfg Config) *Engine {
	e := NewEngine(cfg)
	e.BeginWindow(tAnchor, tHist)
	return e
}

// stdTick 构造基础 tick（bids>0、spot=anchor、latency=0）。可再覆写字段。
func stdTick(rem int, upAsk, downAsk float64) Tick {
	return Tick{
		Rem:       rem,
		UpBid:     0.8,
		UpAsk:     upAsk,
		DownBid:   0.8,
		DownAsk:   downAsk,
		BinPrice:  tAnchor,
		TwapPrice: 0,
	}
}

// feed 喂 n 个非触发 tick（同侧 ask 历史, 构造急跌窗）。
func feed(t *testing.T, e *Engine, n, fromRem int, askUp, askDown float64) {
	t.Helper()
	for i := 0; i < n; i++ {
		if o := e.ProcessTick(stdTick(fromRem-i, askUp, askDown)); o != nil {
			t.Fatalf("历史 tick rem=%d 意外触发: %+v", fromRem-i, o)
		}
	}
}

func approx(a, b float64) bool { return math.Abs(a-b) < 1e-9 }

// TestTrigger 覆盖触发条件与狗侧选择（镜像回测 :75-92）。
func TestTrigger(t *testing.T) {
	cases := []struct {
		name      string
		feedN     int   // 前置非触发历史 tick 数（构造急跌窗）
		latency   int64 // 触发 tick 盘口延迟（>300 = 无效）
		upBid     float64
		upAsk     float64
		downBid0  bool // 覆写 DownBid=0（整簿门控用例）
		downAsk   float64
		spot      float64 // 覆写 BinPrice（0 = 保持 stdTick 默认 spot=anchor；交叉态选边用）
		wantSide  string
		wantFill  float64
		wantNoObs bool // 期望不产出观测
	}{
		{"首个有效 tick 触底 yes", 0, 0, 0.8, 0.19, false, 0.9, 0, SideYes, 0.19, false},
		{"触底发生在 no 侧", 0, 0, 0.8, 0.55, false, 0.12, 0, SideNo, 0.12, false},
		{"双 ask≤0.2 spot=锚 默认 yes", 0, 0, 0.8, 0.19, false, 0.15, 0, SideYes, 0.19, false},
		{"ask 恰为 0.20 触发", 0, 0, 0.8, 0.20, false, 0.9, 0, SideYes, 0.20, false},
		{"延迟>300ms tick 不检不触发", 0, 400, 0.8, 0.19, false, 0.9, 0, "", 0, true},
		{"UP 报价缺失不触发（bid=0）", 0, 0, 0, 0.19, false, 0.15, 0, "", 0, true},
		{"UP ask=0 不触发", 0, 0, 0.8, 0, false, 0.15, 0, "", 0, true},
		{"Down ask=0 不触发", 0, 0, 0.8, 0.55, false, 0, 0, "", 0, true},
		{"DOWN 簿缺失压制 yes 触底（bid=0）", 0, 0, 0.8, 0.19, true, 0.9, 0, "", 0, true},
		{"DOWN bid=0 压制 no 触底", 0, 0, 0.8, 0.55, true, 0.12, 0, "", 0, true},
		{"两侧均不触底无观测", 0, 0, 0.8, 0.55, false, 0.6, 0, "", 0, true},
		{"交叉态双侧≤0.2 spot>锚 取 no", 0, 0, 0.8, 0.19, false, 0.18, 100_010, SideNo, 0.18, false},
		{"交叉态双侧≤0.2 spot<锚 取 yes", 0, 0, 0.8, 0.19, false, 0.18, 99_990, SideYes, 0.19, false},
		{"交叉态 spot=锚 退回 yes", 0, 0, 0.8, 0.19, false, 0.18, 0, SideYes, 0.19, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			e := newEng(cfgOK())
			feed(t, e, c.feedN, 260, 0.5, 0.5)
			tick := stdTick(250, c.upAsk, c.downAsk)
			tick.UpBid = c.upBid
			if c.downBid0 {
				tick.DownBid = 0
			}
			if c.spot != 0 {
				tick.BinPrice = c.spot
			}
			tick.BookLatMs = c.latency
			o := e.ProcessTick(tick)
			if c.wantNoObs {
				if o != nil {
					t.Fatalf("期望无观测, 得到 %+v", o)
				}
				return
			}
			if o == nil {
				t.Fatal("期望触底观测, 得到 nil")
			}
			if o.Side != c.wantSide || !approx(o.Fill, c.wantFill) {
				t.Fatalf("side/fill = %s/%v, 期望 %s/%v", o.Side, o.Fill, c.wantSide, c.wantFill)
			}
			if o.Rem != 250 {
				t.Fatalf("rem = %d, 期望 250", o.Rem)
			}
		})
	}
}

// TestAnchorZeroWindow 锚缺失窗口**始终未恢复**时整窗不产出观测（镜像回测 :69 锚缺失
// 事件整体跳过）: 触底不落盘（含交叉态），只留 lost_triggers(anchor_pending) 痕迹，
// rem==0 终 tick 照常转 Done。
//
// 窗口内 tick 照常占槽与计数（锚恢复通道的价值所在，见 TestAnchorPendingWindow），
// 故 AnchorMissing 与计数可同时非 0——「锚缺失」的最终含义是窗口结束时锚仍未就绪。
func TestAnchorZeroWindow(t *testing.T) {
	e := NewEngine(cfgOK())
	e.BeginWindow(0, tHist) // anchor 缺失
	for i := 0; i < 3; i++ {
		if o := e.ProcessTick(stdTick(250-i, 0.19, 0.18)); o != nil {
			t.Fatalf("锚缺失窗口不应产出任何观测: %+v", o)
		}
	}
	st := e.WindowStats()
	if !st.AnchorMissing || st.Ticks != 3 || st.TicksValid != 3 {
		t.Fatalf("锚未就绪应标 AnchorMissing 且照常分类计数: %+v", st)
	}
	if len(st.LostTriggers) != 3 {
		t.Fatalf("恢复前的触底应逐 tick 留痕, 得到 %d 条: %+v", len(st.LostTriggers), st.LostTriggers)
	}
	for i, lt := range st.LostTriggers {
		if lt.Reason != LostReasonAnchorPending {
			t.Fatalf("lost[%d].reason = %q, 期望 %q", i, lt.Reason, LostReasonAnchorPending)
		}
	}
	if got := e.State().String(); got != "Watching" {
		t.Fatalf("state = %s, 期望 Watching", got)
	}
	if o := e.ProcessTick(stdTick(0, 0.19, 0.18)); o != nil {
		t.Fatalf("rem==0 终 tick 不应产观测: %+v", o)
	}
	if got := e.State().String(); got != "Done" {
		t.Fatalf("rem==0 后 state = %s, 期望 Done", got)
	}
	if a, _ := e.WindowAnchor(); a != 0 {
		t.Fatalf("未恢复窗口锚应保持 0, 得到 %v", a)
	}
}

// TestAnchorPendingWindow 锚升级通道的核心契约（2026-09-18，docs/dog020_anchor_upgrade_2026-09-18.md）:
// 锚未就绪期 tick 照常占槽（crash 腿拿得到真实盘口历史），触底不产出观测（只留痕），
// UpgradeAnchor 升级后本窗照常判定——且 m_45 能看到**升级前**的 ask（与回测 1:1，回测里锚恒可用）。
func TestAnchorPendingWindow(t *testing.T) {
	e := NewEngine(cfgOK())
	e.BeginWindow(0, 0) // 锚与 σ 均未知（恢复时一并回填）

	// 恢复前: 3 个历史 tick 构造急跌窗（ask 0.55 → 满足 m_45 ≥ 0.40）
	for i := 0; i < 3; i++ {
		tk := stdTick(280-i, 0.55, 0.55)
		tk.BinPrice = 99_986 // −0.2σ（浅洞带内）
		if o := e.ProcessTick(tk); o != nil {
			t.Fatalf("锚未就绪不得产出观测: %+v", o)
		}
	}
	// 恢复前触底: 不产出观测, 只留痕（不追溯——那时锚未知, 用陈旧 ask 成交无意义）
	touch := stdTick(275, 0.19, 0.55)
	touch.BinPrice = 99_986
	if o := e.ProcessTick(touch); o != nil {
		t.Fatalf("锚未就绪期的触底不得产出观测: %+v", o)
	}
	st := e.WindowStats()
	if !st.AnchorMissing || st.Ticks != 4 || st.TicksValid != 4 {
		t.Fatalf("锚未就绪期计数错: %+v", st)
	}
	if len(st.LostTriggers) != 1 || st.LostTriggers[0].Reason != LostReasonAnchorPending ||
		st.LostTriggers[0].Side != SideYes {
		t.Fatalf("恢复前触底留痕错: %+v", st.LostTriggers)
	}

	// 升级锚（升级通道成功）: 锚/σ 就位、AnchorMissing 清除
	if !e.UpgradeAnchor(tAnchor, tHist) {
		t.Fatal("产出观测前升级应被采纳")
	}
	if a, hb := e.WindowAnchor(); !approx(a, tAnchor) || !approx(hb, tHist) {
		t.Fatalf("WindowAnchor = (%v, %v), 期望 (%v, %v)", a, hb, tAnchor, tHist)
	}
	if st := e.WindowStats(); st.AnchorMissing {
		t.Fatalf("升级后应清除 AnchorMissing: %+v", st)
	}
	// 产出观测前可连续覆盖（+1s 缓存重选 → +2s 官方 open 是常态）
	if !e.UpgradeAnchor(tAnchor+1, tHist+1) {
		t.Fatal("产出观测前二次升级应被采纳")
	}
	if a, hb := e.WindowAnchor(); !approx(a, tAnchor+1) || !approx(hb, tHist+1) {
		t.Fatalf("二次升级未生效: (%v, %v)", a, hb)
	}
	e.UpgradeAnchor(tAnchor, tHist) // 复位到本用例后续断言用的值

	// 回填后触底 → 正常判定: crash 腿看到恢复前的 ask（m_45 = 0.55）
	tk := stdTick(270, 0.19, 0.55)
	tk.BinPrice = 99_986
	o := e.ProcessTick(tk)
	if o == nil {
		t.Fatal("锚回填后触底应产出观测")
	}
	if !o.OK || o.Side != SideYes || !approx(o.Fill, 0.19) {
		t.Fatalf("观测错: %+v", o)
	}
	if !approx(o.M45, 0.55) {
		t.Fatalf("m_45 = %v, 期望 0.55（恢复前占槽的 ask 必须留在 ring 里）", o.M45)
	}
	if !approx(o.Anchor, tAnchor) || !approx(o.HistBps, tHist) {
		t.Fatalf("观测未带上恢复的锚/σ: anchor=%v hist_bps=%v", o.Anchor, o.HistBps)
	}
	if !approx(o.DistS, -0.2) {
		t.Fatalf("dist_s = %v, 期望 -0.2", o.DistS)
	}
	if got := e.State().String(); got != "Done" {
		t.Fatalf("判定后 state = %s, 期望 Done", got)
	}
}

// TestUpgradeAnchorInvalid 升级非法锚（≤0）不改变窗口状态并返回 false
// （防线: 升级通道只送 >0 值）。
func TestUpgradeAnchorInvalid(t *testing.T) {
	e := NewEngine(cfgOK())
	e.BeginWindow(0, 0)
	for _, bad := range []float64{0, -1} {
		if e.UpgradeAnchor(bad, tHist) {
			t.Fatalf("非法锚 %v 不得被采纳", bad)
		}
	}
	if a, hb := e.WindowAnchor(); a != 0 || hb != 0 {
		t.Fatalf("非法锚不得升级: (%v, %v)", a, hb)
	}
}

// TestUpgradeAnchorFrozenAfterObserve 产出观测后锚冻结: 判定入口即终点, 已落盘的
// 观测行不可追溯改写——其后到达的升级值一律丢弃并返回 false（2026-09-18 锚升级通道；
// 判定用的锚与升级通道是并发的，窗口内官方 open 可能晚到）。
func TestUpgradeAnchorFrozenAfterObserve(t *testing.T) {
	e := NewEngine(cfgOK())
	e.BeginWindow(tAnchor, tHist)
	for i := 0; i < 3; i++ { // 急跌窗历史（ask 0.55）
		tk := stdTick(280-i, 0.55, 0.55)
		tk.BinPrice = 99_986
		e.ProcessTick(tk)
	}
	touch := stdTick(275, 0.19, 0.55)
	touch.BinPrice = 99_986
	if o := e.ProcessTick(touch); o == nil || !o.OK {
		t.Fatalf("首触应产出 ok 观测: %+v", o)
	}
	if e.UpgradeAnchor(tAnchor+5, tHist) {
		t.Fatal("产出观测后升级应被拒绝")
	}
	if a, hb := e.WindowAnchor(); !approx(a, tAnchor) || !approx(hb, tHist) {
		t.Fatalf("冻结后锚/σ 不得变: (%v, %v)", a, hb)
	}
}

// TestUpgradeAnchorFrozenAtWindowEnd rem==0 终 tick 后同样冻结（第二种 Done）:
// 窗口收尾与升级通道的 cancel+join 之间有毫秒级窗口，此处钉死「终 tick 之后不认」。
func TestUpgradeAnchorFrozenAtWindowEnd(t *testing.T) {
	e := NewEngine(cfgOK())
	e.BeginWindow(tAnchor, tHist)
	if o := e.ProcessTick(stdTick(0, 0.55, 0.55)); o != nil {
		t.Fatalf("rem==0 终 tick 不产出观测: %+v", o)
	}
	if e.UpgradeAnchor(tAnchor+5, tHist) {
		t.Fatal("rem==0 后升级应被拒绝")
	}
	if a, _ := e.WindowAnchor(); !approx(a, tAnchor) {
		t.Fatalf("终 tick 后锚不得变: %v", a)
	}
}

// TestDecideMissingAnchor decide 纯函数防线：直接调用下锚缺失输入仍回 missing_anchor
// （ProcessTick 已整窗早退，此路径现网不可达，防未来直调层回归）。
func TestDecideMissingAnchor(t *testing.T) {
	e := NewEngine(cfgOK())
	e.BeginWindow(0, tHist) // anchor 缺失
	o := e.decide(stdTick(250, 0.19, 0.9), SideYes)
	if o == nil || o.RejectReason != RejectMissingAnchor {
		t.Fatalf("decide(anchor=0) = %+v, 期望 missing_anchor", o)
	}
}

// TestFirstOnly 事件内仅首个观测、无重试；判定后状态 Done。
func TestFirstOnly(t *testing.T) {
	e := newEng(cfgOK())
	feed(t, e, 5, 260, 0.5, 0.5)
	tick := stdTick(250, 0.19, 0.9)
	tick.BinPrice = 99_986 // 浅洞带内（dist_s = −0.2）
	o := e.ProcessTick(tick)
	if o == nil || !o.OK {
		t.Fatalf("首个触底应 ok: %+v", o)
	}
	if got := e.State().String(); got != "Done" {
		t.Fatalf("state = %s, 期望 Done", got)
	}
	// 后续更深的触底不再观测（Done 态直接 nil）
	for i := 0; i < 3; i++ {
		tk := stdTick(250-i, 0.1, 0.9)
		if o := e.ProcessTick(tk); o != nil {
			t.Fatalf("Done 后不应再观测: %+v", o)
		}
	}
}

// TestRejectReasons 覆盖四腿判定与各拒绝原因 + 判定顺序。
func TestRejectReasons(t *testing.T) {
	cases := []struct {
		name       string
		begin      func(*Engine) // 覆写 BeginWindow 输入（默认齐备）
		histN      int           // 急跌历史（0 = 无 → no_crash）
		rem        int
		spot       float64 // BinPrice（0 = 缺失）
		expReason  string
		wantOK     bool
		checkDistS bool // 期望行记录了 dist_s
		expDistS   float64
	}{
		{"rem_low（rem=180 边界）", nil, 5, 180, 99_986, RejectRemLow, false, true, -0.2},
		{"no_hist（σ 窗口不可用）", func(e *Engine) { e.BeginWindow(tAnchor, 0) }, 5, 250, 99_986, RejectNoHist, false, false, 0},
		{"missing_spot（无 Binance 价）", nil, 5, 250, 0, RejectMissingSpot, false, false, 0},
		{"no_crash（窗内从未 ≥0.40）", nil, 0, 250, 99_986, RejectNoCrash, false, true, -0.2},
		{"dist_out（spot 已过锚 +0.143σ）", nil, 5, 250, 100_010, RejectDistOut, false, true, 0.14285714285714285},
		{"dist_out（下界外 −1.0σ）", nil, 5, 250, 99_930, RejectDistOut, false, true, -1.0},
		{"全腿通过 → ok", nil, 5, 250, 99_986, "", true, true, -0.2},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			e := newEng(cfgOK())
			if c.begin != nil {
				c.begin(e)
			}
			feed(t, e, c.histN, 260, 0.5, 0.5)
			tick := stdTick(c.rem, 0.19, 0.9)
			tick.BinPrice = c.spot
			o := e.ProcessTick(tick)
			if o == nil {
				t.Fatal("触底应产出观测")
			}
			if o.RejectReason != c.expReason || o.OK != c.wantOK {
				t.Fatalf("reason/ok = %q/%v, 期望 %q/%v", o.RejectReason, o.OK, c.expReason, c.wantOK)
			}
			if c.checkDistS && !approx(o.DistS, c.expDistS) {
				t.Fatalf("dist_s = %v, 期望 %v", o.DistS, c.expDistS)
			}
			if o.OK {
				if want := cfgOK().Stake / 0.19; !approx(o.Shares, want) {
					t.Fatalf("shares = %v, 期望 %v", o.Shares, want)
				}
			}
		})
	}
}

// TestSideBand 侧别浅洞带（2026-09-03 组合版默认: yes (−0.6, 0) / no (−1, 0)）:
// no 因 spot 领先 TWAP 天然深一档 → 放宽到 −1.0; yes 破位下行中继 → 只到 −0.6。
func TestSideBand(t *testing.T) {
	cases := []struct {
		name    string
		upAsk   float64 // 触底侧（ask ≤ 0.2 的一侧为狗）
		downAsk float64
		spot    float64 // BinPrice（σ=7bps: dist = Δ/70）
		wantOK  bool
		expDist float64
	}{
		// no 深坑 −0.7σ（Binance 超锚 +0.7σ, sgn=−1）: no 带内 → ok
		//（旧双侧 (−0.5,0) 会 dist_out——引擎侧别放宽即此新增行为）
		{"no 深坑 −0.7σ 带内 ok", 0.9, 0.19, 100_049, true, -0.7},
		// no 恰 −1.0σ 边界: 开区间不含下界 → dist_out
		{"no 恰 −1.0σ 边界带外", 0.9, 0.19, 100_070, false, -1.0},
		// yes −0.55σ: 在 yes 带 (−0.6,0) 内（旧 (−0.5,0) 会拒——yes 加深段生效）
		{"yes −0.55σ 加深段带内 ok", 0.19, 0.9, 99_961.5, true, -0.55},
		// yes −0.7σ 深坑: yes 带外 → dist_out（no 深带不适用于 yes 侧）
		{"yes −0.7σ 深坑带外", 0.19, 0.9, 99_951, false, -0.7},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			e := newEng(cfgOK())
			feed(t, e, 5, 260, 0.5, 0.5) // 急跌历史（ask 曾 ≥0.40 已构造）
			tick := stdTick(250, c.upAsk, c.downAsk)
			tick.BinPrice = c.spot
			o := e.ProcessTick(tick)
			if o == nil {
				t.Fatal("触底应产出观测")
			}
			if !approx(o.DistS, c.expDist) {
				t.Fatalf("dist_s = %v, 期望 %v", o.DistS, c.expDist)
			}
			if c.wantOK && (!o.OK || o.RejectReason != "") {
				t.Fatalf("应 ok, 得到 reason=%q ok=%v", o.RejectReason, o.OK)
			}
			if !c.wantOK && o.OK {
				t.Fatal("应 dist_out, 得到 ok")
			}
			if !c.wantOK && o.RejectReason != RejectDistOut {
				t.Fatalf("reason = %q, 期望 dist_out", o.RejectReason)
			}
		})
	}
}

// TestMWindow 急跌窗（索引槽位）语义。
func TestMWindow(t *testing.T) {
	t.Run("无效 tick 占槽但不贡献", func(t *testing.T) {
		e := newEng(cfgOK())
		feed(t, e, 44, 300, 0.5, 0.5)
		bad := stdTick(255, 0.9, 0.9)
		bad.BookLatMs = 500 // 高延迟: ask 0.9 不贡献
		if o := e.ProcessTick(bad); o != nil {
			t.Fatalf("高延迟 tick 不应触发: %+v", o)
		}
		tick := stdTick(254, 0.19, 0.9)
		tick.BinPrice = 99_986 // 浅洞带内
		o := e.ProcessTick(tick)
		if o == nil || !o.OK {
			t.Fatalf("m45=0.5 应 ok: %+v", o)
		}
		if !approx(o.M45, 0.5) {
			t.Fatalf("m45 = %v, 期望 0.5", o.M45)
		}
	})

	t.Run("窗头截断（老槽位滑出）", func(t *testing.T) {
		e := newEng(cfgOK())
		// 第 1 个 tick ask 0.9（rem 300）, 其后 45 个 tick ask 0.3
		if o := e.ProcessTick(stdTick(300, 0.9, 0.9)); o != nil {
			t.Fatal("历史 tick 不应触发")
		}
		feed(t, e, 45, 299, 0.3, 0.3)
		o := e.ProcessTick(stdTick(253, 0.19, 0.9))
		if o == nil {
			t.Fatal("应产出观测")
		}
		// 46 个槽位: 0.9 已滑出 ring（45 槽）→ m45 = 0.3 < 0.4 → no_crash
		if o.RejectReason != RejectNoCrash || !approx(o.M45, 0.3) {
			t.Fatalf("reason/m45 = %q/%v, 期望 no_crash/0.3", o.RejectReason, o.M45)
		}
	})

	t.Run("m20/m30 分层", func(t *testing.T) {
		e := newEng(cfgOK())
		// 5 个 ask 0.5 在最老, 20 个 ask 0.3 在最近 → m20=0.3, m30=0.5
		feed(t, e, 5, 300, 0.5, 0.5)
		feed(t, e, 20, 295, 0.3, 0.3)
		o := e.ProcessTick(stdTick(274, 0.19, 0.9))
		if o == nil {
			t.Fatal("应产出观测")
		}
		if !approx(o.M20, 0.3) || !approx(o.M30, 0.5) || !approx(o.M45, 0.5) {
			t.Fatalf("m20/m30/m45 = %v/%v/%v, 期望 0.3/0.5/0.5", o.M20, o.M30, o.M45)
		}
	})
}

// TestDistSignAndTwap 狗侧符号与 dist_t 观察腿。
func TestDistSignAndTwap(t *testing.T) {
	t.Run("dog=no 时 sgn=−1", func(t *testing.T) {
		e := newEng(cfgOK())
		feed(t, e, 5, 260, 0.6, 0.5) // no 侧急跌历史
		tick := stdTick(250, 0.6, 0.12)
		tick.BinPrice = 100_014 // 现货已在 UP 侧 +0.2σ → 对 dog=no 是 −0.2σ（浅洞）
		o := e.ProcessTick(tick)
		if o == nil || o.Side != SideNo {
			t.Fatalf("应 dog=no: %+v", o)
		}
		if !o.OK || !approx(o.DistS, -0.2) {
			t.Fatalf("dist_s = %v (ok=%v), 期望 −0.2/ok", o.DistS, o.OK)
		}
	})

	t.Run("dist_t 观察腿记录", func(t *testing.T) {
		e := newEng(cfgOK())
		feed(t, e, 5, 260, 0.5, 0.5)
		tick := stdTick(250, 0.19, 0.9)
		tick.BinPrice = 99_986
		tick.TwapPrice = 99_972 // TWAP 距锚 −2.8bp/7 = −0.4σ
		o := e.ProcessTick(tick)
		if o == nil || !o.OK {
			t.Fatalf("应 ok: %+v", o)
		}
		if !approx(o.DistS, -0.2) || !approx(o.DistT, -0.4) {
			t.Fatalf("dist_s/dist_t = %v/%v, 期望 −0.2/−0.4", o.DistS, o.DistT)
		}
	})

	t.Run("TWAP 缺失时 dist_t 为 0（不带出）", func(t *testing.T) {
		e := newEng(cfgOK())
		feed(t, e, 5, 260, 0.5, 0.5)
		tick := stdTick(250, 0.19, 0.9)
		tick.BinPrice = 99_986 // TwapPrice 默认 0
		o := e.ProcessTick(tick)
		if o == nil || !o.OK || o.DistT != 0 {
			t.Fatalf("dist_t 应 0: %+v", o)
		}
	})
}

// TestWindowEnd 窗口结束终 tick 与状态复位。
func TestWindowEnd(t *testing.T) {
	t.Run("rem=0 终 tick 置 Done 不观测", func(t *testing.T) {
		e := newEng(cfgOK())
		feed(t, e, 5, 300, 0.5, 0.5)
		if o := e.ProcessTick(stdTick(0, 0.1, 0.9)); o != nil {
			t.Fatalf("终 tick 不应观测: %+v", o)
		}
		if got := e.State(); got != stateDone {
			t.Fatalf("state = %v, 期望 Done", got)
		}
	})

	t.Run("BeginWindow 复位可复用", func(t *testing.T) {
		e := newEng(cfgOK())
		if got := e.State(); got != stateWatching {
			t.Fatalf("初始 state = %v, 期望 Watching", got)
		}
		o := e.ProcessTick(stdTick(250, 0.19, 0.9)) // 无急跌 → no_crash 观测
		if o == nil || o.RejectReason != RejectNoCrash {
			t.Fatalf("应产出 no_crash 观测: %+v", o)
		}
		if got := e.State(); got != stateDone {
			t.Fatalf("state = %v, 期望 Done", got)
		}
		e.BeginWindow(99_900, 6.0) // 下个窗口
		if got := e.State(); got != stateWatching {
			t.Fatalf("复位后 state = %v, 期望 Watching", got)
		}
		feed(t, e, 5, 260, 0.5, 0.5)
		tick := stdTick(250, 0.19, 0.9)
		tick.BinPrice = 99_886 // 距新锚 99_900: Δ −14/6σ ≈ −0.23σ（带内）
		if o := e.ProcessTick(tick); o == nil || !o.OK {
			t.Fatalf("新窗口应可观测 ok: %+v", o)
		}
	})
}

// TestConfigAccessor 参数透传。
func TestConfigAccessor(t *testing.T) {
	c := cfgOK()
	e := NewEngine(c)
	if got := e.Config(); got != c {
		t.Fatalf("Config() = %+v, 期望 %+v", got, c)
	}
}
