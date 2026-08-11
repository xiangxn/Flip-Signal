package trading

import (
	"log"
	"strconv"

	"github.com/tidwall/gjson"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// PrefetchTokenInfo 从 gamma API 市场响应中提取 tickSize / negRisk / feeRate，
// 预注入 SDK 内部缓存，避免 CreateOrder 时额外网络请求，降低下单延迟。
//
// marketData 为 FetchMarketBySlug 的返回值，其中包含：
//   - orderPriceMinTickSize: 最小价格变动单位（字符串，如 "0.001"）
//   - negRisk: 是否为负风险市场
//   - feeSchedule.rate: 手续费率（bps）
//
// 这些值与 token 无关（同一市场内 YES/NO 共享），对 tokenIDs 中所有 token
// 统一设置。先清空旧缓存避免 map 无限增长，再注入新周期数据。
func PrefetchTokenInfo(client *sdk.PolymarketClient, marketData *gjson.Result, tokenIDs []string) {
	// 清理上一周期的缓存，防止 tokenID 对应的 map 无限增长
	client.ClearTickSizes()
	client.ClearFeeRates()
	client.ClearNegRisk()

	// tickSize：gamma 返回的是字符串，需转为 float64
	if tsStr := marketData.Get("orderPriceMinTickSize").String(); tsStr != "" {
		ts, err := strconv.ParseFloat(tsStr, 64)
		if err != nil {
			log.Printf("[Prefetch] 解析 tickSize 失败: %v (raw=%s)", err, tsStr)
		} else if ts > 0 {
			for _, tid := range tokenIDs {
				if err := client.SetTickSize(tid, ts); err != nil {
					log.Printf("[Prefetch] 设置 tickSize 失败 (token=%s): %v", tid, err)
				}
			}
		}
	}

	// negRisk：市场级别 bool，直接设置
	negRisk := marketData.Get("negRisk").Bool()
	for _, tid := range tokenIDs {
		client.SetNegRisk(tid, negRisk)
	}

	// feeRate：费率（bps），非零时才设置（多数市场费率为 0）
	if fr := marketData.Get("feeSchedule.rate").Float(); fr > 0 {
		for _, tid := range tokenIDs {
			client.SetFeeRateBps(tid, fr)
		}
	}
}
