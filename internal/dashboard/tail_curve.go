package dashboard

import (
	"fmt"
	"net/http"

	"github.com/necklace/flip-signal/internal/collect"
	"github.com/necklace/flip-signal/internal/tail"
)

// 本文件是 tail 曲线接口（`/api/curve`）的全部实现：**实况**（内存缓冲）与
// **历史重建**（原始采集）两条路。
//
// 为什么要重建：`curveBuf` 只保当前一窗（见 cmd/tail/curve.go 的换窗语义），而本族
// 一窗只下一单 ⇒ 信号表 50 行里最多 1 行能靠内存画出来。历史窗改成从
// `data/events/events_<UTC日>.jsonl` 回读（cmd/tail 兼作采集器，决策 #27）——**纯读**,
// 不新落盘任何曲线文件, 决策 #30「曲线不写文件」那条红线不动（决策 #31）。

// tailCurveResponse 是 tail `/api/curve` 的响应体。
//
// 嵌入 tail.Curve ⇒ 字段平铺（event_start / slug / window_sec / points 四键的**名字
// 与含义全不变**），前端主图那条轮询路径一字不用改。
type tailCurveResponse struct {
	tail.Curve
	// Source 数据来源: live = 内存里正在采的这窗（就是当时屏幕上那条）;
	// events = 由原始采集重建; none = 都没有（Note 里写原因）。
	Source string `json:"source"`
	// Note 给页面看的一句话：none 时是原因, events 时是口径提醒（重建的是原始值,
	// 与实况那条不完全相同）。live 时为空。
	Note string `json:"note,omitempty"`
}

// 曲线来源标记（前端只用来显示一行小字, 不参与任何判定）。
const (
	curveSourceLive   = "live"
	curveSourceEvents = "events"
	curveSourceNone   = "none"
)

// rebuildNote 是重建曲线的口径提醒（前端常显）。
//
// 两个口径差异都会让**重建线比实况线更完整**，不写清楚会被当成「当时的图就是这样」：
// 采集落 `LatestData()` 原值而实况取引擎 tick（现货超龄会被钳 0 ⇒ 断线）；采集行在窗末
// 组装、锚从第一秒就有，而实况要等取锚通道命中（正常 +2s）。
const rebuildNote = "由原始采集重建：现货缺秒处不断线、锚从第一秒就有——比实况曲线更完整，两者不完全相同"

// handleCurve 返回曲线：不带参数 = 当前窗口的实况采样（前端主图每 1s 轮询）;
// 带 `?event_start=N` = **那一窗**的曲线（前端点信号/判定行时弹窗用）。
//
// 换窗语义由服务端决定（见 tail.Curve 类型注释）: 新窗还没有采样时返回的是**上一窗**
// 的序列, 前端据此保留旧曲线不擦。
func (s *TailState) handleCurve(w http.ResponseWriter, r *http.Request) {
	live := s.snapshot.Curve()

	var es int64
	if v := r.URL.Query().Get("event_start"); v != "" {
		_, _ = fmt.Sscanf(v, "%d", &es) // 解析失败 = 0 = 当没给（退回实况曲线）
	}
	if es <= 0 {
		writeJSON(w, tailCurveResponse{Curve: live, Source: curveSourceLive})
		return
	}

	// ① 内存里正好是这一窗 ⇒ 直接给它: 这才是**当时屏幕上**那条（含引擎看到的钳零
	// 口径）。哪怕它一个点都没有也照给——总比去磁盘上找一条还没落盘的曲线强。
	if live.EventStart == es {
		writeJSON(w, tailCurveResponse{Curve: live, Source: curveSourceLive})
		return
	}

	// ② 回读原始采集重建
	c, ok, note := s.eventCurve(es)
	if !ok {
		writeJSON(w, tailCurveResponse{
			Curve:  tail.Curve{EventStart: es, WindowSec: tail.WindowSec},
			Source: curveSourceNone,
			Note:   note,
		})
		return
	}
	writeJSON(w, tailCurveResponse{Curve: c, Source: curveSourceEvents, Note: note})
}

// eventCurve 从 `s.eventsDir` 回读该窗的原始采样并重建成曲线。
// 第二个返回值 = 是否拿到; 第三个 = 给页面看的一句话（成功时是口径提醒, 失败时是原因）。
//
// 三条派生量（Anchor / Extrap / Tie）与实况路径调**同一个** `internal/tail` 实现
// （决策 #31）——否则同一张图会有两种口径。
func (s *TailState) eventCurve(eventStart int64) (tail.Curve, bool, string) {
	if s.eventsDir == "" {
		return tail.Curve{}, false, "原始采集未开启（runtime.events_dir 留空），历史窗无曲线可看"
	}
	ev, ok, err := collect.LoadEventByStart(s.eventsDir, eventStart)
	if err != nil {
		return tail.Curve{}, false, "读取原始采集失败：" + err.Error()
	}
	if !ok {
		return tail.Curve{}, false, "这一窗没有原始采集（当天文件不存在 / 该窗未被采到 / 被落盘红线丢弃）"
	}

	anchor := ev.TwapOpenPrice // 锚 = 窗口起点那一秒的 TWAP 推送（决策 #15）
	pts := make([]tail.CurvePoint, 0, len(ev.Ticks))
	for _, tk := range ev.Ticks {
		p := tail.CurvePoint{
			Ts:     tk.Ts,
			Rem:    tk.Rem,
			Anchor: anchor,
			Twap:   tk.Twap.Price,
			Spot:   tk.Bin.Price,
		}
		p.Extrap = tail.ExtrapPrice(anchor, p.Spot, p.Rem, s.cfg.T150Rem)
		pts = append(pts, p)
	}
	// 结算线要**本点之前**的序列（tail.TieAt 的契约），故逐点推进: 先算 Tie 再往后走,
	// 本点自己绝不进 pts。就地改写安全——TieAt 只读 Ts/Spot 两个字段, 且不追加。
	for i := range pts {
		pts[i].Tie = tail.TieAt(pts[:i], anchor, pts[i])
	}

	return tail.Curve{
		EventStart: eventStart,
		Slug:       ev.Slug,
		// 窗口长度取常量而非实况缓冲的读数: 两者本是同一个常量（cmd/tail 的 windowSec
		// 就是 tail.WindowSec 的别名）——用常量则与「当时缓冲里有没有点」无关。
		WindowSec: tail.WindowSec,
		Points:    pts,
	}, true, rebuildNote
}
