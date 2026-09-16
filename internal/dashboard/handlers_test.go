package dashboard

// writePage 分页测试: 时间倒序切片 / total / 越界钳制 / page/limit 参数解析。

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/necklace/flip-signal/internal/flip"
)

// recs 构造 total 条 Ts 从 1000 递增的记录（时间正序，模拟 recorder 返回序）。
func recs(total int) []*flip.Record {
	out := make([]*flip.Record, 0, total)
	for i := 0; i < total; i++ {
		out = append(out, &flip.Record{Observation: flip.Observation{Ts: int64(1000 + i)}})
	}
	return out
}

// resp 发起带 query 的请求并解析 listResp。
func resp(t *testing.T, records []*flip.Record, defSize int, query string) listResp {
	t.Helper()
	s := &State{}
	req := httptest.NewRequest(http.MethodGet, "/api/x"+query, nil)
	w := httptest.NewRecorder()
	s.writePage(w, req, records, defSize)
	var r listResp
	if err := json.Unmarshal(w.Body.Bytes(), &r); err != nil {
		t.Fatalf("解析响应失败: %v", err)
	}
	return r
}

func tsItems(items []recordResponse) []int64 {
	out := make([]int64, 0, len(items))
	for _, it := range items {
		out = append(out, it.Ts)
	}
	return out
}

func eqTs(t *testing.T, got []int64, want ...int64) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("items ts=%v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("items ts=%v, want %v", got, want)
		}
	}
}

func TestWritePage(t *testing.T) {
	cases := []struct {
		name    string
		records int
		defSize int
		query   string
		total   int
		page    int
		size    int
		wantTs  []int64 // 期望 items 的 ts（时间倒序）
	}{
		{name: "空数据", records: 0, defSize: 50, query: "", total: 0, page: 1, size: 50},
		{name: "不足一页", records: 5, defSize: 50, query: "", total: 5, page: 1, size: 50, wantTs: []int64{1004, 1003, 1002, 1001, 1000}},
		{name: "首页", records: 5, defSize: 50, query: "?page=1&limit=2", total: 5, page: 1, size: 2, wantTs: []int64{1004, 1003}},
		{name: "中间页", records: 5, defSize: 50, query: "?page=2&limit=2", total: 5, page: 2, size: 2, wantTs: []int64{1002, 1001}},
		{name: "末页余数", records: 5, defSize: 50, query: "?page=3&limit=2", total: 5, page: 3, size: 2, wantTs: []int64{1000}},
		{name: "越界钳制到末页", records: 5, defSize: 50, query: "?page=99&limit=2", total: 5, page: 3, size: 2, wantTs: []int64{1000}},
		{name: "page 非法回落 1", records: 3, defSize: 50, query: "?page=0&limit=1", total: 3, page: 1, size: 1, wantTs: []int64{1002}},
		{name: "page 非法回落默认", records: 3, defSize: 50, query: "?page=abc&limit=1", total: 3, page: 1, size: 1, wantTs: []int64{1002}},
		{name: "limit 超上限钳 1000", records: 3, defSize: 50, query: "?limit=99999", total: 3, page: 1, size: 1000, wantTs: []int64{1002, 1001, 1000}},
		{name: "信号默认页大 200", records: 3, defSize: 200, query: "", total: 3, page: 1, size: 200, wantTs: []int64{1002, 1001, 1000}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r := resp(t, recs(c.records), c.defSize, c.query)
			if r.Total != c.total || r.Page != c.page || r.Size != c.size {
				t.Fatalf("信封 total/page/size = %d/%d/%d, want %d/%d/%d",
					r.Total, r.Page, r.Size, c.total, c.page, c.size)
			}
			eqTs(t, tsItems(r.Items), c.wantTs...)
		})
	}
}

// ── /api/state: 三源阈值下发 + 本窗 tick 健康度（2026-09-16）──

// fakeSnap 实现 flip.Snapshotter（固定快照）。
type fakeSnap struct{ s flip.LiveSnapshot }

func (f fakeSnap) Snapshot() flip.LiveSnapshot { return f.s }

// TestStateLimitsAndWindowStats 前端标红/诊断行的数据源: limits 原样下发、
// window_stats 随快照透出（含丢信号明细）。
func TestStateLimitsAndWindowStats(t *testing.T) {
	rec, err := flip.NewRecorder(t.TempDir())
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	defer rec.Close()

	stats := flip.WindowStats{
		Ticks: 300, TicksValid: 298, BookStale: 1, BookMissing: 1,
		LostTriggers: []flip.LostTrigger{{
			Ts: 1, Side: flip.SideYes, Rem: 196, Ask: 0.19,
			BookLatMs: 412, Reason: flip.LostReasonStaleBook,
		}},
	}
	limits := SourceLimits{BookLatMs: 300, SpotAgeMs: 2000, TwapAgeMs: 10_000}
	s := NewState(rec, fakeSnap{flip.LiveSnapshot{
		EngineState: "Watching", BookLatMs: 412, Stats: &stats,
	}}, flip.DefaultConfig(), "paper", limits)

	w := httptest.NewRecorder()
	s.handleState(w, httptest.NewRequest(http.MethodGet, "/api/state", nil))
	var got stateResponse
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatalf("解析 /api/state: %v", err)
	}
	if got.Limits != limits {
		t.Fatalf("limits = %+v, 期望 %+v", got.Limits, limits)
	}
	if got.WindowStats == nil {
		t.Fatal("window_stats 缺失（快照有 Stats 时应透出）")
	}
	if got.WindowStats.Ticks != 300 || got.WindowStats.TicksValid != 298 ||
		got.WindowStats.BookStale != 1 || got.WindowStats.BookMissing != 1 {
		t.Fatalf("本窗计数错: %+v", got.WindowStats)
	}
	if len(got.WindowStats.LostTriggers) != 1 ||
		got.WindowStats.LostTriggers[0].BookLatMs != 412 {
		t.Fatalf("丢信号明细未透出: %+v", got.WindowStats.LostTriggers)
	}

	// 窗口间（快照无 Stats）: 字段省略，前端隐藏健康度行
	s2 := NewState(rec, fakeSnap{flip.LiveSnapshot{}}, flip.DefaultConfig(), "paper", limits)
	w2 := httptest.NewRecorder()
	s2.handleState(w2, httptest.NewRequest(http.MethodGet, "/api/state", nil))
	if body := w2.Body.String(); !strings.Contains(body, "\"limits\"") ||
		strings.Contains(body, "\"window_stats\"") {
		t.Fatalf("窗口间应只下发 limits 而不含 window_stats: %s", body)
	}
}
