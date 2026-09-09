package trading

import (
	"testing"

	"github.com/necklace/flip-signal/internal/flip"
)

func TestOrderSpecForObs(t *testing.T) {
	ok := func(price, stake, wantShares float64) {
		t.Helper()
		obs := &flip.Observation{Fill: price}
		gotP, gotS, err := OrderSpecForObs(obs, stake)
		if err != nil {
			t.Fatalf("fill=%.2f stake=%.2f: %v", price, stake, err)
		}
		if gotP != price || gotS != wantShares {
			t.Fatalf("fill=%.2f stake=%.2f → (%.4f, %.4f), 期望 (%.4f, %.4f)",
				price, stake, gotP, gotS, price, wantShares)
		}
		// cost = shares×price 必须 ≤ stake（向下取整保证）
		if cost := gotS * gotP; cost > stake+1e-9 {
			t.Fatalf("cost %.6f 超 stake %.2f", cost, stake)
		}
	}

	ok(0.19, 2, 10.52)  // 2/0.19 = 10.5263… → floor2 = 10.52
	ok(0.20, 2, 10.00)  // 整档
	ok(0.17, 2, 11.76)  // 2/0.17 = 11.7647…
	ok(0.18, 2, 11.11)  // 2/0.18 = 11.1111…（浮点边界, floor2 的 +1e-9 防尾差）
	ok(0.015, 0.30, 20) // 低价档多股
}

func TestOrderSpecForObsErrors(t *testing.T) {
	cases := []struct {
		name  string
		obs   *flip.Observation
		stake float64
	}{
		{name: "nil 观测", obs: nil, stake: 2},
		{name: "fill 为 0", obs: &flip.Observation{Fill: 0}, stake: 2},
		{name: "fill 为负", obs: &flip.Observation{Fill: -0.1}, stake: 2},
		{name: "stake 为 0", obs: &flip.Observation{Fill: 0.19}, stake: 0},
		{name: "股数取整为 0", obs: &flip.Observation{Fill: 0.19}, stake: 0.001},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if _, _, err := OrderSpecForObs(c.obs, c.stake); err == nil {
				t.Fatal("应返回错误")
			}
		})
	}
}

func TestFloor2(t *testing.T) {
	cases := []struct{ in, want float64 }{
		{10.5263157, 10.52},
		{2 / 0.18, 11.11}, // 浮点: 2/0.18 = 11.111111… ×100 可能得 1111.1111 或边界
		{2 / 0.19, 10.52},
		{2 / 0.2, 10.0}, // 浮点伪影: 2/0.2 常得 9.999999999999998, +1e-9 防尾差
		{1.998, 1.99},
	}
	for _, c := range cases {
		if got := floor2(c.in); got != c.want {
			t.Fatalf("floor2(%v) = %v, 期望 %v", c.in, got, c.want)
		}
	}
}
