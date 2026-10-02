package trading

import (
	"errors"
	"strings"
	"testing"

	"github.com/xiangxn/go-polymarket-sdk/orders"

	"github.com/necklace/flip-signal/internal/flip"
)

// ── parseSellFill（FAK 卖单响应解析——字段语义与 BUY 相反, 本实现唯一需真盘验证点）──

func TestParseSellFill(t *testing.T) {
	req := 2.1739 // 2U @0.92 的目标股数
	limit := 0.26

	cases := []struct {
		name    string
		raw     string
		wantSt  string
		wantSh  float64
		wantPx  float64
		unknown bool // 期望 Note 以 ExecNoteUnknown 开头
	}{
		{
			// 正常形态: makingAmount = 成交股数, takingAmount = 收到 USDC
			name:   "全额成交（股数/USDC 两字段同单位）",
			raw:    `{"success":true,"orderID":"0xok","status":"matched","makingAmount":2.1739,"takingAmount":0.5652}`,
			wantSt: flip.ExecStatusFilled, wantSh: 2.1739, wantPx: 0.5652 / 2.1739,
		},
		{
			name:   "1e6 基单位（两字段一起折算）",
			raw:    `{"success":true,"orderID":"0xok","status":"matched","makingAmount":2173900,"takingAmount":565214}`,
			wantSt: flip.ExecStatusFilled, wantSh: 2.1739, wantPx: 0.565214 / 2.1739,
		},
		{
			// 部分成交: 价 0.27 ≥ 限价 0.26（卖价只会更优）。
			name:   "部分成交",
			raw:    `{"success":true,"orderID":"0xpart","status":"matched","makingAmount":1.0,"takingAmount":0.27}`,
			wantSt: flip.ExecStatusPartial, wantSh: 1.0, wantPx: 0.27,
		},
		{
			// 成交价低于限价 ⇒ 撮合不可能这么给（限价是卖方**最低**可接受价）⇒ 未知。
			name: "成交价劣于限价 ⇒ 未知", raw: `{"success":true,"orderID":"0xbad","makingAmount":2.0,"takingAmount":0.40}`,
			wantSt: flip.ExecStatusRejected, unknown: true,
		},
		{
			// 两字段记反（making 给美元、taking 给股数）: cash/shares ≈ 3.8 > 1 ⇒ 上界兜住。
			name: "语义颠倒 ⇒ 未知", raw: `{"success":true,"orderID":"0xrev","makingAmount":0.5652,"takingAmount":2.1739}`,
			wantSt: flip.ExecStatusRejected, unknown: true,
		},
		{
			// 成交股数超过下单量（折基单位后仍超: 3e9/1e6 = 3000 股 > 2.17）⇒ 未知。
			name: "成交超下单量 ⇒ 未知", raw: `{"success":true,"orderID":"0xover","makingAmount":3000000000,"takingAmount":780000000}`,
			wantSt: flip.ExecStatusRejected, unknown: true,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			res := parseSellFill(parseJSON(c.raw), "0xid", req, limit)
			if res.Status != c.wantSt {
				t.Fatalf("status = %q, 期望 %q（note=%q）", res.Status, c.wantSt, res.Note)
			}
			if c.unknown != strings.HasPrefix(res.Note, flip.ExecNoteUnknown) {
				t.Fatalf("unknown 标记不符: note=%q", res.Note)
			}
			if c.wantSt != flip.ExecStatusRejected {
				if !near(res.Shares, c.wantSh) || !near(res.Price, c.wantPx) {
					t.Fatalf("成交解析 = %.6f 股 @%.6f, 期望 %.6f @%.6f", res.Shares, res.Price, c.wantSh, c.wantPx)
				}
			}
		})
	}
}

// TestParseSellFillZeroAndMissing 0 成交（FAK 被立即撤销）按**未成交**交给调用方重试;
// 字段缺失（schema 与假设不符）⇒ 未知（绝不当作未成交——可能已成交）。
func TestParseSellFillZeroAndMissing(t *testing.T) {
	zero := parseSellFill(parseJSON(`{"success":true,"orderID":"0xz","status":"canceled","makingAmount":0,"takingAmount":0}`), "0xz", 2.17, 0.26)
	if zero.Status != flip.ExecStatusUnfilled {
		t.Fatalf("0 成交应为 unfilled（可重试）, 得到 %q", zero.Status)
	}
	missing := parseSellFill(parseJSON(`{"success":true,"orderID":"0xm","status":"matched"}`), "0xm", 2.17, 0.26)
	if missing.Status != flip.ExecStatusRejected || !strings.HasPrefix(missing.Note, flip.ExecNoteUnknown) {
		t.Fatalf("字段缺失应为未知, 得到 %q note=%q", missing.Status, missing.Note)
	}
}

// ── Sell 端到端（fakeClient 脚本化 POST）──

func newSellClient(resp string, err error) *fakeClient {
	f := &fakeClient{postErr: err}
	if resp != "" {
		f.postResp = parseJSON(resp)
	}
	return f
}

// TestLiveSellEndToEnd 卖出路径的签名/提交参数与终态分类: SELL + FAK、成交解析、
// 各种失败形态映射到「重试（unfilled）」或「冻结（rejected + 未知）」。
func TestLiveSellEndToEnd(t *testing.T) {
	t.Run("成交", func(t *testing.T) {
		f := newSellClient(`{"success":true,"orderID":"0xa","status":"matched","makingAmount":2.17,"takingAmount":0.5642}`, nil)
		res := NewLiveSellExecutor(f).Sell("tok", 2.17, 0.26)
		if res.Status != flip.ExecStatusFilled || !near(res.Shares, 2.17) || !near(res.Price, 0.5642/2.17) {
			t.Fatalf("成交解析不对: %+v", res)
		}
		if f.got == nil || f.got.Side != orders.SELL || f.got.TokenID != "tok" {
			t.Fatalf("CreateOrder 应收到 SELL 单: %+v", f.got)
		}
		if f.orderType != orders.FAK {
			t.Fatalf("应提交 FAK（余量即撤, 单次 POST 即终态）: %v", f.orderType)
		}
	})

	t.Run("FAK 无对价走 4xx ⇒ 未成交可重试", func(t *testing.T) {
		f := newSellClient("", errors.New(`API request failed with status 400: {"error":"no orders found to match with FAK order"}`))
		res := NewLiveSellExecutor(f).Sell("tok", 2.17, 0.26)
		if res.Status != flip.ExecStatusUnfilled {
			t.Fatalf("无对价应映射为 unfilled（重试）, 得到 %q note=%q", res.Status, res.Note)
		}
		if strings.HasPrefix(res.Note, flip.ExecNoteUnknown) {
			t.Fatalf("无对价是**已知**形态, 不该标未知: %q", res.Note)
		}
	})

	t.Run("success=false + 无对价文案 ⇒ 未成交", func(t *testing.T) {
		f := newSellClient(`{"success":false,"orderID":"","errorMsg":"no orders found to match with FAK order"}`, nil)
		if res := NewLiveSellExecutor(f).Sell("tok", 2.17, 0.26); res.Status != flip.ExecStatusUnfilled {
			t.Fatalf("应映射为 unfilled, 得到 %q note=%q", res.Status, res.Note)
		}
	})

	t.Run("429 限流 ⇒ 未成交可重试（订单确定未受理）", func(t *testing.T) {
		f := newSellClient("", errors.New("API request failed with status 429: rate limit"))
		if res := NewLiveSellExecutor(f).Sell("tok", 2.17, 0.26); res.Status != flip.ExecStatusUnfilled {
			t.Fatalf("429 应按可重试处理, 得到 %q note=%q", res.Status, res.Note)
		}
	})

	t.Run("传输错误 ⇒ 未知（冻结, 绝不重试）", func(t *testing.T) {
		f := newSellClient("", errors.New("context deadline exceeded"))
		res := NewLiveSellExecutor(f).Sell("tok", 2.17, 0.26)
		if res.Status != flip.ExecStatusRejected || !strings.HasPrefix(res.Note, flip.ExecNoteUnknown) {
			t.Fatalf("传输错误应为未知: %+v", res)
		}
	})

	t.Run("其它 4xx ⇒ 已知拒单（冻结, 非未知）", func(t *testing.T) {
		f := newSellClient("", errors.New(`API request failed with status 400: {"error":"not enough balance / allowance"}`))
		res := NewLiveSellExecutor(f).Sell("tok", 2.17, 0.26)
		if res.Status != flip.ExecStatusRejected || strings.HasPrefix(res.Note, flip.ExecNoteUnknown) {
			t.Fatalf("已知拒单不该标未知: %+v", res)
		}
	})

	t.Run("参数非法/空 token ⇒ 拒单（不触网）", func(t *testing.T) {
		f := newSellClient("", nil)
		for _, c := range []struct {
			tok   string
			sh    float64
			limit float64
		}{{"tok", 0, 0.26}, {"tok", 1, 0}, {"", 1, 0.26}, {"tok", 1, 1.2}} {
			if res := NewLiveSellExecutor(f).Sell(c.tok, c.sh, c.limit); res.Status != flip.ExecStatusRejected {
				t.Fatalf("非法入参应拒单: %+v", res)
			}
		}
		if f.got != nil {
			t.Fatal("非法入参不该触网")
		}
	})

	t.Run("CreateOrder 失败 ⇒ 拒单", func(t *testing.T) {
		f := &fakeClient{createErr: errors.New("签名失败")}
		if res := NewLiveSellExecutor(f).Sell("tok", 2.17, 0.26); res.Status != flip.ExecStatusRejected {
			t.Fatalf("应拒单: %+v", res)
		}
	})
}

