package main

import (
	"testing"

	"github.com/tidwall/gjson"
	"github.com/xiangxn/go-polymarket-sdk/orders"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// book 构造测试订单簿：bids 升序（最优在末尾）、asks 降序（0.99 填充单在前，
// 真实最优在末尾）——镜像 CLOB WS 真实排列。
func book(bids, asks []float64, lat int64) *sdk.OrderBook {
	b := &sdk.OrderBook{Latency: lat}
	for _, p := range bids {
		b.Bids = append(b.Bids, orders.Book{Price: p, Size: 1})
	}
	for _, p := range asks {
		b.Asks = append(b.Asks, orders.Book{Price: p, Size: 1})
	}
	return b
}

// TestBestPrices 最优价取自切片末尾（asks 降序陷阱回归，2026-08-18 审计）。
func TestBestPrices(t *testing.T) {
	// bids 升序: 0.10 → 0.30 → 0.55, 最优 0.55
	// asks 降序: 0.99(填充) → 0.75 → 0.65, 最优 0.65（若误取 Asks[0] 恒得 0.99）
	ub := book([]float64{0.10, 0.30, 0.55}, []float64{0.99, 0.75, 0.65}, 0)
	if got := bestBid(ub); got != 0.55 {
		t.Fatalf("bestBid = %v, 期望 0.55", got)
	}
	if got := bestAsk(ub); got != 0.65 {
		t.Fatalf("bestAsk = %v, 期望 0.65（asks 降序最优在末尾）", got)
	}

	nilBook := (*sdk.OrderBook)(nil)
	if got := bestBid(nilBook); got != 0 {
		t.Fatalf("nil book bestBid = %v, 期望 0", got)
	}
	if got := bestAsk(&sdk.OrderBook{}); got != 0 {
		t.Fatalf("空盘口 bestAsk = %v, 期望 0", got)
	}
}

// TestMakePMTick 四价 + 延迟取两簿较大者。
func TestMakePMTick(t *testing.T) {
	up := book([]float64{0.2, 0.7, 0.9}, []float64{0.99, 0.6, 0.5}, 12)
	down := book([]float64{0.05, 0.08}, []float64{0.99, 0.9, 0.88}, 25)

	pm := makePMTick(up, down)
	if pm.UpBid != 0.9 || pm.UpAsk != 0.5 || pm.DownBid != 0.08 || pm.DownAsk != 0.88 {
		t.Fatalf("四价 = %v/%v/%v/%v", pm.UpBid, pm.UpAsk, pm.DownBid, pm.DownAsk)
	}
	if pm.BookLatMs != 25 {
		t.Fatalf("BookLatMs = %d, 期望 25（取较大者）", pm.BookLatMs)
	}

	// down 簿缺失: 对应价格 0、延迟只计 up
	pm2 := makePMTick(up, nil)
	if pm2.DownBid != 0 || pm2.DownAsk != 0 || pm2.BookLatMs != 12 {
		t.Fatalf("单簿缺失: %+v", pm2)
	}
}

// TestParseMarketTokens Up/Yes 与 Down/No 两种命名。
func TestParseMarketTokens(t *testing.T) {
	for _, oc := range []string{"Up", "Yes"} {
		for _, dn := range []string{"Down", "No"} {
			j := `{"clobTokenIds":"[\"1111\",\"2222\"]","outcomes":"[\"` + oc + `\",\"` + dn + `\"]"}`
			res := gjson.Parse(j)
			up, down := parseMarketTokens(&res)
			if up != "1111" || down != "2222" {
				t.Fatalf("%s/%s: up/down = %s/%s, 期望 1111/2222", oc, dn, up, down)
			}
		}
	}

	// 长度不匹配时缺侧返回空串（调用方据此跳过窗口）
	j := `{"clobTokenIds":"[\"1111\"]","outcomes":"[\"Up\",\"Down\"]"}`
	res := gjson.Parse(j)
	up, down := parseMarketTokens(&res)
	if up != "1111" || down != "" {
		t.Fatalf("缺 token 时 up/down = %s/%s, 期望 1111/空", up, down)
	}
}
