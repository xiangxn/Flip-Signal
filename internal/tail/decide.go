package tail

// 判定逻辑（纯函数）——本文件只放规则求值与派生量, 不持有状态、不做 I/O。
// 状态机与快照采集见 engine.go。
//
// 口径来源: docs/tail_sweep_2026-09-22.md §1.3 / §2,
// 实现对照: python/v4/13_tail_sweep.py（快照与五格）+ 15_tail_sweep_union_sigma.py（⑤ 定向）。

import "github.com/necklace/flip-signal/internal/flip"

// EvalRules 求四条原始腿（纯函数）。
//
//   - hotAsk 热门侧 ask（成交价口径）
//   - dev    sgn·(spot − anchor)，**美元**——不要传 bps 量（python 13 里的 d_spot 是
//     bps、15 里的 sd 是美元, 混用会得到 n=17 这种量级完全错误的族, 故本包一律用美元）
//   - sd     hist_bps·anchor/1e4，**美元**（该窗 1σ 的美元值）
//   - hasSigma σ 是否可用。**必须显式传入**, 不能靠 sd > 0 判断: sd = 0 会让
//     dev ≥ sd 恒真, ③④ 会在没有历史的窗口全放行（正是冷启动期最危险的那批）。
//
// 返回值是四条**原始**腿; 五格与价格腿的作用域见 Rules 的 Rule1…Rule5。
func EvalRules(cfg Config, hotAsk, dev, sd float64, hasSigma bool) Rules {
	sigma := hasSigma && dev >= sd
	return Rules{
		Price:      hotAsk >= cfg.PriceMin,
		Dev63:      dev >= cfg.DevMinUSD,
		Sigma:      sigma,
		SigmaUSD40: sigma && sd >= cfg.SigmaMinUSD,
	}
}

// ── 派生量（纯函数）──
//
// 引擎快照（engine.snapshot）与 dashboard 的「当前窗口」读数共用这同一组实现——
// 页面上的 dev/sd 必须与落盘行里的 dev/sd 逐位对得上, 不能各算一遍。

// SideOfHot 返回热门侧 = ask 高的一侧（**平局取 yes**, 与 python `ya >= na` 同）。
func SideOfHot(yesAsk, noAsk float64) string {
	if noAsk > yesAsk {
		return flip.SideNo
	}
	return flip.SideYes
}

// HotAskOf 返回热门侧 ask（= 成交价口径）。
func HotAskOf(side string, yesAsk, noAsk float64) float64 {
	if side == flip.SideNo {
		return noAsk
	}
	return yesAsk
}

// SgnFor 返回押注方向符号（押 yes +1 / 押 no −1）。
func SgnFor(side string) float64 {
	if side == flip.SideNo {
		return -1
	}
	return 1
}

// DevUSD 位移（**美元**, 正 = 朝押注方向）= sgn·(spot − anchor)。
// ⚠️ 单位是美元: 传 bps 量会得到量级完全错误的判定（见文件头 EvalRules 注释）。
func DevUSD(side string, spot, anchor float64) float64 {
	return SgnFor(side) * (spot - anchor)
}

// SigmaUSD 该窗 1σ 折美元 = hist_bps·anchor/1e4（hist_bps ≤ 0 = 不可用 → 0）。
func SigmaUSD(histBps, anchor float64) float64 {
	if histBps <= 0 {
		return 0
	}
	return histBps * anchor / 1e4
}
