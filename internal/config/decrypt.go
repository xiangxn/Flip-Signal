package config

import (
	"errors"
	"fmt"
	"os"
	"strings"

	sdkmodel "github.com/xiangxn/go-polymarket-sdk/model"
	pmutils "github.com/xiangxn/go-polymarket-sdk/utils"
	"golang.org/x/term"
)

// decryptSensitiveFields 检测敏感字段是否为加密值，若是则提示输入密码进行解密。
//
// 加密方案由 SDK 提供（pmutils.NewEncryptor）: AES-256-CBC，key = SHA256(密码)，
// IV = 同一 hash 的前 16 字节，PKCS7 填充，密文 hex 编码 —— 与 master 分支同款。
//
// **判定语义: 非空即视为密文**（没有"看起来像明文"的启发式——那会让损坏的密文被
// 静默当作明文使用，比启动失败更糟）。因此明文凭证写进配置文件会在这里解密失败而
// 启动报错；要么填 Encrypt() 生成的密文，要么留空（留空 = 只读纸面运行）。
//
// 若所有敏感字段均为空，则直接返回、**不提示密码**——纸面运行永不卡在密码输入。
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

	// relayer_key 是另一种凭证结构（headers.RelayerKey 而非 ApiKeyCreds）: key 是
	// API key、key_address 是它的归属地址。**整块**加密（与 clob_creds 同语义）——
	// 只加密其中一个会让另一个以密文形态被当成地址发出去（RELAYER_API_KEY_ADDRESS
	// 头），而"哪个字段该明文"这种半加密规则正是本包刻意不留的东西。
	if rk := cfg.SDK.Polymarket.RelayerKey; rk != nil {
		targets = append(targets,
			decryptTarget{label: "sdk.polymarket.relayer_key.key", value: &rk.ApiKey},
			decryptTarget{label: "sdk.polymarket.relayer_key.key_address", value: &rk.ApiKeyAddress},
		)
	}

	// 若无加密字段，跳过（配置文件里没写凭证 = 只读纸面，不需要密码）
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
// 优先读取 PM_CONFIG_DECRYPT_PASSWORD 环境变量（无人值守启动的唯一途径——nohup/
// systemd 下没有终端，term.ReadPassword 会直接失败），否则通过终端安全输入（无回显）。
func readDecryptPassword() (string, error) {
	if envPassword := strings.TrimSpace(os.Getenv("PM_CONFIG_DECRYPT_PASSWORD")); envPassword != "" {
		return envPassword, nil
	}

	fmt.Fprint(os.Stdout, "请输入启动密码: ")
	passwordBytes, err := term.ReadPassword(int(os.Stdin.Fd()))
	fmt.Fprintln(os.Stdout)
	if err != nil {
		return "", fmt.Errorf("读取解密密码失败（无终端时请设 PM_CONFIG_DECRYPT_PASSWORD）: %w", err)
	}
	password := strings.TrimSpace(string(passwordBytes))
	if password == "" {
		return "", errors.New("解密密码不能为空")
	}
	return password, nil
}
