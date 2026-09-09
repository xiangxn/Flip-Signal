package flip

// AnchorUsableAtBoundary 纯函数测试: 边界 anchor 采样的新鲜度守卫
// （2026-09-09 review 补, 与窗口结束 σ 采样共用阈值 twapCloseFreshMs,
// 测试恒以 10s 阈值验证）。
// 不可用 → 调用方把 anchor 置 0 → 引擎整窗不观测 + 窗口结束不计入 σ
// （既有 anchor≤0 路径, 镜像回测 :69 锚缺失事件跳过）。

import "testing"

func TestAnchorUsableAtBoundary(t *testing.T) {
	const freshMs = int64(10_000)
	cases := []struct {
		name   string
		price  float64
		ageMs  int64
		usable bool
	}{
		{name: "未推送", price: 0, ageMs: 0, usable: false},
		{name: "刚推送", price: 65_000, ageMs: 0, usable: true},
		{name: "正常龄 p99", price: 65_000, ageMs: 1700, usable: true},
		{name: "恰在阈值边界", price: 65_000, ageMs: 10_000, usable: true},
		{name: "超阈值(55s 缺口级别)", price: 65_000, ageMs: 55_000, usable: false},
		{name: "陈旧且价非法", price: -1, ageMs: 1_000_000, usable: false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := AnchorUsableAtBoundary(c.price, c.ageMs, freshMs); got != c.usable {
				t.Fatalf("AnchorUsableAtBoundary(%v, %d) = %v, 期望 %v", c.price, c.ageMs, got, c.usable)
			}
		})
	}
}
