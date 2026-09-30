// 本窗动态曲线（anchor / twap / spot 三线同轴 + 两条派生阈值线: rem≤60 的结算线与
// rem∈[60,150] 的速度外推线）: 主循环逐 tick 采样 → Dashboard 只读。
//
// 为什么采样放在**服务端主循环**而不是前端攒点: 主循环本来就是 1s 一拍, 曲线要的是
// 「每窗从边界起、逐秒」的完整轨迹; 前端按 /api/state 的 5s 轮询攒点会漏掉 4/5 的
// 采样（窗口 300s 只剩 60 个点）。结算线同理必须在这里算: 它要的是**逐秒现货序列**
// (只为了那 (60−rem) 秒的均值), 前端手上只有自己攒的点, 而且攒不齐。
//
// 边界（与引擎判定路径完全无交集）: 采样只写本文件的缓冲, 不参与任何判定、不落盘、
// 不影响执行与结算——缓冲丢了只影响页面上那条线。
//
// ⚠️ 两条派生线的**公式**不在这里, 在 `internal/tail`（决策 #31）: 历史窗曲线由
// Dashboard 从 events 重建, 两边必须共用一份公式。本文件只管**实时缓冲**。
package main

import (
	"sync"

	"github.com/necklace/flip-signal/internal/flip"
	"github.com/necklace/flip-signal/internal/tail"
)

// curveMaxPoints 是单窗曲线缓冲的容量上限（窗口秒数 + ticker 抖动余量）。
// 到顶后**丢弃新点**而不是从头挤掉旧点: 曲线要看的是「从边界到闭市」的整条轨迹,
// 挤掉起点等于把锚与开局的位移一起抹掉。
const curveMaxPoints = windowSec + 30

// curveBuf 是当前窗口的曲线缓冲（换窗即重置, 见 add 的 eventStart 判据）。
//
// 用独立的互斥量而不是 runtimeState 的窗口锁: 主循环每 tick 写、Dashboard 每 1s 读,
// 与窗口换装（setWindow/clearWindow）的读写面完全不同——混用会让 Snapshot 那条
// 「全量观测遍历不阻塞窗口换装」的注释失效。
type curveBuf struct {
	mu         sync.Mutex
	eventStart int64 // 缓冲所属窗口起点（unix 秒; 0 = 还没采过任何点）
	slug       string
	pts        []tail.CurvePoint
}

// add 追加一条采样。eventStart 与缓冲当前所属窗口不同 ⇒ **整条曲线换装**（旧窗的点
// 全部丢弃）——这就是「窗口完成时保留上一窗曲线, 直到新窗新数据到来」的服务端半边:
// 新窗第一个采样到达之前, snapshot 返回的仍是上一窗。
func (b *curveBuf) add(eventStart int64, slug string, p tail.CurvePoint) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.eventStart != eventStart {
		b.eventStart, b.slug, b.pts = eventStart, slug, nil
	}
	if len(b.pts) >= curveMaxPoints {
		return
	}
	b.pts = append(b.pts, p)
}

// snapshot 返回当前缓冲的副本（切片复制——dashboard 序列化期间主循环还在追加）。
func (b *curveBuf) snapshot() tail.Curve {
	b.mu.Lock()
	defer b.mu.Unlock()
	pts := make([]tail.CurvePoint, len(b.pts)) // 非 nil: 空缓冲也序列化成 []
	copy(pts, b.pts)
	return tail.Curve{EventStart: b.eventStart, Slug: b.slug, Points: pts}
}

// sampleCurve 把本 tick 的读数追加进曲线缓冲（主循环每 tick 调用一次）。
//
// remMax = 本族第一个判定点（`tail.t150_rem`）: 外推临界价**从这里起画**（见
// tail.ExtrapPrice）。
//
// 引擎为 nil（跳窗路径/窗口间）⇒ 不采样: 缓冲保持不动, 页面上继续显示上一窗的曲线;
// 下一窗的第一个采样到达时由 add 整体换装。
func (rt *runtimeState) sampleCurve(tick flip.Tick, remMax int) {
	rt.mu.RLock()
	eng, start, slug := rt.Engine, rt.EventStart, rt.Slug
	rt.mu.RUnlock()
	if eng == nil {
		return
	}
	// 锚与判定路径同源（engine.WindowAnchor）: 取锚通道命中前恒 0, 曲线从 +2s 起才有锚。
	anchor, _ := eng.WindowAnchor()
	p := tail.CurvePoint{
		Ts:     tick.Ts,
		Rem:    tick.Rem,
		Anchor: anchor,
		Twap:   tick.TwapPrice,
		Spot:   tick.BinPrice,
	}
	p.Tie = rt.curve.tieFor(anchor, tick)
	p.Extrap = tail.ExtrapPrice(anchor, tick.BinPrice, tick.Rem, remMax)
	rt.curve.add(start, slug, p)
}

// tieFor 是本 tick 的结算线读数（不适用时返回 0）。
//
// 序列取自缓冲、**不含本 tick**（缓冲此刻还没有这一条）——与 tail.TieAt 的契约一致。
func (b *curveBuf) tieFor(anchor float64, tick flip.Tick) float64 {
	cur := tail.CurvePoint{Ts: tick.Ts, Rem: tick.Rem, Spot: tick.BinPrice}
	return tail.TieAt(b.snapshot().Points, anchor, cur)
}

// Curve 实现 tail.Snapshotter（Dashboard 每 1s 轮询）。
//
// windowSec 在这里补上（而不是塞进 curveBuf）: 缓冲只管「谁的点、什么点」,
// 窗口长度是市场结构（btc-updown-5m），是另一件事。
func (rt *runtimeState) Curve() tail.Curve {
	c := rt.curve.snapshot()
	c.WindowSec = windowSec
	return c
}
