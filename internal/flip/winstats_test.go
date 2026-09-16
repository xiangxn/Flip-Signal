package flip

import "testing"

// 2026-09-16 每窗 tick 健康度计数（docs/dog020_risk_latency_plan_2026-09-16.md §2.4）。
// 红线: 计数器只累加，不改变任何分支走向（btreplay 逐位对账）——本文件用
// TestTouchSideMirrorsTrigger 钉住诊断镜像与真实触发 switch 的等价性。

// TestWindowStatsCounters 计数、明细与恒等式 Ticks == TicksValid + BookStale + BookMissing。
func TestWindowStatsCounters(t *testing.T) {
	e := newEng(cfgOK())

	// 2 个延迟超阈 tick（asks 0.9，本不会触发 → 不进 LostTriggers）
	for i := 0; i < 2; i++ {
		tk := stdTick(280-i, 0.9, 0.9)
		tk.BookLatMs = 400
		if o := e.ProcessTick(tk); o != nil {
			t.Fatalf("延迟超阈 tick 不得产出观测: %+v", o)
		}
	}
	// 1 个延迟超阈但本会触发 yes 的 tick（信号被闸挡掉的核心场景）
	lost := stdTick(275, 0.19, 0.9)
	lost.BookLatMs = 301
	if o := e.ProcessTick(lost); o != nil {
		t.Fatalf("stale tick 不得产出观测（信号应被闸挡掉并记账）")
	}
	// 1 个整簿缺失（DownBid=0）但本会触发 yes
	missYes := stdTick(274, 0.18, 0.9)
	missYes.DownBid = 0
	if o := e.ProcessTick(missYes); o != nil {
		t.Fatalf("整簿缺失 tick 不得产出观测")
	}
	// 1 个整簿缺失（UpBid=0）但本会触发 no
	missNo := stdTick(273, 0.9, 0.19)
	missNo.UpBid = 0
	if o := e.ProcessTick(missNo); o != nil {
		t.Fatalf("整簿缺失 tick 不得产出观测")
	}
	// 3 个有效 tick（不触底）
	feed(t, e, 3, 272, 0.9, 0.9)

	st := e.WindowStats()
	if st.AnchorMissing {
		t.Fatal("正常窗口不得标 AnchorMissing")
	}
	if st.Ticks != 8 || st.TicksValid != 3 || st.BookStale != 3 || st.BookMissing != 2 {
		t.Fatalf("计数错: ticks=%d valid=%d stale=%d missing=%d（期望 8/3/3/2）",
			st.Ticks, st.TicksValid, st.BookStale, st.BookMissing)
	}
	if st.Ticks != st.TicksValid+st.BookStale+st.BookMissing {
		t.Fatalf("恒等式破: %d != %d+%d+%d", st.Ticks, st.TicksValid, st.BookStale, st.BookMissing)
	}

	want := []LostTrigger{
		{Ts: 0, Side: SideYes, Rem: 275, Ask: 0.19, BookLatMs: 301, Reason: LostReasonStaleBook},
		{Ts: 0, Side: SideYes, Rem: 274, Ask: 0.18, BookLatMs: 0, Reason: LostReasonBookMissing},
		{Ts: 0, Side: SideNo, Rem: 273, Ask: 0.19, BookLatMs: 0, Reason: LostReasonBookMissing},
	}
	if len(st.LostTriggers) != len(want) {
		t.Fatalf("丢信号明细 %d 条, 期望 %d 条: %+v", len(st.LostTriggers), len(want), st.LostTriggers)
	}
	for i, w := range want {
		got := st.LostTriggers[i]
		got.Ts = 0 // stdTick 不设 Ts
		if got != w {
			t.Fatalf("丢信号[%d] = %+v, 期望 %+v", i, got, w)
		}
	}

	// 返回的是深拷贝: 改返回值不得影响引擎内部
	st.LostTriggers[0].Side = "poisoned"
	if again := e.WindowStats(); again.LostTriggers[0].Side != SideYes {
		t.Fatal("WindowStats 未深拷贝 LostTriggers（内部状态被改写）")
	}

	// BeginWindow 重置本窗计数
	e.BeginWindow(tAnchor, tHist)
	if z := e.WindowStats(); z.Ticks != 0 || z.BookStale != 0 || len(z.LostTriggers) != 0 || z.AnchorMissing {
		t.Fatalf("BeginWindow 未重置计数: %+v", z)
	}
}

// TestWindowStatsAnchorMissing 锚未就绪窗口: 标位、照常分类计数、触底记
// anchor_pending、不产出观测（未恢复时镜像回测整窗跳过，见 engine_test.go
// TestAnchorPendingWindow 的恢复路径）。
func TestWindowStatsAnchorMissing(t *testing.T) {
	e := NewEngine(cfgOK())
	e.BeginWindow(0, tHist) // anchor≤0 = 锚未就绪
	feed(t, e, 3, 280, 0.19, 0.19)
	st := e.WindowStats()
	if !st.AnchorMissing {
		t.Fatal("锚未就绪窗口应标 AnchorMissing")
	}
	// 恢复通道可能稍后回填锚 → 窗口内照常占槽（crash 腿需要真实历史）,
	// 计数与恒等式同正常窗口
	if st.Ticks != 3 || st.TicksValid != 3 || st.BookStale != 0 || st.BookMissing != 0 {
		t.Fatalf("锚未就绪窗口计数错: %+v", st)
	}
	if st.Ticks != st.TicksValid+st.BookStale+st.BookMissing {
		t.Fatalf("恒等式破: %+v", st)
	}
	if len(st.LostTriggers) != 3 {
		t.Fatalf("触底应逐 tick 留痕 anchor_pending: %+v", st.LostTriggers)
	}
	for i, lt := range st.LostTriggers {
		if lt.Reason != LostReasonAnchorPending {
			t.Fatalf("lost[%d].reason = %q, 期望 %q", i, lt.Reason, LostReasonAnchorPending)
		}
	}
}

// TestWindowStatsFrozenAfterDone 判定完成后（Done/终 tick）计数冻结——不再分类本窗余下 tick。
func TestWindowStatsFrozenAfterDone(t *testing.T) {
	e := newEng(cfgOK())
	feed(t, e, 45, 250, 0.6, 0.6)

	trig := stdTick(200, 0.18, 0.6)
	trig.BinPrice = 99_986 // −0.2σ: 浅洞带内（spot=anchor 时 dist_s=0 落带外）
	obs := e.ProcessTick(trig)
	if obs == nil || !obs.OK {
		t.Fatalf("基准触发失败: %+v", obs)
	}
	before := e.WindowStats()

	for i := 0; i < 5; i++ {
		tk := stdTick(195-i, 0.9, 0.9)
		tk.BookLatMs = 5000
		if o := e.ProcessTick(tk); o != nil {
			t.Fatalf("Done 后不得产出观测")
		}
	}
	after := e.WindowStats()
	if after.Ticks != before.Ticks || after.BookStale != before.BookStale {
		t.Fatalf("Done 后计数仍在变: %+v → %+v", before, after)
	}
}

// TestTouchSideMirrorsTrigger 钉住 touchSide 与 ProcessTick 内联触发 switch 的等价性
// （引擎文档: 两处必须同步修改）。过整簿门控的 tick 上二者必须逐条一致
// （是否触发 + 触发侧）；不过门控的 tick 上 touchSide 仍须报出本会触发侧
// （诊断口径——多一条 ask>0 守卫，ask=0 不算触底，正是 book_missing 的来源）。
func TestTouchSideMirrorsTrigger(t *testing.T) {
	cases := []struct {
		name             string
		upBid, upAsk     float64
		downBid, downAsk float64
		spot             float64 // 0 = anchor（交叉态判边用）
		wantSide         string  // "" = 无触底
		valid            bool    // 是否过整簿四字段门控
	}{
		{"yes 侧触底", 0.8, 0.19, 0.8, 0.9, 0, SideYes, true},
		{"no 侧触底", 0.8, 0.9, 0.8, 0.19, 0, SideNo, true},
		{"交叉态 spot=anchor 退回 yes", 0.8, 0.19, 0.8, 0.19, 0, SideYes, true},
		{"交叉态 spot>anchor 取 no", 0.8, 0.19, 0.8, 0.19, 100_100, SideNo, true},
		{"交叉态 spot<anchor 取 yes", 0.8, 0.19, 0.8, 0.19, 99_900, SideYes, true},
		{"边界 ask=0.20 触底", 0.8, 0.2, 0.8, 0.9, 0, SideYes, true},
		{"边界外 ask=0.21 不触底", 0.8, 0.21, 0.8, 0.9, 0, "", true},
		{"双侧不触底", 0.8, 0.9, 0.8, 0.9, 0, "", true},
		{"整簿缺失: yes 触底仍记账", 0, 0.18, 0.8, 0.9, 0, SideYes, false},
		{"整簿缺失: no 触底仍记账", 0, 0.9, 0.8, 0.19, 0, SideNo, false},
		{"ask=0 不算触底", 0.8, 0, 0.8, 0.9, 0, "", false},
		{"ask=0 一侧不挡另一侧", 0.8, 0, 0.8, 0.19, 0, SideNo, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			e := newEng(cfgOK())
			feed(t, e, lookMax, 250, 0.6, 0.6) // 急跌窗: 两侧 m_45=0.6 ≥ 0.40

			tk := Tick{
				Rem: 200, UpBid: c.upBid, UpAsk: c.upAsk,
				DownBid: c.downBid, DownAsk: c.downAsk, BinPrice: tAnchor,
			}
			if c.spot > 0 {
				tk.BinPrice = c.spot
			}
			got := e.touchSide(tk)
			if got != c.wantSide {
				t.Fatalf("touchSide = %q, 期望 %q", got, c.wantSide)
			}

			obs := e.ProcessTick(tk)
			if !c.valid {
				if obs != nil {
					t.Fatalf("不过整簿门控的 tick 不得产出观测: %+v", obs)
				}
				return // 不过门控时触发 switch 不运行, 等价性无从谈起（只钉诊断侧）
			}
			if c.wantSide == "" {
				if obs != nil {
					t.Fatalf("无触底不得产出观测: %+v", obs)
				}
				return
			}
			if obs == nil {
				t.Fatal("touchSide 报有触底但引擎未产出观测（镜像漂移）")
			}
			if obs.Side != got {
				t.Fatalf("触发侧不一致: 引擎 %q vs touchSide %q", obs.Side, got)
			}
		})
	}
}
