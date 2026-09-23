package config

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	pmutils "github.com/xiangxn/go-polymarket-sdk/utils"
)

// writeConfig 把 YAML 内容写进临时文件，返回路径。
func writeConfig(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "test.config.yaml")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatalf("写临时配置文件失败: %v", err)
	}
	return path
}

// TestLoadEmptyPath 空路径 = 不读文件，直接代码默认值（requirements: 允许 configPath 为 ""）。
func TestLoadEmptyPath(t *testing.T) {
	got, err := Load("")
	if err != nil {
		t.Fatalf("Load(\"\") 应成功: %v", err)
	}
	if want := defaults(); !reflect.DeepEqual(got, want) {
		t.Errorf("Load(\"\") 不等于 defaults():\ngot  %+v\nwant %+v", got, want)
	}
}

// TestLoadPartialFile 缺键保留代码默认值，写了的键覆盖——这是"预置结构体当
// Unmarshal 目标"的核心行为，也是配置文件能只写要改的项的前提。
func TestLoadPartialFile(t *testing.T) {
	path := writeConfig(t, `
flip:
  trigger_ask_max: 0.31
runtime:
  mode: "live"
`)
	got, err := Load(path)
	if err != nil {
		t.Fatalf("Load 失败: %v", err)
	}
	if got.Flip.TriggerAskMax != 0.31 {
		t.Errorf("flip.trigger_ask_max = %v, 期望 0.31", got.Flip.TriggerAskMax)
	}
	if got.Runtime.Mode != "live" {
		t.Errorf("runtime.mode = %q, 期望 live", got.Runtime.Mode)
	}
	// 未提及的键必须是默认值
	if want := defaults(); got.Flip.CrashWindow != want.Flip.CrashWindow ||
		got.Risk.MaxDailyLoss != want.Risk.MaxDailyLoss ||
		got.Runtime.OutputDir != want.Runtime.OutputDir ||
		got.Feed.MaxTwapAgeMs != want.Feed.MaxTwapAgeMs {
		t.Errorf("未提及的键没有保留默认值: %+v", got)
	}
	// nil 指针不能被凭空造出来（resolveLiveMode 的非 nil 判定依赖它）
	if got.SDK.Polymarket.CLOBCreds != nil {
		t.Errorf("文件没写 clob_creds, 结果却是 %+v", got.SDK.Polymarket.CLOBCreds)
	}
}

// TestLoadMissingFile 显式指定的路径不存在 → 硬报错（不是静默回退默认值）。
func TestLoadMissingFile(t *testing.T) {
	if _, err := Load(filepath.Join(t.TempDir(), "nope.yaml")); err == nil {
		t.Fatal("文件不存在时应报错")
	} else if !strings.Contains(err.Error(), "读取配置文件失败") {
		t.Errorf("报错文案不含「读取配置文件失败」: %v", err)
	}
}

// TestLoadUnknownKey 拼错的键 → 启动失败（UnmarshalExact）。
func TestLoadUnknownKey(t *testing.T) {
	path := writeConfig(t, `
flip:
  dist_lo_no: -1.0
  dist_lo_no_typo: -1.0
`)
	if _, err := Load(path); err == nil {
		t.Fatal("未知键应导致加载失败（ErrorUnused）")
	}
}

// TestLoadDecryptSensitive 密文凭证经 PM_CONFIG_DECRYPT_PASSWORD 解密，
// 并顺带校验 owner_key 的 0x 前缀与空白被剥掉。
func TestLoadDecryptSensitive(t *testing.T) {
	const password = "test-password"
	enc := pmutils.NewEncryptor(password)
	ownerKey, err := enc.Encrypt(" 0x" + strings.Repeat("ab", 32) + " ")
	if err != nil {
		t.Fatalf("加密 owner_key 失败: %v", err)
	}
	clobKey, err := enc.Encrypt("clob-key-1")
	if err != nil {
		t.Fatalf("加密 clob key 失败: %v", err)
	}
	clobSecret, err := enc.Encrypt("clob-secret-1")
	if err != nil {
		t.Fatalf("加密 clob secret 失败: %v", err)
	}
	clobPassphrase, err := enc.Encrypt("clob-passphrase-1")
	if err != nil {
		t.Fatalf("加密 clob passphrase 失败: %v", err)
	}
	relayerKey, err := enc.Encrypt("relayer-key-1")
	if err != nil {
		t.Fatalf("加密 relayer key 失败: %v", err)
	}
	relayerKeyAddr, err := enc.Encrypt("0xrelayeraddr")
	if err != nil {
		t.Fatalf("加密 relayer key_address 失败: %v", err)
	}

	path := writeConfig(t, `
sdk:
  polymarket:
    owner_key: "`+ownerKey+`"
    clob_creds:
      key: "`+clobKey+`"
      secret: "`+clobSecret+`"
      passphrase: "`+clobPassphrase+`"
    relayer_key:
      key: "`+relayerKey+`"
      key_address: "`+relayerKeyAddr+`"
`)
	t.Setenv("PM_CONFIG_DECRYPT_PASSWORD", password)

	got, err := Load(path)
	if err != nil {
		t.Fatalf("Load 失败: %v", err)
	}
	if want := strings.Repeat("ab", 32); got.SDK.Polymarket.OwnerKey != want {
		t.Errorf("owner_key = %q, 期望 %q（应已剥掉空白与 0x）", got.SDK.Polymarket.OwnerKey, want)
	}
	if got.SDK.Polymarket.CLOBCreds == nil {
		t.Fatal("clob_creds 不应为 nil")
	}
	if got.SDK.Polymarket.CLOBCreds.Key != "clob-key-1" {
		t.Errorf("clob_creds.key = %q, 期望 clob-key-1", got.SDK.Polymarket.CLOBCreds.Key)
	}
	if got.SDK.Polymarket.RelayerKey == nil {
		t.Fatal("relayer_key 不应为 nil")
	}
	if got.SDK.Polymarket.RelayerKey.ApiKey != "relayer-key-1" {
		t.Errorf("relayer_key.key = %q, 期望 relayer-key-1", got.SDK.Polymarket.RelayerKey.ApiKey)
	}
	if got.SDK.Polymarket.RelayerKey.ApiKeyAddress != "0xrelayeraddr" {
		t.Errorf("relayer_key.key_address = %q, 期望 0xrelayeraddr", got.SDK.Polymarket.RelayerKey.ApiKeyAddress)
	}
	// 明文凭证写着（未加密）→ 解密失败、启动报错（判定语义: 非空即密文）
	if _, err := Load(writeConfig(t, "sdk:\n  polymarket:\n    owner_key: \""+strings.Repeat("cd", 32)+"\"\n")); err == nil {
		t.Error("明文 owner_key 应解密失败")
	}
	// relayer_key 整块同语义: key 或 key_address 任一是明文即启动失败
	if _, err := Load(writeConfig(t, "sdk:\n  polymarket:\n    relayer_key:\n      key: \"plaintext-key\"\n")); err == nil {
		t.Error("明文 relayer_key.key 应解密失败")
	}
	if _, err := Load(writeConfig(t, "sdk:\n  polymarket:\n    relayer_key:\n      key_address: \"0xdeadbeef\"\n")); err == nil {
		t.Error("明文 relayer_key.key_address 应解密失败（整块同一口径）")
	}
}

// TestEncryptRoundTrip 加密工具（-encrypt）的产出必须能被 Load() 解回来——同一个密码、
// 同一个方案，这是「生成的密文可以放心粘进配置」的唯一凭据。
func TestEncryptRoundTrip(t *testing.T) {
	// 复用测试里既有的密码来源: Encrypt 与 Load 都走 PM_CONFIG_DECRYPT_PASSWORD
	t.Setenv("PM_CONFIG_DECRYPT_PASSWORD", "test-password")

	ownerKey, err := Encrypt("owner-secret-1")
	if err != nil {
		t.Fatalf("Encrypt(owner_key) 失败: %v", err)
	}
	relayerKey, err := Encrypt("relayer-key-1")
	if err != nil {
		t.Fatalf("Encrypt(relayer_key.key) 失败: %v", err)
	}
	relayerAddr, err := Encrypt("0xrelayeraddr")
	if err != nil {
		t.Fatalf("Encrypt(relayer_key.key_address) 失败: %v", err)
	}
	if ownerKey == "owner-secret-1" {
		t.Fatal("密文与明文相同——没有真的加密")
	}

	path := writeConfig(t, `
sdk:
  polymarket:
    owner_key: "`+ownerKey+`"
    relayer_key:
      key: "`+relayerKey+`"
      key_address: "`+relayerAddr+`"
`)
	got, err := Load(path)
	if err != nil {
		t.Fatalf("Load 加密工具产出的密文失败: %v", err)
	}
	if got.SDK.Polymarket.OwnerKey != "owner-secret-1" {
		t.Errorf("owner_key 解回 %q, 期望 owner-secret-1", got.SDK.Polymarket.OwnerKey)
	}
	if got.SDK.Polymarket.RelayerKey == nil ||
		got.SDK.Polymarket.RelayerKey.ApiKey != "relayer-key-1" ||
		got.SDK.Polymarket.RelayerKey.ApiKeyAddress != "0xrelayeraddr" {
		t.Errorf("relayer_key 解回 %+v, 期望 {relayer-key-1 0xrelayeraddr}", got.SDK.Polymarket.RelayerKey)
	}

	// 空明文 = 拒绝: 产出「空秘密」的密文只会让配置在启动时才暴露问题
	if _, err := Encrypt("   "); err == nil {
		t.Error("空明文应报错")
	}
}

// TestPlaintextPreview 自检指纹: 短串只报长度（不把大半个秘密回显出来），
// 长串首尾各 4 字符。
func TestPlaintextPreview(t *testing.T) {
	cases := []struct{ name, in, want string }{
		{"私钥（64 字符）", strings.Repeat("ab", 32), "64 字符（首 abab… 尾 …abab）"},
		{"恰好到阈值 23", strings.Repeat("c", 23), "23 字符"},
		{"阈值 24", strings.Repeat("c", 24), "24 字符（首 cccc… 尾 …cccc）"},
		{"短串", "short", "5 字符"},
	}
	for _, c := range cases {
		if got := PlaintextPreview(c.in); got != c.want {
			t.Errorf("%s: PlaintextPreview = %q, 期望 %q", c.name, got, c.want)
		}
	}
}

// TestValidate 校验规则表。
func TestValidate(t *testing.T) {
	base := func() *AppConfig { return defaults() }

	cases := []struct {
		name     string
		mutate   func(*AppConfig)
		wantErr  string // 非空则期望报错且文案包含它
		wantWarn int
	}{
		{name: "默认值合法"},
		{
			name:    "book 阈值非正",
			mutate:  func(c *AppConfig) { c.Flip.MaxBookLatMs = 0 },
			wantErr: "新鲜度阈值必须 > 0",
		},
		{
			name:    "spot 阈值非正",
			mutate:  func(c *AppConfig) { c.Feed.MaxSpotAgeMs = -1 },
			wantErr: "新鲜度阈值必须 > 0",
		},
		{
			name:     "book 阈值 < 100 只告警",
			mutate:   func(c *AppConfig) { c.Flip.MaxBookLatMs = 20 },
			wantWarn: 1,
		},
		{
			name:    "日亏线为正",
			mutate:  func(c *AppConfig) { c.Risk.MaxDailyLoss = 24 },
			wantErr: "必须为负值",
		},
		{
			name:    "日亏线为零",
			mutate:  func(c *AppConfig) { c.Risk.MaxDailyLoss = 0 },
			wantErr: "必须为负值",
		},
		{
			name:    "未知模式",
			mutate:  func(c *AppConfig) { c.Runtime.Mode = "Live" },
			wantErr: "runtime.mode",
		},
		{
			name:    "stake 为零",
			mutate:  func(c *AppConfig) { c.Flip.Stake = 0 },
			wantErr: "flip.stake 必须 > 0",
		},
		{
			name:    "输出目录为空",
			mutate:  func(c *AppConfig) { c.Runtime.OutputDir = "" },
			wantErr: "runtime.output_dir",
		},
		// ── tail（扫尾盘）──
		{
			name:    "tail 帧闸早于快照闸",
			mutate:  func(c *AppConfig) { c.Tail.FrameRem = 30 }, // < rem_start 60
			wantErr: "tail.frame_rem",
		},
		{
			name:    "tail 时间腿非正",
			mutate:  func(c *AppConfig) { c.Tail.RemStart = 0 },
			wantErr: "tail.rem_start",
		},
		{
			// >1 的 ask 门槛不可能有 tick 满足 → 信号永远为空。
			name:    "tail 价格腿 > 1",
			mutate:  func(c *AppConfig) { c.Tail.PriceMin = 1.2 },
			wantErr: "tail.price_min",
		},
		{
			name:    "tail 价格腿为 0",
			mutate:  func(c *AppConfig) { c.Tail.PriceMin = 0 },
			wantErr: "tail.price_min",
		},
		{
			name:    "tail 美元腿非正",
			mutate:  func(c *AppConfig) { c.Tail.DevMinUSD = 0 },
			wantErr: "tail.dev_min_usd",
		},
		{
			name:    "tail σ 腿下限为负",
			mutate:  func(c *AppConfig) { c.Tail.SigmaMinUSD = -1 },
			wantErr: "tail.sigma_min_usd",
		},
		{
			name:    "tail stake 为零",
			mutate:  func(c *AppConfig) { c.Tail.Stake = 0 },
			wantErr: "tail.stake 必须 > 0",
		},
		{
			name:    "tail book 阈值非正",
			mutate:  func(c *AppConfig) { c.Tail.MaxBookLatMs = 0 },
			wantErr: "tail.max_book_lat_ms",
		},
		{
			name:     "tail book 阈值 < 100 只告警",
			mutate:   func(c *AppConfig) { c.Tail.MaxBookLatMs = 20 },
			wantWarn: 1,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := base()
			if tc.mutate != nil {
				tc.mutate(cfg)
			}
			warns, err := Validate(cfg)
			if tc.wantErr == "" {
				if err != nil {
					t.Fatalf("不应报错: %v", err)
				}
			} else if err == nil {
				t.Fatalf("应报错（含 %q）", tc.wantErr)
			} else if !strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("报错文案 %q 不含 %q", err, tc.wantErr)
			}
			if len(warns) != tc.wantWarn {
				t.Errorf("warnings = %v, 期望 %d 条", warns, tc.wantWarn)
			}
		})
	}
}
