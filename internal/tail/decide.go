package tail

// 判定逻辑（纯函数）——本文件只放规则求值与派生量, 不持有状态、不做 I/O。
// 状态机与快照采集见 engine.go。
//
// 口径来源: docs/tail_sweep_2026-09-22.md §1.3 / §2,
// 实现对照: python/v4/13_tail_sweep.py（快照与五格）+ 15_tail_sweep_union_sigma.py（⑤ 定向）。

import "github.com/necklace/flip-signal/internal/flip"

// EvalRules 求四条原始腿（纯函数）。
//
//   - hotAsk 热门侧**有效价**（ask 优先、bid 兜底, 见 HotBook）——即成交价口径
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

// 有效价来源（Observation.HotSrc 取值）: 本行的热门侧价格吃的是 ask 还是 bid。
// 审计用——`bid` 意味着那一侧**卖单被整侧撤空**, 我们只能按买价挂单（见 HotBook）。
const (
	BookSrcAsk = "ask"
	BookSrcBid = "bid"
)

// HotBook 返回热门侧与它的**有效价**（纯函数）, 以及该价格来自 ask 还是 bid。
//
// 口径（2026-09-24 用户决定, a.md 第 2 条）: **每侧有效价 = `ask > 0 ? ask : bid`**,
// 热门侧 = 有效价高的一侧（平局取 yes, 与 python `ya >= na` 同）。
//
// 为什么不是「只要 ask」: 扫尾盘买的是**价格高的一侧**, 而那一侧在行情一边倒时
// 会把 ask 侧**整侧撤空**（决策 #21 实盘探针: 空侧起始 rem = 12/28/45/47, 一直空
// 到收盘）——旧口径把「ask 为空」当整簿无效直接丢 tick, 于是尾盘最确定的那段行情
// 反而一行都不产。bid 兜底后, 赢家侧报 0.99 的买单仍能被看见并按它挂单。
//
// 平局与全空的边界: 四档全 0 → 返回 (yes, 0, "") —— 调用方以 `px > 0` 判该 tick
// 无有效盘口（**不是**「缺一侧就无效」: 缺一侧只是那一侧没有报价）。
func HotBook(upBid, upAsk, downBid, downAsk float64) (side string, px float64, src string) {
	yesPx, yesSrc := effectivePrice(upAsk, upBid)
	noPx, noSrc := effectivePrice(downAsk, downBid)
	if noPx > yesPx {
		return flip.SideNo, noPx, noSrc
	}
	return flip.SideYes, yesPx, yesSrc
}

// effectivePrice 单侧有效价 = ask 优先、bid 兜底（两者皆 ≤0 → (0, "")）。
func effectivePrice(ask, bid float64) (float64, string) {
	if ask > 0 {
		return ask, BookSrcAsk
	}
	if bid > 0 {
		return bid, BookSrcBid
	}
	return 0, ""
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
