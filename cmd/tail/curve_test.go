package main

import (
	"testing"

	"github.com/necklace/flip-signal/internal/flip"
	"github.com/necklace/flip-signal/internal/tail"
)

// 本文件只测**实时缓冲**（换窗/封顶/快照/接线）; 三条派生线的数学在
// `internal/tail/curve_test.go`——公式搬去那边是为了让 Dashboard 的 events 重建
// 共用同一份实现（决策 #31）, 测试自然跟着走。

// TestCurveBufSwapOnNewWindow 钉住缓冲的换窗语义（前端口径的另一半）:
// 新窗第一个采样到来**之前**返回的仍是上一窗的整条曲线（页面上旧曲线不擦）,
// 第一个采样到来时整条换装（旧窗的点一条不留）。
func TestCurveBufSwapOnNewWindow(t *testing.T) {
	var b curveBuf
	if got := b.snapshot(); got.EventStart != 0 || len(got.Points) != 0 {
		t.Fatalf("空缓冲 = %+v, 期望 0/空", got)
	}

	b.add(1000, "w1", tail.CurvePoint{Ts: 1000 * 1000, Rem: 300, Anchor: 100000})
	b.add(1000, "w1", tail.CurvePoint{Ts: 1000*1000 + 1000, Rem: 299, Anchor: 100000, Spot: 100010})

	got := b.snapshot()
	if got.EventStart != 1000 || got.Slug != "w1" || len(got.Points) != 2 {
		t.Fatalf("窗内追加后 = %+v（%d 点）, 期望 1000/w1/2", got, len(got.Points))
	}

	// 新窗第一个采样: 整条换装（旧窗 2 点丢弃）, 且此时才换 EventStart
	b.add(1300, "w2", tail.CurvePoint{Ts: 1300 * 1000, Rem: 300})
	got = b.snapshot()
	if got.EventStart != 1300 || got.Slug != "w2" || len(got.Points) != 1 {
		t.Fatalf("换窗后 = %+v（%d 点）, 期望 1300/w2/1", got, len(got.Points))
	}
	if got.Points[0].Ts != 1300*1000 {
		t.Fatalf("换窗后留下的不是新窗的点: %+v", got.Points[0])
	}
}

// TestCurveBufCapsNewPoints 到顶后丢新点、不挤旧点: 曲线要看的是「从边界到闭市」的
// 整条轨迹, 挤掉起点等于把锚与开局的位移一起抹掉。
func TestCurveBufCapsNewPoints(t *testing.T) {
	var b curveBuf
	for i := 0; i < curveMaxPoints+5; i++ {
		b.add(1000, "w1", tail.CurvePoint{Ts: int64(1000*1000 + i*1000), Rem: 300 - i})
	}
	got := b.snapshot()
	if len(got.Points) != curveMaxPoints {
		t.Fatalf("点数 = %d, 期望封顶 %d", len(got.Points), curveMaxPoints)
	}
	if got.Points[0].Rem != 300 {
		t.Fatalf("首点被挤掉了: rem=%d, 期望 300", got.Points[0].Rem)
	}
	if got.Points[len(got.Points)-1].Rem != 300-(curveMaxPoints-1) {
		t.Fatalf("末点 = %+v", got.Points[len(got.Points)-1])
	}
}

// TestCurveBufSnapshotIsCopy 快照必须是副本: dashboard 序列化的同时主循环还在追加,
// 共享底层数组会踩到正在被编码的切片。
func TestCurveBufSnapshotIsCopy(t *testing.T) {
	var b curveBuf
	b.add(1000, "w1", tail.CurvePoint{Ts: 1000 * 1000, Rem: 300, Spot: 100})
	got := b.snapshot()
	b.add(1000, "w1", tail.CurvePoint{Ts: 1000*1000 + 1000, Rem: 299, Spot: 200})
	if len(got.Points) != 1 || got.Points[0].Spot != 100 {
		t.Fatalf("快照被后续追加污染: %+v", got.Points)
	}
}

// TestCurveWindowSecFromRuntime 窗口量程由 runtime 补（缓冲不认识市场结构）。
func TestCurveWindowSecFromRuntime(t *testing.T) {
	rt := &runtimeState{}
	rt.curve.add(1000, "w1", tail.CurvePoint{Ts: 1000 * 1000, Rem: 300})
	if got := rt.Curve(); got.WindowSec != windowSec || got.EventStart != 1000 {
		t.Fatalf("Curve() = %+v, 期望 WindowSec=%d / EventStart=1000", got, windowSec)
	}
}

// TestSampleCurveWithoutEngine 跳窗路径（Engine=nil）不采样: 缓冲保持上一窗不动,
// 页面上继续显示上一窗曲线（下一个真窗口的首个采样才换装）。窗口间 EventStart 已被
// clearWindow 清零——若采样不判空, 这里会拿 0 去换装, 把上一窗曲线当场擦掉。
func TestSampleCurveWithoutEngine(t *testing.T) {
	rt := &runtimeState{}
	rt.curve.add(1000, "w1", tail.CurvePoint{Ts: 1000 * 1000, Rem: 300})
	rt.sampleCurve(flip.Tick{Ts: 1300 * 1000, Rem: 299, TwapPrice: 100000, BinPrice: 100000}, 150)
	got := rt.Curve()
	if got.EventStart != 1000 || len(got.Points) != 1 {
		t.Fatalf("无引擎时缓冲被动了: %+v", got)
	}
}

// TestCurveTieForReadsBuffer 钉住接线: tieFor 把**当前缓冲**喂给 tail.TieAt, 本 tick
// 不在缓冲里（sampleCurve 是先算 tie 再 add）——缓冲 + 本 tick 各自只算一次。
// 区间/闸门/补样本的细节在 `internal/tail` 的 TestTieAt, 这里只要走到量上。
func TestCurveTieForReadsBuffer(t *testing.T) {
	const baseMs = 1000 * 1000
	var b curveBuf
	for i := 1; i <= 30; i++ { // 本点之前那 30 秒, 现货恒 100010
		b.add(1000, "w1", tail.CurvePoint{Ts: baseMs - int64(i)*1000, Rem: 30 + i, Spot: 100010})
	}
	// P = (60·100000 − 30·100010)/30
	if got := b.tieFor(100000, flip.Tick{Ts: baseMs, Rem: 30, BinPrice: 100010}); got != 99990 {
		t.Fatalf("tieFor = %v, 期望 99990", got)
	}
	// rem=60 ⇒ 一秒未定局 ⇒ 恰好落在 anchor 上（不看缓冲）
	if got := b.tieFor(100000, flip.Tick{Ts: baseMs, Rem: 60, BinPrice: 100010}); got != 100000 {
		t.Fatalf("rem=60 = %v, 期望 anchor 100000", got)
	}
	if got := b.tieFor(0, flip.Tick{Ts: baseMs, Rem: 30, BinPrice: 100010}); got != 0 {
		t.Fatalf("锚缺失应无值: %v", got)
	}
}
