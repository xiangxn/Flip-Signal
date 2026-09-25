package feed

import (
	"strings"

	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// Asset 是一个 5 分钟 updown 标的的资产参数。
//
// 同一条市场在三个子系统里各有各的命名，全部由**资产名**派生，一个参数贯穿
// 全程（2026-09-25 消除 BTC 硬编码：后续还要采 SOL/BNB 等）：
//
//	btc → slug btc-updown-5m / Binance BTCUSDT / Chainlink BTC / 目录 data/btc
//	eth → slug eth-updown-5m / Binance ETHUSDT / Chainlink ETH / 目录 data/eth
//
// 采集侧（cmd/collect）四项都可单独覆盖: `-slug` / `-asset` / `-symbol` / `-output`，
// 以应对引号货币不是 USDT（如 1000XXXUSDT）这类标的；引擎侧（cmd/flip、cmd/tail）
// 只认 `runtime.slug_prefix`，符号与交易对随它派生。
type Asset struct {
	Name      string                // btc —— 数据目录名与日志
	Slug      string                // btc-updown-5m —— Polymarket 市场 slug 前缀
	Binance   string                // BTCUSDT —— Binance spot/trade/depth 流交易对
	Chainlink sdk.CryptoPriceSymbol // BTC —— Chainlink TWAP-60 流与 crypto-price 接口
}

// UpdownSuffix 是 Polymarket 5 分钟涨跌市场 slug 的固定后缀（<asset>-updown-5m）。
const UpdownSuffix = "-updown-5m"

// DataDir 返回该资产的默认采集数据目录（btc → data/btc）。一标的一目录，
// 新旧格式混写是采集侧的老坑（见 docs/recollection_plan_2026-08-16.md §3.1）。
func (a Asset) DataDir() string { return "data/" + a.Name }

// AssetFor 由资产名派生全套参数（btc → btc-updown-5m / BTCUSDT / BTC）。
// 大小写与首尾空白不敏感。
func AssetFor(name string) Asset {
	n := strings.ToLower(strings.TrimSpace(name))
	up := strings.ToUpper(n)
	return Asset{
		Name:      n,
		Slug:      n + UpdownSuffix,
		Binance:   up + "USDT",
		Chainlink: sdk.CryptoPriceSymbol(up),
	}
}

// AssetFromSlug 由 slug 前缀反推资产参数（eth-updown-5m → eth / ETHUSDT / ETH）。
// 不含已知后缀时整串当资产名（容错：允许直接写 "eth"）；Slug 字段保留调用方
// 给的原串，便于将来出现非 5 分钟市场（如 1 小时窗）时原样沿用。
func AssetFromSlug(slug string) Asset {
	s := strings.ToLower(strings.TrimSpace(slug))
	a := AssetFor(strings.TrimSuffix(s, UpdownSuffix))
	a.Slug = s
	return a
}

// ApplyBinance 用资产参数补齐 Binance 配置里可派生的字段：Symbol 留空 = 由资产
// 派生（<ASSET>USDT），显式填了就用显式的。**两个下游进程必须都调它**，否则
// 改 slug 前缀换资产时 Binance 流会静默留在默认交易对上（有漂移守卫测试钉住
// 「留空」这个默认值，但接不接线只有调用点能保证）。
func (a Asset) ApplyBinance(cfg BinanceConfig) BinanceConfig {
	if cfg.Symbol == "" {
		cfg.Symbol = a.Binance
	}
	return cfg
}
