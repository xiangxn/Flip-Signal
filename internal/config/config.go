// Package config 提供引擎配置的加载：YAML 配置文件 + viper 反序列化 + 敏感字段
// （私钥/CLOB 凭证）的 AES-256-CBC 解密，以及启动前的参数校验。
//
// ── 优先级（三层，**不含环境变量层**）──
//
//	CLI flag（cmd/flip 的 -dashboard/-mode/-stake，在 Load 之后覆盖）
//	  > 配置文件（-config 指定的 YAML）
//	    > 代码默认值（本包 defaults()，单一真相）
//
// 实现方式：把 defaults() 的返回值**预置为 Unmarshal 目标**——mapstructure 只写输入
// map 里出现的键，其余字段保留预置值，于是"文件缺哪个键，哪个键就是代码默认值"，
// 配置文件可以只写要改的项。
//
// ── 为什么没有环境变量层（2026-09-16 决定，别再加回来）──
//
// viper 的 AutomaticEnv() 单独使用是**假生效**：Unmarshal → getSettings(AllKeys())
// → 逐键 Get()（viper.go 的 Unmarshal/AllSettings），而 AllKeys() 只汇总
// aliases / override / pflags / **显式 BindEnv** / 配置文件 / kvstore / **SetDefault**，
// AutomaticEnv 探测出来的键不在内——环境变量只在"该键已经出现在配置文件里"时才生效。
// 要修就得逐叶 SetDefault 注册，且 nil 指针子树（如 clob_creds）在 viper flatten 时会被
// 当成叶子**遮蔽其子键**，还得额外逐个 BindEnv，成本高、语义绕，判定不值当。
// 于是本包不调用任何 env 相关 API（SetEnvPrefix/AutomaticEnv/BindEnv/SetDefault）。
// 唯一残留的环境变量是 decrypt.go 的 PM_CONFIG_DECRYPT_PASSWORD（启动解密密码，
// nohup 无终端时解密密文凭证的唯一途径）。
package config

import (
	"fmt"
	"log"
	"strings"

	"github.com/spf13/viper"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"

	"github.com/necklace/flip-signal/internal/feed"
	"github.com/necklace/flip-signal/internal/flip"
	"github.com/necklace/flip-signal/internal/tail"
)

// AppConfig 是与配置文件（v4.config.yaml）对应的顶层配置结构，节顺序即文件顺序。
type AppConfig struct {
	Runtime RuntimeConfig      `mapstructure:"runtime"`
	Flip    flip.Config        `mapstructure:"flip"`
	Tail    tail.Config        `mapstructure:"tail"`
	Feed    FeedConfig         `mapstructure:"feed"`
	Risk    RiskConfig         `mapstructure:"risk"`
	Binance feed.BinanceConfig `mapstructure:"binance"`
	SDK     sdk.Config         `mapstructure:"sdk"`
}

// RuntimeConfig 保存运行/输出/成交模式参数（原 cmd/flip 的 -output/-slug/-mode/-dashboard）。
type RuntimeConfig struct {
	// Mode 成交模式: paper|live（live 需凭证, 缺则降级 paper, 见 cmd/flip.resolveLiveMode）
	Mode string `mapstructure:"mode"`
	// OutputDir 观测 JSONL 输出目录（按日切分; 原 -output, 默认 data/v4）
	OutputDir string `mapstructure:"output_dir"`
	// DashboardAddr HTTP Dashboard 监听地址（如 ":8090"）; 留空 = 不启动
	DashboardAddr string `mapstructure:"dashboard_addr"`
	// SlugPrefix Polymarket slug 前缀（运行时拼接 "-<unix_ts>"; 原 -slug）
	SlugPrefix string `mapstructure:"slug_prefix"`
}

// FeedConfig 保存数据源新鲜度闸（book 阈值在 flip 节，因其属回测口径常量）。
type FeedConfig struct {
	// MaxSpotAgeMs Binance spot 距本地接收超过此值判现货缺失（置 0 → missing_spot 否决）
	MaxSpotAgeMs int64 `mapstructure:"max_spot_age_ms"`
	// MaxTwapAgeMs Chainlink TWAP-60 新鲜度: **只**用于窗末 close 采样（2026-09-19 起
	// 窗口起 anchor 改由 feed.RecoverAnchor 精确匹配边界那一秒的推送, 不再看到达龄）
	MaxTwapAgeMs int64 `mapstructure:"max_twap_age_ms"`
}

// RiskConfig 保存风控参数。
type RiskConfig struct {
	// MaxDailyLoss 日亏熔断线（负值）: 当日（UTC）已结算 P&L ≤ 此值即停单并当日锁存。
	// 口径见 docs/dog020_risk_latency_plan_2026-09-16.md（两模式同源, 纸面只标记）。
	MaxDailyLoss float64 `mapstructure:"max_daily_loss"`
}

// Load 按「CLI flag > 配置文件 > 代码默认值」加载配置。
//
// configPath 为空表示**不使用配置文件**（requirements: 允许 configPath 为 ""）：
// 直接返回代码默认值，不读盘、不接触密文、自然也不会弹解密密码。
// configPath 非空但读不到 → 硬报错（显式指定了就是想要它; 注意 SetConfigFile 读不到
// 文件返回的是 *os.PathError 而非 viper.ConfigFileNotFoundError）。
func Load(configPath string) (*AppConfig, error) {
	cfg := defaults()
	if configPath == "" {
		log.Println("[Config] 未指定配置文件: 全部使用代码默认值")
		return cfg, nil
	}

	v := viper.New()
	v.SetConfigFile(configPath)
	if err := v.ReadInConfig(); err != nil {
		return nil, fmt.Errorf("读取配置文件失败（%s）: %w", configPath, err)
	}

	// UnmarshalExact（ErrorUnused=true）: 拼错或写错 schema 的键 = 启动即失败。
	// 有意选择——策略参数静默回落默认值（如 dist_lo_no 少写个负号）比启动崩溃危险得多。
	// 注意不要传 viper.DecodeHook(...): defaultDecoderConfig 本就组合了
	// StringToTimeDurationHookFunc 且 WeaklyTypedInput=true，显式传会**替换**掉它，
	// sdk.http_timeout: 10s 就解析不出来了。
	if err := v.UnmarshalExact(cfg); err != nil {
		return nil, fmt.Errorf("配置反序列化失败: %w", err)
	}

	if err := decryptSensitiveFields(cfg); err != nil {
		return nil, err
	}
	// 私钥容忍粘贴时的空白与 0x 前缀（SDK 要求不含 0x 的 hex）
	cfg.SDK.Polymarket.OwnerKey = strings.TrimPrefix(strings.TrimSpace(cfg.SDK.Polymarket.OwnerKey), "0x")

	log.Printf("[Config] 已加载配置文件: %s", v.ConfigFileUsed())
	return cfg, nil
}

// defaults 返回全部代码默认值——本包唯一的默认值真相，也是 Unmarshal 的预置目标。
// `v4.config.yaml` 是它的镜像（有漂移测试守着），改这里就要同步改那个文件。
func defaults() *AppConfig {
	return &AppConfig{
		Runtime: RuntimeConfig{
			Mode:          "paper", // 纸面为默认: 实盘必须显式 -mode live 或配置写 live
			OutputDir:     "data/v4",
			DashboardAddr: "", // 留空 = 不启动 Dashboard（保持 v4 现状; master 默认 :8090）
			SlugPrefix:    "btc-updown-5m",
		},
		Flip: flip.DefaultConfig(), // 策略参数单一真相（含 MaxBookLatMs=300）, 见 internal/flip/config.go
		// 扫尾盘（cmd/tail）参数: 与 flip 各自独立成节——两个引擎可分别部署,
		// 共享键会让调其中一个时误改另一个。默认值全部不可调, 见内部注释。
		Tail: tail.DefaultConfig(),
		Feed: FeedConfig{
			// spot 2s: BTC 常态每秒多笔成交，>2s 无推送基本等于链路断流；用本地接收时刻
			// 而非交易所成交时间戳（链路排队/服务器时钟都会让后者失真，见 BinanceAdapter）。
			MaxSpotAgeMs: 2000,
			// TWAP 10s: 窗口起/止两处采样共用——① 窗口结束 σ push（陈旧 close 会把假振幅
			// 污染进其后 18 窗的 σ 尺度，进而扭曲浅洞带判定）; ② 窗口起点 anchor（陈旧
			// anchor 让整窗 dist_s 相对错锚失真，超龄按锚缺失整窗跳过，镜像回测 :69）。
			// 2min 看门狗重建线太粗，够不到数秒~分钟的推送缺口（实测曾现 55s 缺口）；
			// 正常推送龄 p99≈1.7s，10s 余量充足。
			MaxTwapAgeMs: 10_000,
		},
		// 日亏熔断线: 24U 在纸面 14 天回放里 Δ+64.9U 且 4 次触发全落在亏损日、
		// 10 个盈利日零误伤（docs/dog020_risk_latency_plan_2026-09-16.md §3）。
		Risk:    RiskConfig{MaxDailyLoss: -24},
		Binance: feed.DefaultBinanceConfig(),
		SDK:     defaultSDKConfig(),
	}
}

// defaultSDKConfig 构造 SDK 配置的默认值：以 sdk.DefaultConfig() 为基底（端点 URL /
// http_timeout / signature_type 等常量不手抄，SDK 改一处我们跟着走），
// 只覆盖两处 v4 有意偏离项。
//
//  1. **OwnerKey 清空**：SDK 给的是占位私钥 "1111…1111"（64 hex，能过 HexToECDSA），
//     而 v4 用**空串**表达"未配置"——decrypt.go 判「非空即密文」会拿它去解密，
//     main.go 也以空串为「只读纸面 + 生成临时密钥」的判据。不清空则：任何只写要改的
//     项的配置文件（省略 owner_key）启动即报"读取解密密码失败"，且 live 缺 key 时
//     不再降级纸面。
//  2. **429 退避复位 0**：SDK 的 3 次/500ms 会盖掉它自己的内建兜底 6 次/1000ms
//     （polymarket.go 的 defaultRateLimit* 常量，只有 ≤0 才走兜底）。
//
// SignatureType 不再显式赋 POLY_GNOSIS_SAFE（live 签名形态恒为 Gnosis Safe，
// maker=FunderAddress、签名=OwnerKey EOA）：DefaultConfig 已是该值，且
// v4.config.yaml 的 signature_type: 2 受漂移守卫逐键比对，SDK 侧一改就测试失败。
func defaultSDKConfig() sdk.Config {
	cfg := *sdk.DefaultConfig()
	cfg.Polymarket.OwnerKey = ""
	cfg.RateLimitMaxRetries = 0
	cfg.RateLimitBaseDelay = 0
	return cfg
}
