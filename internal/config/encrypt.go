package config

import (
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	pmutils "github.com/xiangxn/go-polymarket-sdk/utils"
	"golang.org/x/term"
)

// Encrypt 把明文加密成可直接写进配置文件的密文（hex）。
//
// 方案与启动解密同源（pmutils.NewEncryptor: AES-256-CBC, key = SHA256(密码),
// IV = 同一 hash 前 16 字节, PKCS7 填充, 密文 hex），故密文只有配**同一个密码**
// 才能被 Load() 解出来——加密与解密共用同一个密码来源（PM_CONFIG_DECRYPT_PASSWORD
// 或终端无回显，见 readConfigPassword）。
//
// 方案由 SDK 决定、不自行改动: 换 IV/加盐都会让既有密文（以及 master 那套）解不出来。
// 代价是**确定性**——固定 IV 意味着同密码 + 同明文 → 同密文（等值关系会泄露），
// 这与 SDK 的 Decrypt 必须配对，不在这里单方面"修"。
//
// 供 `cmd/flip -encrypt`（换凭证时的离线工具）调用; 引擎主路径不用它。
func Encrypt(text string) (string, error) {
	if strings.TrimSpace(text) == "" {
		return "", errors.New("明文为空——没有可加密的内容")
	}
	password, err := readConfigPassword("请输入配置密码（加密用; 须与启动解密一致）: ")
	if err != nil {
		return "", err
	}
	cipher, err := pmutils.NewEncryptor(password).Encrypt(text)
	if err != nil {
		return "", fmt.Errorf("加密失败: %w", err)
	}
	return cipher, nil
}

// ReadSecret 读一段待加密的敏感明文。
//
// stdin 是终端 → 无回显提示输入（私钥不落屏、不进 shell 历史——这也正是**不**做
// `-encrypt <明文>` 参数形态的原因: 命令行明文会进 shell 历史与 ps）;
// stdin 是管道/文件 → 读整个 stdin 并去掉首尾空白，便于脚本与 CI
// （`printf %s "$KEY" | go run ./cmd/flip -encrypt`）。
//
// 管道形态把**整段输入当作一个明文**（不是逐行切）: 多行秘密（如 PEM 私钥）逐行
// 处理会静默产出多个互不相干的密文。要加密多个值请分开调用（逐条对应粘贴）。
func ReadSecret(prompt string) (string, error) {
	fd := int(os.Stdin.Fd())
	if term.IsTerminal(fd) {
		fmt.Fprint(os.Stderr, prompt)
		raw, err := term.ReadPassword(fd)
		fmt.Fprintln(os.Stderr)
		if err != nil {
			return "", fmt.Errorf("读取明文失败: %w", err)
		}
		return strings.TrimSpace(string(raw)), nil
	}
	raw, err := io.ReadAll(os.Stdin)
	if err != nil {
		return "", fmt.Errorf("读取 stdin 失败: %w", err)
	}
	return strings.TrimSpace(string(raw)), nil
}

// PlaintextPreview 给出明文的「首尾指纹」，供人工核对刚才贴进去的是不是想加密的那个
// 值（-encrypt 的失败模式是贴错东西而密文照样合法——启动时只会被当成"解出来的凭证"，
// 不会报错）。只回显首尾各 4 字符 + 总长度。
//
// 短于 minPreviewLen 的只报长度: 首尾各 4 个字符对短串是太大比例（13 字符的值就露 8 个）。
// 24 是给凭证里的短项留的余量——实际用到的值都更长（私钥 64 hex / 地址 42 / UUID 36 /
// base64 secret ≈ 44）。
func PlaintextPreview(text string) string {
	const (
		edge          = 4
		minPreviewLen = 24
	)
	if len(text) < minPreviewLen {
		return fmt.Sprintf("%d 字符", len(text))
	}
	return fmt.Sprintf("%d 字符（首 %s… 尾 …%s）", len(text), text[:edge], text[len(text)-edge:])
}
