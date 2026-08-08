// Package config 提供 Flip Signal 的配置加载，包括 YAML 文件、环境变量自动绑定
// 以及敏感字段（私钥/API 凭证）的 AES-256-CBC 解密。
package config

import (
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/go-viper/mapstructure/v2"
	"github.com/spf13/viper"
	sdkmodel "github.com/xiangxn/go-polymarket-sdk/model"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
	pmutils "github.com/xiangxn/go-polymarket-sdk/utils"

	"github.com/necklace/flip-signal/internal/feed"
	"github.com/necklace/flip-signal/internal/flip"
	"github.com/necklace/flip-signal/internal/trading"

	"golang.org/x/term"
)

// ── 顶层配置 ──

// AppConfig 是与 config.yaml 对应的顶层配置结构。
type AppConfig struct {
	Runtime RuntimeConfig         `mapstructure:"runtime"`
	SDK     sdk.Config            `mapstructure:"sdk"`
	Binance feed.BinanceConfig    `mapstructure:"binance"`
	Flip    flip.FlipConfig       `mapstructure:"flip"`
	Trading trading.TradingConfig `mapstructure:"trading"`
}

// RuntimeConfig 保存可通过 CLI 参数覆盖的运行时参数。
type RuntimeConfig struct {
	Symbol        string `mapstructure:"symbol"`         // Binance 交易对
	SlugPrefix    string `mapstructure:"slug_prefix"`    // Polymarket slug 前缀
	OutputPath    string `mapstructure:"output_path"`    // Flip 信号 JSONL 输出路径
	LabOutputDir  string `mapstructure:"lab_output_dir"` // 可选的 Lab 事件输出目录
	DashboardAddr string `mapstructure:"dashboard_addr"` // HTTP Dashboard 监听地址
}

// ── Load ──

// Load 按优先级加载配置：代码默认值（最低）← config.yaml ← 环境变量 PM_*（最高）。
//
// 敏感字段（owner_key / clob_creds.*）支持 AES-256-CBC 加密存储，
// 启动时通过密码解密。密码来源：PM_CONFIG_DECRYPT_PASSWORD 环境变量
// 或交互式终端输入。
func Load(configPath string) (*AppConfig, error) {
	v := viper.New()

	// 环境变量：PM_ 前缀，"." → "_" 自动映射
	v.SetEnvPrefix("PM")
	v.SetEnvKeyReplacer(strings.NewReplacer(".", "_"))
	v.AutomaticEnv()

	// 代码默认值
	cfg := defaults()

	// 文件配置
	if configPath != "" {
		v.SetConfigFile(configPath)
	} else {
		v.SetConfigName("config")
		v.SetConfigType("yaml")
		v.AddConfigPath(".")
	}

	if err := v.ReadInConfig(); err != nil {
		var notFound viper.ConfigFileNotFoundError
		if !errors.As(err, &notFound) {
			return nil, fmt.Errorf("读取配置文件失败: %w", err)
		}
		fmt.Fprintf(os.Stderr, "[Config] 未找到配置文件（%s），使用代码默认值\n", configPath)
	} else {
		fmt.Fprintf(os.Stderr, "[Config] 已加载配置文件: %s\n", v.ConfigFileUsed())
	}

	// 合并：env > file > default
	if err := v.Unmarshal(cfg, viper.DecodeHook(mapstructure.StringToTimeDurationHookFunc())); err != nil {
		return nil, fmt.Errorf("配置反序列化失败: %w", err)
	}

	// 敏感字段解密
	if err := decryptSensitiveFields(cfg); err != nil {
		return nil, err
	}
	if cfg.SDK.Polymarket.OwnerKey != "" {
		cfg.SDK.Polymarket.OwnerKey = strings.TrimPrefix(strings.TrimSpace(cfg.SDK.Polymarket.OwnerKey), "0x")
	}

	return cfg, nil
}

// ── 默认值 ──

func defaults() *AppConfig {
	cfg := &AppConfig{
		Runtime: RuntimeConfig{
			Symbol:        "BTCUSDT",
			SlugPrefix:    "btc-updown-5m",
			OutputPath:    "data/flip_signals.jsonl",
			LabOutputDir:  "data/lab",
			DashboardAddr: ":8090",
		},
		SDK:     *sdk.DefaultConfig(),
		Binance: feed.DefaultBinanceConfig(),
		Flip:    flip.DefaultConfig(),
		Trading: trading.DefaultConfig(),
	}
	// SDK DefaultConfig 内置 dummy OwnerKey，置空以触发只读模式
	cfg.SDK.Polymarket.OwnerKey = ""
	return cfg
}

// ── 解密 ──

// decryptSensitiveFields 检测敏感字段是否为加密值，若是则提示输入密码进行解密。
//
// 若所有敏感字段均为空（已通过环境变量设置），则跳过密码提示。
func decryptSensitiveFields(cfg *AppConfig) error {
	type decryptTarget struct {
		label string
		value *string
	}

	targets := []decryptTarget{
		{label: "sdk.polymarket.owner_key", value: &cfg.SDK.Polymarket.OwnerKey},
	}

	appendCredTargets := func(prefix string, creds *sdkmodel.ApiKeyCreds) {
		if creds == nil {
			return
		}
		targets = append(targets,
			decryptTarget{label: prefix + ".key", value: &creds.Key},
			decryptTarget{label: prefix + ".secret", value: &creds.Secret},
			decryptTarget{label: prefix + ".passphrase", value: &creds.Passphrase},
		)
	}
	appendCredTargets("sdk.polymarket.clob_creds", cfg.SDK.Polymarket.CLOBCreds)

	// 若无加密字段，跳过
	hasEncrypted := false
	for i := range targets {
		if strings.TrimSpace(*targets[i].value) != "" {
			hasEncrypted = true
			break
		}
	}
	if !hasEncrypted {
		return nil
	}

	password, err := readDecryptPassword()
	if err != nil {
		return err
	}
	encryptor := pmutils.NewEncryptor(password)

	for i := range targets {
		raw := strings.TrimSpace(*targets[i].value)
		if raw == "" {
			continue
		}
		decrypted, err := encryptor.Decrypt(raw)
		if err != nil {
			return fmt.Errorf("解密 %s 失败: %w", targets[i].label, err)
		}
		*targets[i].value = strings.TrimSpace(decrypted)
	}
	return nil
}

// readDecryptPassword 获取解密密码。
//
// 优先读取 PM_CONFIG_DECRYPT_PASSWORD 环境变量，否则通过终端安全输入（无回显）。
func readDecryptPassword() (string, error) {
	if envPassword := strings.TrimSpace(os.Getenv("PM_CONFIG_DECRYPT_PASSWORD")); envPassword != "" {
		return envPassword, nil
	}

	fmt.Fprint(os.Stdout, "请输入启动密码: ")
	passwordBytes, err := term.ReadPassword(int(os.Stdin.Fd()))
	fmt.Fprintln(os.Stdout)
	if err != nil {
		return "", fmt.Errorf("读取解密密码失败: %w", err)
	}
	password := strings.TrimSpace(string(passwordBytes))
	if password == "" {
		return "", errors.New("解密密码不能为空")
	}
	return password, nil
}
