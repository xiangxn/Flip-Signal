package tail

// 判定逻辑（纯函数）——本文件只放规则求值与派生量, 不持有状态、不做 I/O。
// 状态机与快照采集见 engine.go。
//
// 口径来源: docs/tail_sweep_2026-09-22.md §1.3 / §2,
// 实现对照: python/v4/13_tail_sweep.py（快照与五格）+ 15_tail_sweep_union_sigma.py（⑤ 定向）。

import "github.com/necklace/flip-signal/internal/flip"

// EvalRules 求四条原始腿（纯函数）。
//
//   - stage  本行所属的判定段——**唯一的作用是按段选价格腿的比较符**（见 PriceLeg:
//     T=150 段严格大于, 其余段 ≥）。其余三条腿与段无关。
//   - hotAsk 热门侧**有效价**（ask 优先、bid 兜底, 见 HotBook）——即成交价口径
//   - dev    sgn·(spot − anchor)，**美元**——不要传 bps 量（python 13 里的 d_spot 是
//     bps、15 里的 sd 是美元, 混用会得到 n=17 这种量级完全错误的族, 故本包一律用美元）
//   - sd     hist_bps·anchor/1e4，**美元**（该窗 1σ 的美元值）
//   - hasSigma σ 是否可用。**必须显式传入**, 不能靠 sd > 0 判断: sd = 0 会让
//     dev ≥ sd 恒真, ③④ 会在没有历史的窗口全放行（正是冷启动期最危险的那批）。
//
// 返回值是四条**原始**腿; 五格与价格腿的作用域见 Rules 的 Rule1…Rule5。
//
// ⚠️ T=150 段的**入场闸**（WalkLeg）**不在这里**——它是 decision() 里价格腿与 ⑤ 之后的
// 一道独立闸（2026-09-29 决策 #29）, 也不是五格的组成部分（五格是本族的历史口径,
// 回测对照用; 入场闸是链的准入）。理由见 WalkLeg 注释。
func EvalRules(cfg Config, stage string, hotAsk, dev, sd float64, hasSigma bool) Rules {
	sigma := hasSigma && dev >= sd
	return Rules{
		Price:      PriceLeg(cfg, stage, hotAsk),
		Dev63:      dev >= cfg.DevMinUSD,
		Sigma:      sigma,
		SigmaUSD40: sigma && sd >= cfg.SigmaMinUSD,
	}
}

// PriceLeg 价格腿（纯函数）: 热门侧有效价是否过价格闸。除入场闸（WalkLeg）外
// **本包唯一的段相关腿**——
//
//	T=150 段（StageT150）: hotAsk >  PriceMin  （严格大于）
//	其余段（T=60 / 监听）: hotAsk >= PriceMin
//
// 为什么 T=150 要严格大于（2026-09-26 用户决定, 依据见
// docs/tail_integrated_2026-09-24.md §6）: 报价落在 0.01 的 tick 网格上,
// `hotAsk == 0.80` 是**常态形态**而非浮点边界——两个样本里它都是整条价格梯度上
// 唯一的负 EV 档（实盘 09-24~25: n=4 WR 50% −14.38U, 而同批 >0.80 的 201 笔
// WR 98.5% +70.15U; 14 天回测: n=17 WR 76.5% −1.50U）。T=60 与监听段**不动**:
// 那两段的价格本就更高（回测均价 0.978 / 0.974）, 0.80 入场几乎只出现在 T=150。
//
// ⚠️ 两者的差别只在 `hotAsk == PriceMin` 这一格上（同一格从「恰好放行」变成
// 「恰好拦下」）——但它是确定的整数格, 不是浮点噪声, 故 parity 可逐位对账。
func PriceLeg(cfg Config, stage string, hotAsk float64) bool {
	if stage == StageT150 {
		return hotAsk > cfg.PriceMin
	}
	return hotAsk >= cfg.PriceMin
}

// ── T=150 段入场闸（walk 腿, 2026-09-29 决策 #29）──

// WalkUSD 已结算位移（**美元**, 正 = 朝押注方向）= sgn·(twap − anchor)。
//
// 与 DevUSD 的关系（恒等式, 逐 tick 残差 0）:
//
//	dev  = sgn·(spot − anchor) = basis + walk
//	basis = sgn·(spot − twap)    缺口 = **领先量**（现货动了、结算线还没跟上）
//	walk  = sgn·(twap − anchor)  位移 = **状态量**（唯一已经落进结算线的那部分）
//
// ⚠️ 单位是美元（同 DevUSD）: twap 与 anchor 都是价格本身, 不是 bps。
func WalkUSD(side string, twap, anchor float64) float64 {
	return SgnFor(side) * (twap - anchor)
}

// WalkLeg 入场闸（纯函数）: **仅 T=150 段生效**——该段要 `walk ≥ cfg.WalkMinUSD` 才放行。
//
// 为什么只闸第一段（2026-09-29 用户决定, 决策 #29）: 三段链里段 1 是**位移最没写进去**
// 的决策点（rem=150, 也是报价最便宜的入场点）, 「`dev ≥ 63` 由缺口撑起来」的单最容易
// 在这里混进来; 而闸掉它**不等于**该窗不下单——链继续走到 T=60/监听, 多数会在更高的价
// 上重入（14 天: 250 个被闸的 T150 信号里 197 个改道, 53 个整窗死亡）。把闸加到全部
// 三段（A 口径）多砍 258 笔净效果为零的交易; 改成「整窗不下单」（B 口径）会把改道这
// 条路也堵死, Δ 只有 +14.48U 且区间含零——两种都不如只闸段 1（+28.93U）。
//
// ⚠️ **阈值取 `cfg.WalkMinUSD`（默认 43）, 不是硬编码常量**: 它是 **BTC 价格的标定量**,
// 换标的（ETH 等）必须自己标定一份（决策 #25）——键的存在就是为这件事（+28.93U 这个数
// 只对 BTC 的 43 成立）, **不代表在 BTC 上该调它**。
//
// ⚠️ **twap 缺失（≤0）⇒ 放行**（fail-open）: 数据洞不该变成「静默不下单」, 且与
// oracle / 分析脚本（`walk is None ⇒ 放行`）同口径。14 天数据里 T=150 判定 tick 的
// twap 从不缺失（§5 审计: 缺 twap 的 tick 只在判定区外）, 故这一支零命中。
func WalkLeg(cfg Config, stage, side string, twap, anchor float64) bool {
	if stage != StageT150 {
		return true // 段 2/3 一字不动（这一段链的规则差异只有价格腿与本闸）
	}
	if !(twap > 0) {
		return true
	}
	return WalkUSD(side, twap, anchor) >= cfg.WalkMinUSD
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
