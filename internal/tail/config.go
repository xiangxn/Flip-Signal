package tail

// 策略配置（Config）与它的默认值工厂（DefaultConfig）——本文件只放这两样。
// 数据/状态类型见 types.go；判定逻辑见 decide.go。与 internal/flip/config.go 同构。
//
// 之所以独立成文件: Config 是唯一会跨包使用的类型（cmd/tail 经 internal/config
// 构造后传入），默认值又是回测标定的单一真相——塞在 types.go 的常量与状态机之间
// 既不好找、也容易在后续改动里被误伤。

// Config 包含扫尾盘策略的全部可调参数。
//
// ⚠️ **默认值全部不可调**（docs/tail_sweep_2026-09-22.md §1.4）: 文档 §4 已判定
// 「40 美元」这个门槛**不可辨识**（真 argmax = 35，2000 次重采样里 40 一次没中），
// 且 T=60 这个修正只在该 T 上成立、频率本身就不稳（27~192 注/日）。故这些键的
// 意义是「能读、能对账」，不是「该调」——改任何一个都等于换一条没标定过的策略，
// 必须先在 python 侧重做 §5.2 的决策表。
//
// mapstructure tag = 配置文件（v4.config.yaml `tail:` 节）键名。
// 本包只依赖 standard library + internal/flip（纯计算层，无外部依赖）。
type Config struct {
	// RemStart 决策快照的时间腿 T（60）: 首个「rem ≤ 此值」的有效 tick 即判定快照。
	// 这是策略本体（§1.3 的 T=60s），不是可调旋钮——改它就要重做定标（§4.5）。
	RemStart int `mapstructure:"rem_start"`
	// FrameRem 第一帧的时间腿（150）: 首个「rem ≤ 此值」的有效 tick 落一行**原始
	// 快照**（只记录、不判定、不下单），供离线复算 T=150 及更早的规则形态。
	//
	// 为什么是两帧（2026-09-23 用户口径「两帧折中」）: 回测 13_tail_sweep.py 对每个
	// T ∈ {240,180,150,120,90,60,30} 各取一次首帧，逐秒落盘代价太大；两帧是
	// 「能复算 60/150」与「每窗只写 2 行」的折中——代价是 T 不可再调（其余 T 的
	// 样本不存在）。取值必须 ≥ RemStart（校验强制），否则帧闸晚于快照闸。
	FrameRem int `mapstructure:"frame_rem"`
	// PriceMin 价格腿（0.80）: 热门侧 ask 必须 ≥ 此值。⚠️ 它是**全部五格的前置**
	// 条件（python 先按 hot 过滤再判 dev/σ 腿），不是某一格的过滤器——见 decide.go。
	PriceMin float64 `mapstructure:"price_min"`
	// DevMinUSD 美元腿（63）: dev = sgn·(spot − anchor) ≥ 此值即放行（DEV_MIN）。
	DevMinUSD float64 `mapstructure:"dev_min_usd"`
	// SigmaMinUSD σ 腿的美元下限（40）: σ 腿只在「该窗 1σ 折美元 ≥ 此值」时放行
	// （§1.4 的 F；⑤ 相对 ④ 的唯一差别就是这一条）。⚠️ 该值不可辨识, 见类型注释。
	SigmaMinUSD float64 `mapstructure:"sigma_min_usd"`
	// Stake 每笔投入 USDC（2）。paper 股数 = Stake/fill（精确除，不取整）;
	// live 由 trading.OrderSpecForObs 取 floor2（CLOB 最小步长 0.01 股）。
	Stake float64 `mapstructure:"stake"`
	// MaxBookLatMs 盘口延迟闸（300）: BookLatMs > 此值 的 tick 无效（回测 MAX_LAT）。
	// 与 flip.max_book_lat_ms 同值但独立成键——两个引擎可分别部署，共享一个键会让
	// 调其中一个时误改另一个。默认值 = 数据支持值（收紧在 flip 侧已证负收益）。
	MaxBookLatMs int64 `mapstructure:"max_book_lat_ms"`
}

// DefaultConfig 返回文档 §1.4 定稿的默认参数（全部不可调, 见 Config 注释）:
// ⑤ 规则 14 天 n=1536, WR 99.6%, 均价 0.985, P&L +36.9U（每笔 2U）, 0 个亏损日。
//
// 本函数是策略参数的**单一真相**: internal/config 的 defaults() 从这里取值,
// v4.config.yaml 的 `tail:` 节逐键等于它（有漂移守卫测试）。
func DefaultConfig() Config {
	return Config{
		RemStart:     60,   // T: 尾盘 60s 快照（策略本体）
		FrameRem:     150,  // 第二帧: rem≤150 首帧（只记录, 离线可复算 T=150）
		PriceMin:     0.80, // 价格腿 P_FLOOR（五格共同前置）
		DevMinUSD:    63,   // 美元腿 DEV_MIN（USD）
		SigmaMinUSD:  40,   // σ 腿下限 F（USD, 不可辨识——别调）
		Stake:        2,    // 每笔 2 USDC
		MaxBookLatMs: 300,  // 盘口延迟闸（回测 MAX_LAT=300）
	}
}
