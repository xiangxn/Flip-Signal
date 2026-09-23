package config

import "fmt"

// Validate 校验最终生效的配置（**必须在 CLI 覆盖之后调用**，判的是真正要用的值）。
//
// 返回 (warnings, err): err 非空即启动失败（误配比不配更危险），warnings 只打印不拦。
// 规则与文案来自原 cmd/flip 的启动校验（2026-09-16 迁入），只是把 flag 名换成配置键名。
func Validate(cfg *AppConfig) (warnings []string, err error) {
	// 阈值 ≤0 会让全部 tick/采样无效（信号静默归零而非报错），必须挡在启动前。
	// book 阈值 <100ms 属负收益区但仍允许（配置化的意义是「能调」），只告警。
	if cfg.Flip.MaxBookLatMs <= 0 || cfg.Feed.MaxSpotAgeMs <= 0 || cfg.Feed.MaxTwapAgeMs <= 0 {
		return nil, fmt.Errorf("数据源新鲜度阈值必须 > 0（flip.max_book_lat_ms=%d feed.max_spot_age_ms=%d feed.max_twap_age_ms=%d）"+
			"——0 会让全部采样判无效、信号静默归零",
			cfg.Flip.MaxBookLatMs, cfg.Feed.MaxSpotAgeMs, cfg.Feed.MaxTwapAgeMs)
	}
	if cfg.Flip.MaxBookLatMs < 100 {
		warnings = append(warnings, fmt.Sprintf("flip.max_book_lat_ms=%d < 100ms —— 14 天回测显示该区为负收益（EV 单调变差），确认无误再用",
			cfg.Flip.MaxBookLatMs))
	}

	// 口径是「当日已结算 P&L ≤ 线值即停单」，正数会让首次判定立刻熔断并锁存整日。
	if cfg.Risk.MaxDailyLoss >= 0 {
		return nil, fmt.Errorf("risk.max_daily_loss 必须为负值（现值 %.2f）——口径是「当日已结算 P&L ≤ 线值即停单」，传正数会立刻永久熔断",
			cfg.Risk.MaxDailyLoss)
	}

	// 未知模式原先会被 resolveLiveMode 静默当作 paper（把 live 写错时你以为在实盘、
	// 其实在纸面），显式判死。
	if cfg.Runtime.Mode != "paper" && cfg.Runtime.Mode != "live" {
		return nil, fmt.Errorf("runtime.mode 只能是 paper|live（现值 %q）", cfg.Runtime.Mode)
	}

	// 显式 -stake 0 曾被当作「用配置值」静默回退；这里让它响亮报错
	// （成交口径 shares = stake/fill，stake=0 会记出一堆 0 股观测）。
	if cfg.Flip.Stake <= 0 {
		return nil, fmt.Errorf("flip.stake 必须 > 0（现值 %.3f）", cfg.Flip.Stake)
	}

	if cfg.Runtime.OutputDir == "" {
		return nil, fmt.Errorf("runtime.output_dir 不能为空（观测 JSONL 输出目录）")
	}

	// ── tail（扫尾盘, cmd/tail）──
	// 与 flip 各自独立成节（见 AppConfig 注释），但仍由同一处校验: 配置是同一个结构体,
	// 一个节写错却在另一个命令里静默生效更危险。
	if cfg.Tail.MaxBookLatMs <= 0 {
		return nil, fmt.Errorf("tail.max_book_lat_ms 必须 > 0（现值 %d）——0 会让全部 tick 判无效、信号静默归零",
			cfg.Tail.MaxBookLatMs)
	}
	if cfg.Tail.MaxBookLatMs < 100 {
		warnings = append(warnings, fmt.Sprintf("tail.max_book_lat_ms=%d < 100ms —— 同 flip 口径为负收益区（EV 单调变差），确认无误再用",
			cfg.Tail.MaxBookLatMs))
	}
	if cfg.Tail.Stake <= 0 {
		return nil, fmt.Errorf("tail.stake 必须 > 0（现值 %.3f）", cfg.Tail.Stake)
	}
	// 两帧的时间腿顺序: 帧闸必须**不晚于**快照闸。写反（frame_rem < rem_start）不会
	// 报错, 只会让帧行在多数窗口里根本不产出（快照已把本窗置 Done, 而首个有效 tick
	// 的 rem 通常还在两者之间）——静默丢一半样本, 故挡在启动前。
	if cfg.Tail.RemStart <= 0 || cfg.Tail.FrameRem <= 0 {
		return nil, fmt.Errorf("tail.rem_start / tail.frame_rem 必须 > 0（现值 %d / %d）",
			cfg.Tail.RemStart, cfg.Tail.FrameRem)
	}
	if cfg.Tail.FrameRem < cfg.Tail.RemStart {
		return nil, fmt.Errorf("tail.frame_rem(%d) 必须 ≥ tail.rem_start(%d)——反序会让帧行静默不产出",
			cfg.Tail.FrameRem, cfg.Tail.RemStart)
	}
	// 价格腿是「热门侧 ask」——超过 1 不可能有 tick 满足（策略静默归零）, ≤0 则全部
	// 放行（把尾盘无条件买满）。两个方向都不该由配置写出来。
	if cfg.Tail.PriceMin <= 0 || cfg.Tail.PriceMin > 1 {
		return nil, fmt.Errorf("tail.price_min 必须在 (0,1] 内（现值 %.3f）——>1 会让信号永远为空", cfg.Tail.PriceMin)
	}
	// 美元腿 ≤0 会让 dev 腿恒真（退化成只有价格腿的 ①）；σ 腿下限为负同理。
	if cfg.Tail.DevMinUSD <= 0 || cfg.Tail.SigmaMinUSD < 0 {
		return nil, fmt.Errorf("tail.dev_min_usd 必须 > 0、tail.sigma_min_usd 必须 ≥ 0（现值 %.3f / %.3f）",
			cfg.Tail.DevMinUSD, cfg.Tail.SigmaMinUSD)
	}

	return warnings, nil
}
