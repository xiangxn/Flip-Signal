package dashboard

import (
	"embed"
	"io/fs"
	"log"
	"net/http"
	"time"
)

//go:embed flip
var flipFiles embed.FS

// flipStatic 是嵌入的 flip 前端资源文件系统（index.html / app.js / style.css）。
// 目录名 = 策略线名（自 static/ 迁移而来, 前端内容一行未改）。
var flipStatic, _ = fs.Sub(flipFiles, "flip")

// ListenAndServe 启动「狗@0.2」的 HTTP dashboard，阻塞直到服务器错误退出。
// 路由:
//
//	/                 单页前端（手机浏览器兼容）
//	/static/*         静态资源
//	/api/state        运行状态
//	/api/observations 触底观测（成功+失败，时间倒序分页 ?page=&limit=）
//	/api/signals      信号列表（含 P&L，时间倒序分页 ?page=&limit=）
//	/api/daily        逐日盈利明细（UTC 日，弹窗表）
//	/api/config       策略配置
func (s *FlipState) ListenAndServe(addr string) {
	mux := s.routes()

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

// routes 装配本族的路由表（抽出来是为了可测: 静态资源走嵌入 FS, 漏文件/写错前缀
// 都只有在真请求一次时才暴露）。
func (s *FlipState) routes() *http.ServeMux {
	mux := http.NewServeMux()

	// 静态资源与单页（FS 根 = 本族的 flip/ 目录, URL 前缀仍是 /static/）
	mux.Handle("/static/", http.StripPrefix("/static/", http.FileServer(http.FS(flipStatic))))
	mux.HandleFunc("/", s.handleIndex)

	// JSON API
	mux.HandleFunc("/api/state", s.handleState)
	mux.HandleFunc("/api/observations", s.handleObservations)
	mux.HandleFunc("/api/signals", s.handleSignals)
	mux.HandleFunc("/api/daily", s.handleDaily)
	mux.HandleFunc("/api/config", s.handleConfig)
	return mux
}

// handleIndex 返回单页前端入口。
func (s *FlipState) handleIndex(w http.ResponseWriter, r *http.Request) {
	serveIndex(flipStatic, w, r)
}
