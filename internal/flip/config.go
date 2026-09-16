package flip

// 策略配置（Config）与它的默认值工厂（DefaultConfig）——本文件只放这两样。
// 数据/状态类型见 types.go；判定逻辑见 engine.go。
//
// 之所以独立成文件: Config 是唯一会跨包使用的类型（cmd/flip 经 internal/config
// 构造后传入，cmd/btreplay 直接取默认值），默认值又是回测标定的单一真相——
// 塞在 types.go 的常量与状态机之间既不好找、也容易在后续改动里被误伤。

// Config 包含策略的全部可调参数。
// 默认值与回测标定一致（python/v4/01_backtest_r1.py 常量，2026-09-02 定稿 R1、
// 2026-09-03 引擎切组合版侧别带——BAND_YC/BAND_NO，观察头条，09-15 双样本复验后评估）。
//
// mapstructure tag = 配置文件（v4.config.yaml `flip:` 节）键名，取值与 dashboard
// `/api/config` 的 JSON 键一致（internal/dashboard/handlers.go handleConfig）——
// 2026-09-16 config 包落地时统一，配置/接口/文档三处同名。
// 本包仍零第三方依赖（struct tag 是纯字符串，不引入 import）。
type Config struct {
	// 触发阈值: 某侧 ask 满足 0 < ask ≤ 此值 即触底观测（0.20）
	TriggerAskMax float64 `mapstructure:"trigger_ask_max"`
	// 急跌腿阈值: m_45 窗口内同侧 ask 曾 ≥ 此值（0.40, CRASH_MIN）
	CrashMinAsk float64 `mapstructure:"crash_min_ask"`
	// 急跌窗口: 触发前 N 个 tick 槽位内求 max（45, CRASH_WINDOW）
	CrashWindow int `mapstructure:"crash_window"`
	// 浅洞带: dist_s 必须 ∈ (lo(side), DistHi) 开区间——侧别带（组合版 2026-09-03）:
	//   DistLoYes: yes 带下界（-0.6, BAND_YC; 破位下行中继族, 稍深）
	//   DistLoNo:  no 带下界（-1.0, BAND_NO; 顶部恐慌族, spot 领先 TWAP → 带深一档）
	// 旧双侧 (−0.5,0)（BAND）退居回测对照（R1），引擎不再使用。
	DistLoYes float64 `mapstructure:"dist_lo_yes"`
	DistLoNo  float64 `mapstructure:"dist_lo_no"`
	DistHi    float64 `mapstructure:"dist_hi"`
	// 时间腿: 仅 rem > 此值 的触发有效（180, REM_MIN）
	RemMin int `mapstructure:"rem_min"`
	// 每信号投入 USDC（2）
	Stake float64 `mapstructure:"stake"`
	// 盘口延迟闸: BookLatMs > 此值 的 tick 无效（300, 回测 MAX_LAT）。
	// 2026-09-16 由包内常量改为配置（配置文件键 flip.max_book_lat_ms）——默认值不变，
	// 配置化的意义是「能调」而非「该调」: 14 天回测按阈值重算，
	// 300ms 在平台期上，往下收紧单调变差（T=20ms EV +0.633→+0.551）。
	MaxBookLatMs int64 `mapstructure:"max_book_lat_ms"`
}

// DefaultConfig 返回回测标定的默认参数（python/v4/01_backtest_r1.py）:
// 组合版侧别带 yes(−0.6,0)+no(−1,0) 纯现货 n=625, WR 24.6%, EV +0.633U/注,
// +396U/14 天（2026-09-03 分桶扫描定带, 纸面引擎现行口径）。
//
// 本函数是策略参数的**单一真相**: internal/config 的 defaults() 与 cmd/btreplay
// 都从这里取值，改标定只改这一处。
func DefaultConfig() Config {
	return Config{
		TriggerAskMax: 0.20, // 0.2 触底（dog@0.2 规则族核心）
		CrashMinAsk:   0.40, // 急跌: 45s 窗口内曾 ≥ 0.40
		CrashWindow:   45,   // 急跌窗（tick 槽位）
		DistLoYes:     -0.6, // yes 浅洞带下界（BAND_YC, 破位下行中继族）
		DistLoNo:      -1.0, // no 浅洞带下界（BAND_NO, 顶部恐慌族, spot 领先 TWAP）
		DistHi:        0.0,  // 浅洞带上界: dist_s<0 = 现货须在狗败侧（方向约束）
		RemMin:        180,  // 窗口前 2 分钟内才观测
		Stake:         2,    // 每笔 2 USDC
		MaxBookLatMs:  300,  // 盘口延迟闸（回测 MAX_LAT=300，见 Config 注释）
	}
}
