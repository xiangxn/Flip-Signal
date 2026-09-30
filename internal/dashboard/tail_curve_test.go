package dashboard

import (
	"strconv"
	"testing"
	"time"

	"github.com/necklace/flip-signal/internal/collect"
	"github.com/necklace/flip-signal/internal/tail"
)

// 曲线接口的两条路（决策 #31）: 实况（内存缓冲）与历史重建（原始采集回读）。
// 重建的数学全部复用 internal/tail 的三个纯函数, 故这里只钉**接线与口径**:
// 哪条路赢、点怎么装配、缺数据时说什么。

// newCurveState 建一个可指定 eventsDir 的 tail 载体（其余同 newTailState）。
func newCurveState(t *testing.T, curve tail.Curve, eventsDir string) *TailState {
	t.Helper()
	rec, err := tail.NewRecorder(t.TempDir())
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	t.Cleanup(func() { rec.Close() })
	return NewTailState(rec, fakeTailSnap{curve: curve}, tail.DefaultConfig(), "paper", eventsDir, SourceLimits{})
}

func getCurve(t *testing.T, s *TailState, path string) tailCurveResponse {
	t.Helper()
	var got tailCurveResponse
	getJSON(t, s.handleCurve, path, &got)
	return got
}

// TestTailCurveNoEventStart 不带参数 = 实况曲线（主图那条 1s 轮询路径, 行为与加
// event_start 之前一字不差）。
func TestTailCurveNoEventStart(t *testing.T) {
	live := tail.Curve{
		EventStart: 1780000000, Slug: "btc-updown-5m-1780000000", WindowSec: 300,
		Points: []tail.CurvePoint{{Ts: 1780000000 * 1000, Rem: 300}},
	}
	got := getCurve(t, newCurveState(t, live, ""), "/api/curve")
	if got.Source != curveSourceLive || got.Note != "" {
		t.Fatalf("source=%q note=%q, 期望 live/空", got.Source, got.Note)
	}
	if got.EventStart != live.EventStart || len(got.Points) != 1 {
		t.Fatalf("曲线 = %+v, 期望原样透出", got.Curve)
	}
}

// TestTailCurveLiveWindowWins 请求的那一窗**正是内存里这一窗**时给实况曲线: 它才是
// 当时屏幕上那条（含引擎的钳零口径）, 也比磁盘重建更准。
func TestTailCurveLiveWindowWins(t *testing.T) {
	const start = 1780000000
	live := tail.Curve{
		EventStart: start, WindowSec: 300,
		Points: []tail.CurvePoint{{Ts: start * 1000, Rem: 299, Spot: 100123.4}},
	}
	// eventsDir 指向一个**空目录**也不会被用到: 实况命中就不该去读盘
	got := getCurve(t, newCurveState(t, live, t.TempDir()), "/api/curve?event_start=1780000000")
	if got.Source != curveSourceLive || len(got.Points) != 1 || got.Points[0].Spot != 100123.4 {
		t.Fatalf("= %+v（source=%s）, 期望走实况", got.Curve, got.Source)
	}
}

// TestTailCurveRebuildFromEvents 历史窗从原始采集重建: 点数/横轴/锚, 以及两条派生线
// 在各自的边界点上取到手算值（rem=150 的外推、rem=60 的结算线与外推收线、rem=59 的
// 结算线——这里 span=1, 手算完全可校验）。
func TestTailCurveRebuildFromEvents(t *testing.T) {
	const anchor = 100000.0
	start := time.Date(2026, 9, 30, 10, 0, 0, 0, time.UTC).Unix()
	dir := t.TempDir()

	ev := &collect.Event{
		ConditionID: "0xcurve", Slug: "btc-updown-5m-curve", StartTime: start,
		TwapOpenPrice: anchor, TwapClosePrice: anchor + 1, CloseSource: collect.SourcePush,
		AnchorSource: collect.SourcePush, Outcome: 0, BinanceOpen: anchor,
	}
	// 300 个 tick（rem 299→0）, 现货逐秒 +1 美元 —— 顺带把「单行 > 64KB」那条路也走一遍
	for i := 0; i < 300; i++ {
		ev.Ticks = append(ev.Ticks, collect.HFTick{
			Ts:   (start + int64(i)) * 1000,
			Rem:  299 - i,
			Bin:  collect.BinTick{Price: anchor + float64(i)},
			Twap: collect.TwapTick{Price: anchor},
		})
	}
	if written, err := collect.WriteUniqueEvent(dir, ev); err != nil || !written {
		t.Fatalf("写事件行: written=%v err=%v", written, err)
	}

	got := getCurve(t, newCurveState(t, tail.Curve{}, dir), "/api/curve?event_start="+strconv.FormatInt(start, 10))
	if got.Source != curveSourceEvents || got.Note == "" {
		t.Fatalf("source=%q note=%q, 期望 events + 口径提醒", got.Source, got.Note)
	}
	if got.EventStart != start || got.Slug != "btc-updown-5m-curve" || got.WindowSec != 300 {
		t.Fatalf("曲线头 = %+v", got.Curve)
	}
	if len(got.Points) != 300 {
		t.Fatalf("点数 = %d, 期望 300", len(got.Points))
	}
	p0 := got.Points[0]
	if p0.Ts != start*1000 || p0.Rem != 299 || p0.Anchor != anchor || p0.Twap != anchor || p0.Spot != anchor {
		t.Fatalf("首点 = %+v", p0)
	}
	for i, p := range got.Points {
		if p.Anchor != anchor {
			t.Fatalf("第 %d 点锚 = %v: 锚是窗口级常量, 每点都该是 twap_open_price", i, p.Anchor)
		}
	}

	// 派生线的定义域: 外推只在 rem ∈ [60,150]、结算线只在 rem ≤ 60
	if p := got.Points[148]; p.Rem != 151 || p.Extrap != 0 {
		t.Fatalf("rem=151 应为 0（第一个判定点之前不画）: %+v", p)
	}
	// rem=150: spot=100149 ⇒ 100149 + 0.75·(100000−100149) = 100037.25
	if p := got.Points[149]; p.Rem != 150 || p.Extrap != 100037.25 {
		t.Fatalf("rem=150 外推 = %+v, 期望 100037.25", p)
	}
	if p := got.Points[200]; p.Rem != 99 || p.Tie != 0 {
		t.Fatalf("rem=99 结算线应为 0（还没进结算窗）: %+v", p)
	}
	// rem=60: 外推收线到现货、结算线起线于锚
	if p := got.Points[239]; p.Rem != 60 || p.Extrap != anchor+239 || p.Tie != anchor {
		t.Fatalf("rem=60 = %+v, 期望 Extrap=Spot=100239 / Tie=anchor=100000", p)
	}
	// rem=59: 已定局 1 秒（现货 100240）⇒ P = (60·anchor − 100240)/59
	if p := got.Points[240]; p.Rem != 59 {
		t.Fatalf("第 240 点 rem = %d, 期望 59", p.Rem)
	} else if want := (60*anchor - (anchor + 240)) / 59; p.Tie != want {
		t.Fatalf("rem=59 结算线 = %v, 期望 %v", p.Tie, want)
	}
}

// TestTailCurveNoEvents 拿不到原始采集时**不是错误**: HTTP 照 200, source=none +
// 一句能显示给用户的原因（页面直接把 Note 打出来）。
func TestTailCurveNoEvents(t *testing.T) {
	cases := []struct {
		name      string
		eventsDir func(t *testing.T) string
	}{
		{"原始采集未开启（events_dir 留空串）", func(t *testing.T) string { return "" }},
		{"目录里没有那天的文件", func(t *testing.T) string { return t.TempDir() }},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := getCurve(t, newCurveState(t, tail.Curve{}, c.eventsDir(t)), "/api/curve?event_start=1780000000")
			if got.Source != curveSourceNone || got.Note == "" {
				t.Fatalf("source=%q note=%q, 期望 none + 原因", got.Source, got.Note)
			}
			if len(got.Points) != 0 || got.EventStart != 1780000000 || got.WindowSec != tail.WindowSec {
				t.Fatalf("空曲线 = %+v（EventStart/WindowSec 仍要填, 前端拿它写标题）", got.Curve)
			}
		})
	}
}

// TestTailSignalsCarryEventStart 信号/判定行必须带上 event_start: 前端点行时**只能**
// 拿它定位原始采集（拿 date 反推会因跨午夜差一天）。
func TestTailSignalsCarryEventStart(t *testing.T) {
	s, rec, _ := newTailState(t, tail.LiveSnapshot{}, SourceLimits{})
	const start = 1780000000
	if _, err := rec.RecordObservation("0xes", "btc-updown-5m", start, tailSignal(tail.StageT150, start*1000+150000), 2); err != nil {
		t.Fatalf("信号行: %v", err)
	}

	var sigs listResp[tailRecordResponse]
	getJSON(t, s.handleSignals, "/api/signals", &sigs)
	if len(sigs.Items) != 1 || sigs.Items[0].EventStart != start {
		t.Fatalf("信号行 event_start = %+v, 期望 %d", sigs.Items, start)
	}
}
