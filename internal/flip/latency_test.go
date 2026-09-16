package flip

import "testing"

// 2026-09-16 延迟闸配置化 + spot 龄落盘的测试（docs/dog020_risk_latency_plan_2026-09-16.md §2.2/§2.3）。
// 判定行为必须与配置化前逐位一致——阈值只换来源，不改判据。

// TestMaxBookLatMsConfigurable 盘口延迟闸取 Config 值（默认 300 = 回测 MAX_LAT）。
func TestMaxBookLatMsConfigurable(t *testing.T) {
	cases := []struct {
		name    string
		maxLat  int64
		latency int64
		wantOK  bool // 该 tick 是否有效（能触发）
	}{
		{name: "默认 300 内有效", maxLat: 300, latency: 300, wantOK: true},
		{name: "默认 300 外无效", maxLat: 300, latency: 301, wantOK: false},
		{name: "收紧到 50 后 60ms 无效", maxLat: 50, latency: 60, wantOK: false},
		{name: "收紧到 50 后 40ms 仍有效", maxLat: 50, latency: 40, wantOK: true},
		{name: "放宽到 500 后 400ms 有效", maxLat: 500, latency: 400, wantOK: true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			cfg := cfgOK()
			cfg.MaxBookLatMs = c.maxLat
			e := newEng(cfg)
			feed(t, e, 45, 250, 0.6, 0.6) // 急跌窗：同侧曾 ≥0.40

			tk := stdTick(200, 0.18, 0.6)
			tk.BookLatMs = c.latency
			obs := e.ProcessTick(tk)
			if got := obs != nil; got != c.wantOK {
				t.Fatalf("maxLat=%d latency=%d: 触发=%v, 期望 %v", c.maxLat, c.latency, got, c.wantOK)
			}
		})
	}
}

// TestSpotAgePassthrough 触底 tick 的 spot 龄原样进观测（纯诊断，不参与判定）。
func TestSpotAgePassthrough(t *testing.T) {
	e := newEng(cfgOK())
	feed(t, e, 45, 250, 0.6, 0.6)

	tk := stdTick(200, 0.18, 0.6) // spot=anchor → dist_s=0 带外，仍产出观测（诊断口径）
	tk.SpotAgeMs = 1234
	obs := e.ProcessTick(tk)
	if obs == nil {
		t.Fatal("应产出观测（dist 带外亦有观测行）")
	}
	if obs.SpotAgeMs != 1234 {
		t.Fatalf("spot_age_ms 应原样透传: got %d, 期望 1234", obs.SpotAgeMs)
	}
	// 判定结果不受 spot 龄影响（同 tick 只差龄 → 同 reject_reason）
	tk2 := stdTick(200, 0.18, 0.6)
	tk2.SpotAgeMs = 0
	e2 := newEng(cfgOK())
	feed(t, e2, 45, 250, 0.6, 0.6)
	obs2 := e2.ProcessTick(tk2)
	if obs2.RejectReason != obs.RejectReason || obs2.OK != obs.OK {
		t.Fatalf("spot 龄不得影响判定: %s/%v vs %s/%v",
			obs.RejectReason, obs.OK, obs2.RejectReason, obs2.OK)
	}
}
