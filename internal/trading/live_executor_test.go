package trading

import (
	"errors"
	"strings"
	"testing"

	"github.com/tidwall/gjson"
	"github.com/xiangxn/go-polymarket-sdk/orders"

	"github.com/necklace/flip-signal/internal/flip"
)

// fakeClient 脚本化 TradeClient（CreateOrder/PostOrder/GetOpenOrders/CancelOrder
// 结果由测试预设）。
type fakeClient struct {
	createErr error
	postResp  *gjson.Result
	postErr   error
	orderType orders.OrderType
	got       *orders.UserOrder
	ooResp    []orders.OpenOrder // GetOpenOrders 返回值（空 = 挂单表已无此单）
	ooErr     error
	ooCalls   int
	cancelErr error
	cancelled []string // 撤单成功收到的 orderID（按序）
	cnlCalls  int
}

func (f *fakeClient) CreateOrder(u *orders.UserOrder, o orders.CreateOrderOptions) (*orders.SignedOrder, error) {
	if f.createErr != nil {
		return nil, f.createErr
	}
	f.got = u
	return &orders.SignedOrder{}, nil
}

func (f *fakeClient) PostOrder(order *orders.SignedOrder, t orders.OrderType, deferExec bool) (*gjson.Result, error) {
	f.orderType = t
	if f.postErr != nil {
		return nil, f.postErr
	}
	return f.postResp, nil
}

func (f *fakeClient) GetOpenOrders(params *orders.OpenOrderParams, onlyFirstPage bool, nextCursor *string) ([]orders.OpenOrder, error) {
	f.ooCalls++
	return f.ooResp, f.ooErr
}

func (f *fakeClient) CancelOrder(payload *orders.OrderPayload) (*gjson.Result, error) {
	f.cnlCalls++
	if f.cancelErr != nil {
		return nil, f.cancelErr
	}
	f.cancelled = append(f.cancelled, payload.OrderID)
	return &gjson.Result{}, nil
}

func mkExecObs(fill float64) *flip.Observation {
	return &flip.Observation{Ts: 1_780_000_000_000, Side: flip.SideYes, Fill: fill, OK: true, Shares: 2 / fill}
}

// parseJSON 把原始 JSON 解析为 *gjson.Result（与 SDK PostOrder 返回同型）。
func parseJSON(s string) *gjson.Result {
	r := gjson.Parse(s)
	return &r
}

// ── parseFill（POST 响应即时成交解析, 本实现唯一需真盘验证点）──

func TestParseFill(t *testing.T) {
	req := 10.52 // floor2(2/0.19)
	price := 0.19

	cases := []struct {
		name     string
		raw      string
		orderID  string
		wantSt   string
		wantSh   float64
		wantCost float64
		unknown  bool // ExecNoteUnknown 前缀（人工核对类）
	}{
		{
			name:    "即时全额成交 1e6 基单位 → filled 终态",
			raw:     `{"success":true,"orderID":"o1","status":"matched","takingAmount":"10520000","makingAmount":"1998800"}`,
			orderID: "o1", wantSt: flip.ExecStatusFilled, wantSh: 10.52, wantCost: 1.9988,
		},
		{
			name:    "即时部分成交 原始小数 → resting（余量在簿, 不是终态）",
			raw:     `{"success":true,"orderID":"o2","status":"matched","takingAmount":"5","makingAmount":"0.95"}`,
			orderID: "o2", wantSt: flip.ExecStatusResting,
		},
		{
			name:    "即时部分成交 1e6 基单位 → resting（旧刻度法在此误判人工核对）",
			raw:     `{"success":true,"orderID":"o7","status":"live","takingAmount":"5000000","makingAmount":"950000"}`,
			orderID: "o7", wantSt: flip.ExecStatusResting,
		},
		{
			name:    "零成交 unmatched → resting（GTC 吃不到就挂着等, 非 FAK 的即撤）",
			raw:     `{"success":true,"orderID":"o3","status":"unmatched","takingAmount":"0","makingAmount":"0"}`,
			orderID: "o3", wantSt: flip.ExecStatusResting,
		},
		{
			name:    "纯挂单 live 无成交字段 → resting",
			raw:     `{"success":true,"orderID":"o8","status":"live"}`,
			orderID: "o8", wantSt: flip.ExecStatusResting,
		},
		{
			name:    "两字段语义记反 → sanity 拒收转人工",
			raw:     `{"success":true,"orderID":"o4","status":"matched","takingAmount":"1.9988","makingAmount":"10.52"}`,
			orderID: "o4", wantSt: flip.ExecStatusRejected, unknown: true,
		},
		{
			name:    "成交超请求 → sanity 拒收",
			raw:     `{"success":true,"orderID":"o5","status":"matched","takingAmount":"12000000","makingAmount":"2280000"}`,
			orderID: "o5", wantSt: flip.ExecStatusRejected, unknown: true,
		},
		{
			name:    "金额缺失但 status 不认识 → 拒收转人工（可能成交, 不按 0 记）",
			raw:     `{"success":true,"orderID":"o6","status":"matched"}`,
			orderID: "o6", wantSt: flip.ExecStatusRejected, unknown: true,
		},
		{
			name:   "挂单但无 orderID → 无法跟踪, 转人工",
			raw:    `{"success":true,"status":"live"}`,
			wantSt: flip.ExecStatusRejected, unknown: true,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			res := parseFill(parseJSON(c.raw), c.orderID, req, price)
			if res.Status != c.wantSt {
				t.Fatalf("status = %q, 期望 %q（note=%s）", res.Status, c.wantSt, res.Note)
			}
			if c.unknown && !strings.HasPrefix(res.Note, flip.ExecNoteUnknown) {
				t.Fatalf("应标记人工核对, note=%q", res.Note)
			}
			if res.Shares != c.wantSh || res.Cost != c.wantCost {
				t.Fatalf("shares/cost = %v/%v, 期望 %v/%v", res.Shares, res.Cost, c.wantSh, c.wantCost)
			}
			switch res.Status {
			case flip.ExecStatusFilled, flip.ExecStatusPartial:
				if avg := res.Cost / res.Shares; avg > price+0.005 || avg < price-0.005 {
					t.Fatalf("成交均价 %.4f 偏离限价 %.3f", avg, price)
				}
				if res.FillPrice == 0 || res.OrderID == "" {
					t.Fatalf("成交行缺 FillPrice/OrderID: %+v", res)
				}
			case flip.ExecStatusResting:
				// resting 必须带 order_id: FillTracker 靠它查终态, 没有就是人工核对（见上一条）
				if res.OrderID == "" {
					t.Fatalf("resting 行缺 OrderID（无法跟踪）: %+v", res)
				}
			}
		})
	}
}

// ── LiveExecutor.Execute 全链路 ──

func TestExecuteFilled(t *testing.T) {
	fc := &fakeClient{postResp: parseJSON(`{"success":true,"orderID":"o-live","status":"matched","takingAmount":"10.52","makingAmount":"1.9988"}`)}
	tr := NewLiveExecutor(fc)
	res := tr.Execute(mkExecObs(0.19), "tok-up", 2)

	if res.Status != flip.ExecStatusFilled || res.OrderID != "o-live" {
		t.Fatalf("Execute: %+v", res)
	}
	if fc.orderType != orders.GTC {
		t.Fatalf("订单类型 = %s, 期望 GTC", fc.orderType)
	}
	if fc.got == nil || fc.got.Price != 0.19 || fc.got.Size != 10.52 || fc.got.Side != orders.BUY || fc.got.TokenID != "tok-up" {
		t.Fatalf("订单规格: %+v", fc.got)
	}
}

func TestExecuteRejections(t *testing.T) {
	// token 空
	fc := &fakeClient{}
	res := NewLiveExecutor(fc).Execute(mkExecObs(0.19), "", 2)
	if res.Status != flip.ExecStatusRejected {
		t.Fatalf("token 空应 rejected: %+v", res)
	}
	// 规格错误（fill 非法）
	res = NewLiveExecutor(fc).Execute(&flip.Observation{Fill: 0}, "tok", 2)
	if res.Status != flip.ExecStatusRejected {
		t.Fatalf("规格错应 rejected: %+v", res)
	}
	// CreateOrder 失败
	res = NewLiveExecutor(&fakeClient{createErr: errors.New("签名失败")}).Execute(mkExecObs(0.19), "tok", 2)
	if res.Status != flip.ExecStatusRejected || strings.HasPrefix(res.Note, flip.ExecNoteUnknown) {
		t.Fatalf("CreateOrder 失败应明确 rejected: %+v", res)
	}
	// CLOB 明确拒单（HTTP 4xx, SDK 返回 error）
	httpErr := errors.New("API request failed with status 400: {\"errorMsg\":\"not enough balance\"}")
	res = NewLiveExecutor(&fakeClient{postErr: httpErr}).Execute(mkExecObs(0.19), "tok", 2)
	if res.Status != flip.ExecStatusRejected || strings.HasPrefix(res.Note, flip.ExecNoteUnknown) {
		t.Fatalf("4xx 应明确 rejected: %+v", res)
	}
	// 传输错误/超时（结果不明）→ ExecNoteUnknown 前缀
	res = NewLiveExecutor(&fakeClient{postErr: errors.New("Post \"https://clob...\": context deadline exceeded")}).Execute(mkExecObs(0.19), "tok", 2)
	if res.Status != flip.ExecStatusRejected || !strings.HasPrefix(res.Note, flip.ExecNoteUnknown) {
		t.Fatalf("超时应标记未知结果: %+v", res)
	}
	// success=false（HTTP <400 的拒单响应）
	fc2 := &fakeClient{postResp: parseJSON(`{"success":false,"errorMsg":"order size too small"}`)}
	res = NewLiveExecutor(fc2).Execute(mkExecObs(0.19), "tok", 2)
	if res.Status != flip.ExecStatusRejected || strings.Contains(res.Note, "未知结果") {
		t.Fatalf("success=false 应明确 rejected: %+v", res)
	}
}

// GTC 下「0 成交」不再是 POST 能给出的结论: 吃不到就挂着等, 只有 FillTracker
// 在 rem≤RemMin 撤单时定稿才知道它是 unfilled（见 fill_tracker_test.go）。
func TestExecuteResting(t *testing.T) {
	fc := &fakeClient{postResp: parseJSON(`{"success":true,"orderID":"o-none","status":"unmatched"}`)}
	res := NewLiveExecutor(fc).Execute(mkExecObs(0.19), "tok", 2)
	if res.Status != flip.ExecStatusResting || res.OrderID != "o-none" {
		t.Fatalf("Execute: %+v", res)
	}
}
