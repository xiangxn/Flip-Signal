package trading

import "testing"

func TestCanTrade(t *testing.T) {
	cases := []struct {
		name    string
		today   float64
		maxLoss float64
		want    bool
	}{
		{name: "持平可下单", today: 0, maxLoss: -20, want: true},
		{name: "小额亏损可下单", today: -19.99, maxLoss: -20, want: true},
		{name: "恰在熔断线", today: -20, maxLoss: -20, want: false},
		{name: "越过熔断线", today: -20.01, maxLoss: -20, want: false},
		{name: "深亏（-25 默认线外）", today: -25, maxLoss: -20, want: false},
		{name: "当日盈利", today: 30, maxLoss: -20, want: true},
		{name: "自定义更严线", today: -5, maxLoss: -5, want: false},
		{name: "自定义宽松线", today: -5, maxLoss: -10, want: true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := CanTrade(c.today, c.maxLoss); got != c.want {
				t.Fatalf("CanTrade(%.2f, %.2f) = %v, 期望 %v", c.today, c.maxLoss, got, c.want)
			}
		})
	}
}
