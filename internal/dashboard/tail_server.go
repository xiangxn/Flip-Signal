package dashboard

import (
	"embed"
	"io/fs"
	"log"
	"net/http"
	"time"
)

//go:embed tail
var tailFiles embed.FS

// tailStatic 是嵌入的 tail 前端资源文件系统（index.html / app.js / style.css）。
var tailStatic, _ = fs.Sub(tailFiles, "tail")

// ListenAndServe 启动「扫尾盘 ⑤」的 HTTP dashboard，阻塞直到服务器错误退出。
// 路由:
//
//	/                 单页前端（手机浏览器兼容）
//	/static/*         静态资源
//	/api/state        运行状态（含本窗热门侧读数与三段链的四个闩锁）
//	/api/snaps        决策行（判定 + 信号, 时间倒序分页 ?page=&limit=）
//	/api/signals      信号行（ok=true, 时间倒序分页）
//	/api/daily        逐日明细（UTC 日, 含段分布与未成交）
//	/api/config       策略配置
//
// ⚠️ 2026-09-24 起 /api/judge、/api/scans、/api/frames 下线（判决机器与两个 legacy
// 行类型一并删除，见 docs/tail_integrated_2026-09-24.md §4）。纸面判决改由离线脚本
// python/v4/23_tail_integrated.py 做。
func (s *TailState) ListenAndServe(addr string) {
	mux := s.routes()

	server := &http.Server{
		Addr:         addr,
		Handler:      withLogging(mux),
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 20 * time.Second,
		IdleTimeout:  30 * time.Second,
	}

	log.Printf("[Tail-Dashboard] 🖥️  监听 %s（手机浏览器可直接访问）", addr)
	if err := server.ListenAndServe(); err != nil {
		log.Printf("[Tail-Dashboard] server error: %v", err)
	}
}

// routes 装配本族的路由表（抽出来是为了可测: 静态资源走嵌入 FS, 漏文件/写错前缀
// 都只有在真请求一次时才暴露——`go:embed tail` 少一个文件就是编译期失败）。
func (s *TailState) routes() *http.ServeMux {
	mux := http.NewServeMux()

	// 静态资源与单页（FS 根 = 本族的 tail/ 目录, URL 前缀仍是 /static/）
	// noStore: embed.FS 无 ModTime/ETag ⇒ 不禁缓存就会拿旧副本渲染（见 common.go）
	mux.Handle("/static/", noStore(http.StripPrefix("/static/", http.FileServer(http.FS(tailStatic)))))
	mux.HandleFunc("/", s.handleIndex)

	// JSON API
	mux.HandleFunc("/api/state", s.handleState)
	mux.HandleFunc("/api/snaps", s.handleSnaps)
	mux.HandleFunc("/api/signals", s.handleSignals)
	mux.HandleFunc("/api/daily", s.handleDaily)
	mux.HandleFunc("/api/config", s.handleConfig)
	return mux
}

// handleIndex 返回单页前端入口。
func (s *TailState) handleIndex(w http.ResponseWriter, r *http.Request) {
	serveIndex(tailStatic, w, r)
}
