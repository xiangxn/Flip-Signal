package main

import (
	"github.com/tidwall/gjson"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// pmTick 是一秒一条的 PM 盘口采样（flip.Tick 的盘口部分）。
// 引擎包零外部依赖：SDK 类型只在 main 层出现，经 makePMTick 转纯数值喂入。
type pmTick struct {
	UpBid     float64 // UP(=yes) 最优买价
	UpAsk     float64 // UP(=yes) 最优卖价
	DownBid   float64 // DOWN(=no) 最优买价
	DownAsk   float64 // DOWN(=no) 最优卖价
	BookLatMs int64   // 两簿传输延迟较大者（毫秒）
}

// bestBid 返回订单簿最优买价。CLOB bids 升序排列，最优买价在切片末尾；
// 空盘口返回 0。
func bestBid(book *sdk.OrderBook) float64 {
	if book == nil || len(book.Bids) == 0 {
		return 0
	}
	return book.Bids[len(book.Bids)-1].Price
}

// bestAsk 返回订单簿最优卖价。CLOB WS 的 asks 是降序（0.99 填充单在前，
// 真实最优卖价在最后），与 bids 升序对称，最优都在末尾——与 SDK 自身
// GetTokenOrderBook 取 Asks[len-1] 的约定一致。空盘口返回 0。
// ⚠️ 曾误取 Asks[0]（恒为 0.99/1.0 填充价），2026-08-18 数据审计修正。
func bestAsk(book *sdk.OrderBook) float64 {
	if book == nil || len(book.Asks) == 0 {
		return 0
	}
	return book.Asks[len(book.Asks)-1].Price
}

// makePMTick 从 UP/DOWN 订单簿构造 1s 盘口采样（传输延迟取两簿较大者；
// 单簿为空时对应价格字段为 0，由引擎侧判缺失）。
func makePMTick(upBook, downBook *sdk.OrderBook) pmTick {
	t := pmTick{
		UpBid:     bestBid(upBook),
		UpAsk:     bestAsk(upBook),
		DownBid:   bestBid(downBook),
		DownAsk:   bestAsk(downBook),
		BookLatMs: 0,
	}
	if upBook != nil {
		t.BookLatMs = max(t.BookLatMs, upBook.Latency)
	}
	if downBook != nil {
		t.BookLatMs = max(t.BookLatMs, downBook.Latency)
	}
	return t
}

// parseMarketTokens 从 gamma 市场 JSON 解析 UP/DOWN token ID。
//
// outcomes 约定 [0]=Up [1]=Down（与 Python 侧一致），clobTokenIds 与之对齐；
// 支持 "Up"/"Yes"、"Down"/"No" 两种 outcome 命名。未找到时返回空串。
func parseMarketTokens(data *gjson.Result) (upTokenID, downTokenID string) {
	clobRaw := data.Get("clobTokenIds").String()
	var tokenIDs []string
	for _, v := range gjson.Parse(clobRaw).Array() {
		tokenIDs = append(tokenIDs, v.String())
	}

	outcomesRaw := data.Get("outcomes").String()
	var outcomes []string
	for _, v := range gjson.Parse(outcomesRaw).Array() {
		outcomes = append(outcomes, v.String())
	}

	for i, oc := range outcomes {
		if i >= len(tokenIDs) {
			break
		}
		switch oc {
		case "Up", "Yes":
			upTokenID = tokenIDs[i]
		case "Down", "No":
			downTokenID = tokenIDs[i]
		}
	}
	return upTokenID, downTokenID
}
