package flip

import "testing"

// HistState（σ 滚动窗）可用性语义测试（2026-09-16）。
// 价值点: cmd/flip 的「σ 未就绪整窗跳过」闸直接读 Count() < HistMin——它与
// Bps() 返回 0 的条件必须逐点一致, 否则要么放进 σ=0 的窗口（观测行 hist_bps=0,
// 踩 06_oos_review.py 硬检查）, 要么在 σ 可用时误跳整窗（白丢一个可交易窗口）。

// TestHistStateAvailability 可用性判据: Count() < HistMin ⇒ Bps == 0（anchor > 0）,
// 恰达 HistMin 起可用; anchor ≤ 0 恒不可用（锚缺失窗口的第一步是恢复锚, 不在此闸）。
func TestHistStateAvailability(t *testing.T) {
	const anchor = 100_000.0
	h := NewHistState()
	if h.Count() != 0 || h.Bps(anchor) != 0 {
		t.Fatalf("空 σ: count=%d bps=%v, 期望 0/0", h.Count(), h.Bps(anchor))
	}
	for i := 1; i < HistMin; i++ {
		h.Push(float64(i))
		if h.Count() != i || h.Bps(anchor) != 0 {
			t.Fatalf("Push %d 窗（<%d）后 count=%d bps=%v, 期望 %d/0",
				i, HistMin, h.Count(), h.Bps(anchor), i)
		}
	}
	h.Push(float64(HistMin)) // 恰达 HistMin → 可用
	if h.Count() != HistMin {
		t.Fatalf("count=%d, 期望 %d", h.Count(), HistMin)
	}
	if want := 2.0 / anchor * 1e4; h.Bps(anchor) != want { // (1+2+3)/3 = 2 $
		t.Fatalf("bps=%v, 期望 %v", h.Bps(anchor), want)
	}
	for _, bad := range []float64{0, -1} {
		if h.Bps(bad) != 0 {
			t.Fatalf("anchor=%v 时 bps=%v, 期望 0", bad, h.Bps(bad))
		}
	}
}

// TestHistStateRollingWindow 容量: 只保留最近 HistWindows 个振幅（Seed 与 Push 同款截断）。
func TestHistStateRollingWindow(t *testing.T) {
	const anchor = 100_000.0
	h := NewHistState()
	for i := 1; i <= HistWindows+5; i++ { // 推送 HistWindows+5 窗（超容量 5 个）
		h.Push(float64(i))
	}
	if h.Count() != HistWindows {
		t.Fatalf("count=%d, 期望 %d", h.Count(), HistWindows)
	}
	// 只留最近 18 个（6..23）, 均值 14.5
	if want := 14.5 / anchor * 1e4; h.Bps(anchor) != want {
		t.Fatalf("bps=%v, 期望 %v（最旧的 5 窗应已滚出）", h.Bps(anchor), want)
	}

	s := NewHistState()
	vals := make([]float64, 0, HistWindows+5)
	for i := 1; i <= HistWindows+5; i++ {
		vals = append(vals, float64(i))
	}
	s.Seed(vals) // 预热给多了也只留最近的
	if s.Count() != HistWindows {
		t.Fatalf("Seed 后 count=%d, 期望 %d", s.Count(), HistWindows)
	}
	if want := 14.5 / anchor * 1e4; s.Bps(anchor) != want {
		t.Fatalf("Seed 后 bps=%v, 期望 %v", s.Bps(anchor), want)
	}
}

// TestHistStateSeedEmptyNoClear 空 Seed 是 no-op（不清空已有样本）——网络预热拿到
// 空结果时不得抹掉本地种子（warmupSigma 两条路径共用同一 hist）。
func TestHistStateSeedEmptyNoClear(t *testing.T) {
	h := NewHistState()
	h.Seed([]float64{1, 2, 3})
	before := h.Bps(100_000)
	h.Seed(nil)
	if h.Count() != HistMin || h.Bps(100_000) != before {
		t.Fatalf("空 Seed 改动了样本: count=%d bps=%v（原 %v）", h.Count(), h.Bps(100_000), before)
	}
}
