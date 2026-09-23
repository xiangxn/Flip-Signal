package feed

import (
	"context"
	"time"

	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// 官方 open/close 取数（2026-09-24, 结算自算的兜底层）。
//
// 与 anchor_recover.go 里的 OpenPriceFetcher 的区别: 那个只取 openPrice（取锚用,
// 当前休眠）; 本取数器一次请求同时拿到 openPrice 与 closePrice——crypto-price 接口
// 本来就一次返回两者，故结算兜底只需**一次网调**即可判胜负。
//
// ⚠️ 调用时机（由 internal/settle 保证, 这里只负责取数）: 官方值在**闭市后 ~40s
// 才收敛**（决策 #14 实测: 头几十秒返回的是收敛中的临时值, 同窗 +6.9s 与 +33s
// 差 1.18bps ≈ 9.5 美元）。本取数器不重试、不判收敛——取到 0 就是未就绪。
//
// btc-updown-5m 口径: symbol=BTC, variant=fiveminute, twapEnabled=true,
// twapLookbackSeconds=60（与 TWAP 适配器同一条 Chainlink TWAP-60 流）。
type PricePairFetcher func(ctx context.Context, windowStart, windowEnd time.Time) (open, close float64)

// NewPricePairFetcher 构造官方 open/close 取数器（nil client = 恒 (0,0), 纸面兜底）。
func NewPricePairFetcher(client *sdk.PolymarketClient, symbol sdk.CryptoPriceSymbol,
	variant sdk.CryptoPriceUint, twapLookbackSeconds int64) PricePairFetcher {
	return func(ctx context.Context, windowStart, windowEnd time.Time) (float64, float64) {
		if client == nil {
			return 0, 0
		}
		return client.FetchOpenPriceContext(ctx, symbol, windowStart.UTC(), windowEnd.UTC(),
			variant, true, int(twapLookbackSeconds))
	}
}
