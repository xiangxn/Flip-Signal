package dashboard

import (
	"embed"
	"io/fs"
	"log"
	"net/http"
	"strings"
	"time"
)

//go:embed static
var staticFiles embed.FS

// static 是嵌入的静态资源文件系统（index.html / app.js / style.css）。
var static, _ = fs.Sub(staticFiles, "static")

// ListenAndServe 启动 HTTP dashboard，阻塞直到服务器错误退出。
// 路由:
//
//	/                 单页前端（手机浏览器兼容）
//	/static/*         静态资源
//	/api/state        运行状态
//	/api/observations 触底观测（成功+失败，时间倒序分页 ?page=&limit=）
//	/api/signals      信号列表（含 P//	/api/signals      信号列表L，时间倒序分页 ?page=&limit=）
//	/api/daily        逐日盈利明细（UTC 日，弹窗表）
//	/api/config       策略配置
func (s *State) ListenAndServe(addr string) {
	mux := http.NewServeMux()

	// 静态资源与单页
	mux.Handle("/static/", http.StripPrefix("/static/", http.FileServer(http.FS(static))))
	mux.HandleFunc("/", s.handleIndex)

	// JSON API
	mux.HandleFunc("/api/state", s.handleState)
	mux.HandleFunc("/api/observations", s.handleObservations)
	mux.HandleFunc("/api/signals", s.handleSignals)
	mux.HandleFunc("/api/daily", s.handleDaily)
	mux.HandleFunc("/api/config", s.handleConfig)

	server := &http.Server{
		Addr:         addr,
		Handler:      withLogging(mux),
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 10 * time.Second,
		IdleTimeout:  30 * time.Second,
	}

	log.Printf("[Dashboard] 🖥️  监听 %s（手机浏览器可直接访问）", addr)
	if err := server.ListenAndServe(); err != nil {
		log.Printf("[Dashboard] server error: %v", err)
	}
}

// handleIndex 返回单页前端入口。
func (s *State) handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	index, err := fs.ReadFile(static, "index.html")
	if err != nil {
		http.Error(w, "index.html 嵌入缺失", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write(index)
}

// withLogging 包装请求日志（/api/state 轮询 5s 一次，不打日志防刷屏）。
func withLogging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		// 高频轮询与静态资源不打日志（防刷屏）；favicon 404 也无须刷日志
		p := r.URL.Path
		if p != "/api/state" && !strings.HasPrefix(p, "/static/") && p != "/favicon.ico" {
			log.Printf("[Dashboard] %s %s (%s)", r.Method, p, time.Since(start).Round(time.Microsecond))
		}
	})
}
