// Package dashboard 提供两条策略线的 Web 监控界面:
//
//	flip/   「狗@0.2」触底策略（cmd/flip）
//	tail/   「扫尾盘 ⑤」尾盘策略（cmd/tail）
//
// 两族**各自一个 listener、各自一套静态资源**（URL 前缀都是 /static/，但 FS 根
// 不同），互不影响——可以只开其一，也可以两个进程同时开。各自的 API:
//
//	flip:  /api/state /api/observations /api/signals /api/daily /api/config
//	tail:  /api/state /api/snaps /api/signals /api/daily /api/config
//
// 运行时快照类型（flip.LiveSnapshot / tail.LiveSnapshot）定义在各自的引擎包里
// （引擎域数据）——本包只做 HTTP 展示，不自行定义状态类型。
//
// 本文件是**两族共用**的那部分: 分页信封与切片、查询参数解析、JSON 响应、请求
// 日志、单页入口、逐日聚合。策略专属的东西一律不进这里（flip_*.go / tail_*.go）。
package dashboard

import (
	"encoding/json"
	"fmt"
	"io/fs"
	"log"
	"net/http"
	"sort"
	"strings"
	"time"
)

// SourceLimits 是三个数据源新鲜度阈值（/api/state 的 limits 下发，前端按此标红）。
// 口径与引擎/采样层实际生效值同源（配置文件的 max_book_lat_ms / feed.* 三键）——
// 前端不得硬编码阈值，否则调参后页面颜色语义会与实际判定脱节。
//
// 两族共用同一 struct（字段语义相同: 盘口延迟闸 / spot 新鲜度 / TWAP 新鲜度）,
// 只是取值来源各自: flip 取 cfg.Flip.MaxBookLatMs + feed 两键, tail 取
// cfg.Tail.MaxBookLatMs + 同两个 feed 键。
type SourceLimits struct {
	BookLatMs int64 `json:"book_lat_ms"` // 盘口延迟闸（引擎判无效）
	SpotAgeMs int64 `json:"spot_age_ms"` // Binance spot 新鲜度闸（采样层判缺失）
	TwapAgeMs int64 `json:"twap_age_ms"` // TWAP 新鲜度闸（anchor/close 守卫）
}

// listResp 是列表 API 的分页信封（items 为时间倒序的当前页切片; total/page/size
// 供前端分页条渲染）。U 是各族的响应元素类型（flip: recordResponse / tail:
// tailRecordResponse）——泛型参数化让两族共用同一套切片逻辑而不共享字段 schema。
type listResp[U any] struct {
	Items []U `json:"items"`
	Total int `json:"total"`
	Page  int `json:"page"` // 1-based 当前页（越界时服务端钳制到末页）
	Size  int `json:"size"` // 本页条数（?limit=，≤1000）
}

// writePage 输出时间倒序的分页列表: 先按 tsOf 降序排，再切 ?page=&limit= 窗口。
// page 越界时钳制到末页；total=0 时恒为第 1 页 + 空 items。
//
// ⚠️ all 是调用方给的副本（recorder 的读口都返回副本）——本函数**原地排序**它,
// 不得传入内部切片。
func writePage[T, U any](w http.ResponseWriter, r *http.Request, all []T,
	tsOf func(T) int64, mapFn func(T) U, defSize int) {
	sort.Slice(all, func(i, j int) bool { return tsOf(all[i]) > tsOf(all[j]) })
	total := len(all)
	size := queryLimit(r, defSize)
	lastPage := 1
	if total > 0 {
		lastPage = (total + size - 1) / size
	}
	page := queryPage(r)
	if page > lastPage {
		page = lastPage
	}
	start := (page - 1) * size
	end := start + size
	if end > total {
		end = total
	}
	items := make([]U, 0, end-start)
	for _, e := range all[start:end] {
		items = append(items, mapFn(e))
	}
	writeJSON(w, listResp[U]{Items: items, Total: total, Page: page, Size: size})
}

// queryPage 解析 ?page= 参数（1-based，默认 1，最小 1）。
func queryPage(r *http.Request) int {
	v := r.URL.Query().Get("page")
	if v == "" {
		return 1
	}
	var n int
	if _, err := fmt.Sscanf(v, "%d", &n); err != nil {
		return 1
	}
	if n < 1 {
		return 1
	}
	return n
}

// queryLimit 解析 ?limit= 参数（1..1000，默认 def）。
func queryLimit(r *http.Request, def int) int {
	v := r.URL.Query().Get("limit")
	if v == "" {
		return def
	}
	var n int
	if _, err := fmt.Sscanf(v, "%d", &n); err != nil {
		return def
	}
	if n < 1 {
		return 1
	}
	if n > 1000 {
		return 1000
	}
	return n
}

// writeJSON 统一 JSON 响应。
func writeJSON(w http.ResponseWriter, v interface{}) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("[Dashboard] 编码响应失败: %v", err)
	}
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

// serveIndex 返回单页前端入口（fsys 是各族的嵌入静态目录）。
func serveIndex(fsys fs.FS, w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	index, err := fs.ReadFile(fsys, "index.html")
	if err != nil {
		http.Error(w, "index.html 嵌入缺失", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Write(index)
}

// noStore 给静态资源响应加 `Cache-Control: no-store`（包在各自的 /static/ 路由外）。
//
// 为什么必须显式禁缓存（2026-09-24 用户报「被闸行结果列还显示待结算」的根因）:
// 前端三件套走 `go:embed`，而 embed.FS 的 ModTime 恒为零值 ⇒ 响应里**既没有
// Last-Modified 也没有 ETag**，浏览器拿不到任何再验证凭据，只能按启发式决定要不要
// 复用标签页里那份旧副本。服务端一切正常（/static/app.js 与本地逐字节相同、
// /api/signals 里 gate_reason/exec_status 齐备），页面却停在旧渲染——诊断为
// 浏览器侧的陈旧 app.js。
//
// 页面必须永远等于正在运行的二进制（前端与引擎同一次构建、同一次部署），
// 故静态资源与单页一律 no-store：三件套合计 ~35KB，不值得为省这点流量换
// 「部署了新代码、页面还是旧样子」的排查成本。
func noStore(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		next.ServeHTTP(w, r)
	})
}

// remainingSec 计算当前窗口剩余秒（未到窗口起点或已结束为 0）。
//
// ⚠️ 口径: 窗口长 300s 是**两族共同的**市场结构（btc-updown-5m），但本函数只认
// eventStart + 300——两族的 snapshot 都**不下发 rem**（5s 轮询快照里的 rem 会明显
// 滞后, 由 handler 用服务器当前时刻现算才是准的, 见各自 LiveSnapshot 的类型注释）。
func remainingSec(eventStart, nowSec int64) int {
	if eventStart == 0 {
		return 0
	}
	rem := eventStart + 300 - nowSec
	if rem < 0 {
		return 0
	}
	return int(rem)
}

// ── 逐日聚合（两族共用骨架）──

// dayAgg 是按 UTC 日聚合的中立小结构（/api/daily 的一行）。
//
// 「中立」的意思: 它不认识任何一族的记录类型——两个族的 handler 各自把
// []*Record 映射进来（各 ~12 行, 见 flip_handlers.go 的 collectDaily 与
// tail_handlers.go 的 collectDaily）, 然后共用 finalizeDaily 定稿。
// 之所以不定义接口让两族记录直接喂进来: 那需要给两族的 Record 造一组 getter,
// 而这里要的只是 7 个数字。
type dayAgg struct {
	Date    string  `json:"date"` // YYYY-MM-DD（UTC）；total 行为空
	Obs     int     `json:"obs"`  // 行数（flip = 触底观测含失败; tail = 帧行 + 快照行）
	Signals int     `json:"signals"`
	Pending int     `json:"pending"` // 未结算信号
	Won     int     `json:"won"`
	Lost    int     `json:"lost"`
	WinRate float64 `json:"win_rate"` // 已结算口径（won/(won+lost)；无结算 = 0）
	PnL     float64 `json:"pnl"`
}

// finalizeDaily 把按日累积的 map 定稿成时间正序切片（补算胜率）。
// 胜率与 P&L 只统计已结算信号（与 /api/state 统计口径一致）。
func finalizeDaily(byDay map[string]*dayAgg) []dayAgg {
	out := make([]dayAgg, 0, len(byDay))
	for _, d := range byDay {
		if d.Won+d.Lost > 0 {
			d.WinRate = float64(d.Won) / float64(d.Won+d.Lost)
		}
		out = append(out, *d)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Date < out[j].Date })
	return out
}

// sumDaily 把逐日行累加成合计行（Obs/Signals/Pending/Won/Lost/PnL; 胜率另算）。
func sumDaily(days []dayAgg) dayAgg {
	var total dayAgg
	for _, d := range days {
		total.Obs += d.Obs
		total.Signals += d.Signals
		total.Pending += d.Pending
		total.Won += d.Won
		total.Lost += d.Lost
		total.PnL += d.PnL
	}
	if total.Won+total.Lost > 0 {
		total.WinRate = float64(total.Won) / float64(total.Won+total.Lost)
	}
	return total
}
