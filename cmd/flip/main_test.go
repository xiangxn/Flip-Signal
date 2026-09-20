package main

import (
	"testing"

	"github.com/necklace/flip-signal/internal/flip"
)

// ── 未决 GTC 挂单 × 非 live 模式 = 拒绝启动（2026-09-20 review #5）──
//
// pendingResting 就是那道闸的判据: 非 live 模式下它非空即 log.Fatalf（不启动）。
// 这里只测过滤本身——Fatal 分支没法在单测里跑（os.Exit）, 由启动处的调用点兜住。
func TestPendingResting(t *testing.T) {
	const ts = int64(1_780_000_000_000)
	mk := func(status, orderID string) *flip.Record {
		return &flip.Record{
			Observation: flip.Observation{Ts: ts, Side: flip.SideYes, Fill: 0.19, OK: true},
			Date:        "2026-09-20",
			ConditionID: "cond-1",
			Slug:        "btc-updown-5m-1780000000",
			EventStart:  ts / 1000,
			Stake:       2,
			ExecStatus:  status,
			OrderID:     orderID,
		}
	}

	cases := []struct {
		name string
		recs []*flip.Record
		want int
	}{
		{"无记录", nil, 0},
		{"paper 行（exec_status 空）不算", []*flip.Record{mk("", "")}, 0},
		{"已定稿状态都不算", []*flip.Record{
			mk(flip.ExecStatusFilled, "o1"), mk(flip.ExecStatusPartial, "o2"),
			mk(flip.ExecStatusUnfilled, "o3"), mk(flip.ExecStatusRejected, "o4"),
		}, 0},
		{"resting 即命中（缺 order_id 的遗留行也算——一样只能回 live/人工了结）", []*flip.Record{
			mk(flip.ExecStatusResting, "o5"), mk(flip.ExecStatusResting, ""),
		}, 2},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := len(pendingResting(c.recs)); got != c.want {
				t.Fatalf("pendingResting = %d 条, 期望 %d", got, c.want)
			}
		})
	}
}
