package tail

// 判定逻辑（纯函数）——本文件只放规则求值, 不持有状态、不做 I/O。
// 状态机与快照采集见 engine.go。
//
// 口径来源: docs/tail_sweep_2026-09-22.md §1.3 / §2,
// 实现对照: python/v4/13_tail_sweep.py（快照与五格）+ 15_tail_sweep_union_sigma.py（⑤ 定向）。

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
