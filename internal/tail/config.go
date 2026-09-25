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
// 2026-09-24 起 Config 的两个时间腿换了语义（三段判定链, 见
// docs/tail_integrated_2026-09-24.md §1）: 原来「帧（只记录）+ 快照（判定）」的
// 两帧折中改成**两次真判定**——`t150_rem` 与 `t60_rem` 各自是一个**下单点**，
// 键名随之从 frame_rem / rem_start 改成 t150_rem / t60_rem（旧名指「帧」，
// 会让读到的人以为第一腿只记录）。
//
// mapstructure tag = 配置文件（v4.config.yaml `tail:` 节）键名。
// 本包只依赖 standard library + internal/flip（纯计算层，无外部依赖）。
type Config struct {
	// T150Rem 第一段判定的时间腿（150）: 首个「rem ≤ 此值」的有效 tick 判一次完整 ⑤,
	// 达标即下单；不达标进入第二段。取值必须 > T60Rem（校验强制），否则两段同时触发。
	T150Rem int `mapstructure:"t150_rem"`
	// T60Rem 第二段判定的时间腿（60）: 第一段没出信号时, 首个「rem ≤ 此值」的有效 tick
	// 再判一次 ⑤（同一套规则、同一批输入, 只是时点更晚、热门侧价格更高）; 仍不达标则
	// 进入**监听段**（此后每秒判 ② = 价格腿 ∧ dev ≥ 63 美元, 达标即下单）。
	//
	// 三段任一出信号即整窗只下一单（之后不再判定）。
	T60Rem int `mapstructure:"t60_rem"`
	// PriceMin 价格腿（0.80）: 热门侧**有效价**必须过此值。⚠️ 它是**全部五格的前置**
	// 条件（python 先按 hot 过滤再判 dev/σ 腿），也是监听段 ② 的前置——见 decide.go。
	//
	// 比较符**随段而变**（2026-09-26 起）: T=150 段严格大于（`hot > 0.80`）, T=60 与
	// 监听段仍是 ≥——本键只给阈值, 不给比较符（见 decide.PriceLeg）。
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

// DefaultConfig 返回文档 §1.4 定稿的默认参数（全部不可调, 见 Config 注释）。
//
// 三段链的 14 天真金（2026-08-18~31, 2U/注, oracle =
// python/v4/23_tail_integrated.py, 见 docs/tail_integrated_2026-09-24.md §5/§6）:
// T=150 ⑤ n=1208 WR 94.04% +15.02U / T=60 ⑤ n=577 WR 99.13% +16.15U /
// 监听 ② n=348 WR 97.99% +3.92U ⇒ 合计 n=2133 WR 96.06% **+35.09U**。
// 对照旧口径「只在 T=60 判 ⑤」= n=1536 WR 99.61% +36.93U ——总 P&L 基本持平、
// 注数 +39%、EV/注摊薄, 这是 2026-09-24 用户决定的显式取舍（a.md 第 1 条）。
// 上面四格是 **T=150 段价格腿改严格大于后**的数（2026-09-26, 决策 #26; 改前
// 1220/568/347 = 2135 / 95.97% / +35.67U, 配对 Δ −0.58U 区间含 0）。
// ⚠️ 数字口径以 oracle 为准: 它按决策 #13 跳过 σ 未就绪的 3 个窗（引擎同源）,
// 不跳过时 T=60 段会多 1 笔（n=569 / +15.86U, 即文档 §5 表里的预估值）。
//
// 本函数是策略参数的**单一真相**: internal/config 的 defaults() 从这里取值,
// v4.config.yaml 的 `tail:` 节逐键等于它（有漂移守卫测试）。
func DefaultConfig() Config {
	return Config{
		T150Rem:      150,  // 第一段判定点: rem ≤ 150s（真下单, 不再是「只记录的帧」）
		T60Rem:       60,   // 第二段判定点: rem ≤ 60s（策略本体 T=60）
		PriceMin:     0.80, // 价格腿 P_FLOOR（五格与监听段 ② 共同前置; T=150 段严格大于）
		DevMinUSD:    63,   // 美元腿 DEV_MIN（USD）
		SigmaMinUSD:  40,   // σ 腿下限 F（USD, 不可辨识——别调）
		Stake:        2,    // 每笔 2 USDC
		MaxBookLatMs: 300,  // 盘口延迟闸（回测 MAX_LAT=300）
	}
}
