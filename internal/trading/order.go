package trading

import (
	"fmt"
	"math"

	"github.com/necklace/flip-signal/internal/flip"
)

// floor2 向下取整到 2 位小数（CLOB size/price 步进 0.01; +1e-9 吸收浮点
// 除法的尾差——如 2/0.19 = 10.5263157… ×100 恰好在整数边界的场景）。
func floor2(x float64) float64 {
	return math.Floor(x*100+1e-9) / 100
}

// OrderSpecForObs 把 ok 观测映射为 live 限价单规格（纯函数, v4 口径）:
//
//	price  = obs.Fill —— 触发 tick 狗侧 ask（引擎已保证 ≤0.20 且为有效盘口价,
//	         按 tick 格点报价）; FAK 限价单只吃 ≤ 该档的 resting ask, 绝不超价
//	shares = floor2(stake/price) —— 向下取整到 0.01, 保证 cost = shares×price
//	         ≤ stake（与回测 shares=stake/fill 同口径, 差额 ≤ 0.01 股不投）
//
// 不做「跳档 0.19 之类超价补足」——折价保护优先于满额投入。
func OrderSpecForObs(obs *flip.Observation, stake float64) (price, shares float64, err error) {
	if obs == nil || obs.Fill <= 0 {
		return 0, 0, fmt.Errorf("OrderSpecForObs: fill 非法（%.4f）", obsFill(obs))
	}
	if stake <= 0 {
		return 0, 0, fmt.Errorf("OrderSpecForObs: stake 非法（%.2f）", stake)
	}
	shares = floor2(stake / obs.Fill)
	if shares <= 0 {
		return 0, 0, fmt.Errorf("OrderSpecForObs: 股数向下取整后为 0（stake=%.2f fill=%.3f）", stake, obs.Fill)
	}
	return obs.Fill, shares, nil
}

// obsFill 兜底取值（nil 观测打印用）。
func obsFill(obs *flip.Observation) float64 {
	if obs == nil {
		return 0
	}
	return obs.Fill
}
