package dashboard

// writePage 分页测试: 时间倒序切片 / total / 越界钳制 / page/limit 参数解析。

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
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
