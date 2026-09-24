package dashboard

// 路由表测试（两族）: 单页入口、静态资源（嵌入 FS）、JSON API 的接线。
//
// 为什么要真请求一次: 静态资源走 `go:embed` + `fs.Sub`, 前缀写错（/static/ 与 FS 根
// 错配）或漏文件都不会在编译期暴露——`static/` 改成 `flip/` 那次重构正是这里最容易出错。

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/necklace/flip-signal/internal/flip"
	"github.com/necklace/flip-signal/internal/tail"
)

// serve 对一个已装配的路由表发一次请求，返回响应。
func serve(t *testing.T, mux *http.ServeMux, path string) *httptest.ResponseRecorder {
	t.Helper()
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest(http.MethodGet, path, nil))
	return w
}

// TestTailRoutes 逐条打 tail 的路由（含静态资源与单页——本族的前端三件套是新增的）。
func TestTailRoutes(t *testing.T) {
	rec, err := tail.NewRecorder(t.TempDir())
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	defer rec.Close()
	s := NewTailState(rec, fakeTailSnap{tail.LiveSnapshot{
		Mode: "paper", EventStart: time.Now().Unix(), EngineState: "Watching",
	}}, tail.DefaultConfig(), "paper", SourceLimits{BookLatMs: 300})

	mux := s.routes()

	// 单页入口: / 出 HTML; 其它路径 404（不吞掉未知路由）
	if w := serve(t, mux, "/"); w.Code != http.StatusOK ||
		!strings.Contains(w.Header().Get("Content-Type"), "text/html") ||
		!strings.Contains(w.Body.String(), "扫尾盘") {
		t.Fatalf("GET / = %d %q（body 前 120 字符: %.120s）",
			w.Code, w.Header().Get("Content-Type"), w.Body.String())
	}
	if w := serve(t, mux, "/nope"); w.Code != http.StatusNotFound {
		t.Fatalf("GET /nope = %d, 期望 404", w.Code)
	}

	// 静态资源: URL 前缀 /static/ 必须正确映射到本族 FS 根（前缀要 StripPrefix 掉）
	for _, f := range []string{"app.js", "style.css"} {
		w := serve(t, mux, "/static/"+f)
		if w.Code != http.StatusOK || w.Body.Len() == 0 {
			t.Fatalf("GET /static/%s = %d（%d 字节）", f, w.Code, w.Body.Len())
		}
	}
	if w := serve(t, mux, "/static/nope.js"); w.Code != http.StatusNotFound {
		t.Fatalf("GET /static/nope.js = %d, 期望 404", w.Code)
	}

	// JSON API: 每口都应 200 且是 JSON（内容由各自的 handler 测试负责）
	for _, path := range []string{
		"/api/state", "/api/snaps", "/api/signals", "/api/daily", "/api/config",
	} {
		w := serve(t, mux, path)
		if w.Code != http.StatusOK {
			t.Fatalf("GET %s = %d", path, w.Code)
		}
		if ct := w.Header().Get("Content-Type"); !strings.Contains(ct, "application/json") {
			t.Fatalf("GET %s Content-Type = %q", path, ct)
		}
		var v interface{}
		if err := json.Unmarshal(w.Body.Bytes(), &v); err != nil {
			t.Fatalf("GET %s 不是合法 JSON: %v", path, err)
		}
	}
}

// TestNoStoreHeaders 两族的前端三件套与 JSON API 都必须显式禁缓存。
//
// 起因（用户反馈的线上现象, 2026-09-24）: 改了「结果列显示被风控闸拦下的行」并部署后,
// 页面上那一列还是旧渲染（仍显示「待结算」）。排查结论是**浏览器侧**：服务端一切正常
// （/static/app.js 与本地构建逐字节相同、/api/signals 里 gate_reason/exec_status 齐备）,
// 是标签页里那份旧 app.js 没被换掉。根因: 静态资源走 `go:embed`，embed.FS 的 ModTime
// 恒为零值 ⇒ 响应不带 Last-Modified/ETag ⇒ 浏览器没有再验证凭据, 只能按启发式复用旧副本。
// 本测试钉住「页面永远等于正在运行的二进制」: 三处出口（单页 / 静态资源 / JSON API）
// 都必须 `Cache-Control: no-store`。
func TestNoStoreHeaders(t *testing.T) {
	flipRec, err := flip.NewRecorder(t.TempDir())
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	defer flipRec.Close()
	tailRec, err := tail.NewRecorder(t.TempDir())
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	defer tailRec.Close()

	fs := NewFlipState(flipRec, fakeSnap{flip.LiveSnapshot{}}, flip.DefaultConfig(), "paper", SourceLimits{})
	ts := NewTailState(tailRec, fakeTailSnap{tail.LiveSnapshot{}}, tail.DefaultConfig(), "paper", SourceLimits{})

	for _, tc := range []struct {
		family string
		mux    *http.ServeMux
		api    string
	}{
		{"flip", fs.routes(), "/api/signals"},
		{"tail", ts.routes(), "/api/snaps"},
	} {
		for _, path := range []string{"/", "/static/app.js", "/static/style.css", tc.api} {
			w := serve(t, tc.mux, path)
			if w.Code != http.StatusOK {
				t.Fatalf("%s GET %s = %d", tc.family, path, w.Code)
			}
			if cc := w.Header().Get("Cache-Control"); cc != "no-store" {
				t.Fatalf("%s GET %s Cache-Control = %q, 期望 no-store（陈旧前端会让页面与二进制脱节）",
					tc.family, path, cc)
			}
		}
	}
}

// TestFlipRoutes flip 侧的路由回归（重构 static/ → flip/ 后必须一模一样）。
func TestFlipRoutes(t *testing.T) {
	rec, err := flip.NewRecorder(t.TempDir())
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	defer rec.Close()
	s := NewFlipState(rec, fakeSnap{flip.LiveSnapshot{
		Mode: "paper", EventStart: time.Now().Unix(), EngineState: "Watching",
	}}, flip.DefaultConfig(), "paper", SourceLimits{BookLatMs: 300})

	mux := s.routes()

	if w := serve(t, mux, "/"); w.Code != http.StatusOK ||
		!strings.Contains(w.Body.String(), "Dog@0.2") { // = flip 前端的 <h1>
		t.Fatalf("GET / = %d（body 前 120 字符: %.120s）", w.Code, w.Body.String())
	}
	for _, f := range []string{"app.js", "style.css"} {
		if w := serve(t, mux, "/static/"+f); w.Code != http.StatusOK || w.Body.Len() == 0 {
			t.Fatalf("GET /static/%s = %d（%d 字节）", f, w.Code, w.Body.Len())
		}
	}
	for _, path := range []string{
		"/api/state", "/api/observations", "/api/signals", "/api/daily", "/api/config",
	} {
		if w := serve(t, mux, path); w.Code != http.StatusOK {
			t.Fatalf("GET %s = %d", path, w.Code)
		}
	}
}
