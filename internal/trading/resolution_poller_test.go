package trading

import (
	"errors"
	"testing"
	"time"

	"github.com/tidwall/gjson"
)

// mkMarket 构造 gamma 市场响应（outcomePrices 支持浮点/字符串两种序列化）。
func mkMarket(status string, p0, p1 string) *gjson.Result {
	r := gjson.Parse(`{
		"umaResolutionStatus": "` + status + `",
		"outcomePrices": ["` + p0 + `", "` + p1 + `"]
	}`)
	return &r
}

// newTestPoller 构造 poller，fetch 走 fake 闭包，回调记录结果。
func newTestPoller(t *testing.T) (*ResolutionPoller, *func(string) (*gjson.Result, error), *func(string, int) error) {
	t.Helper()
	var fetch func(string) (*gjson.Result, error)
	var onResolved func(string, int) error
	rp := NewResolutionPoller(
		func(slug string) (*gjson.Result, error) { return fetch(slug) },
		10*time.Second,
		func(conditionID string, outcome int) error {
			if onResolved != nil {
				return onResolved(conditionID, outcome)
			}
			return nil
		},
	)
	return rp, &fetch, &onResolved
}

// TestPoller_NotResolvedKeepsPending 验证未结算市场保持 pending。
func TestPoller_NotResolvedKeepsPending(t *testing.T) {
	rp, fetch, _ := newTestPoller(t)
	*fetch = func(slug string) (*gjson.Result, error) {
		return mkMarket("pending", "0.60", "0.40"), nil
	}
	rp.Register("condA", "btc-updown-5m-1")
	rp.pollAll()
	if rp.PendingCount() != 1 {
		t.Fatalf("未结算应保持 pending, got %d", rp.PendingCount())
	}
}

// TestPoller_ResolvedIntOutcome 验证 resolved + "1"/"0" 判定 outcome=0（UP 胜）。
func TestPoller_ResolvedIntOutcome(t *testing.T) {
	rp, fetch, onResolved := newTestPoller(t)
	*fetch = func(slug string) (*gjson.Result, error) {
		return mkMarket("resolved", "1", "0"), nil
	}
	var gotCID, gotOutcome string
	*onResolved = func(cid string, outcome int) error {
		gotCID, gotOutcome = cid, string(rune('0'+outcome))
		return nil
	}
	rp.Register("condB", "btc-updown-5m-2")
	rp.pollAll()
	if gotCID != "condB" || gotOutcome != "0" {
		t.Fatalf("回调参数错误: cid=%s outcome=%s, want condB/0", gotCID, gotOutcome)
	}
	if rp.PendingCount() != 0 {
		t.Fatalf("结算后应移除 pending, got %d", rp.PendingCount())
	}
}

// TestPoller_ResolvedFloatNormalized 验证 outcomePrices 浮点序列化归一化:
// "0.999…"/"0.0" → outcome 0（胜方 = 价格 > 0.5 的一侧）。
func TestPoller_ResolvedFloatNormalized(t *testing.T) {
	rp, fetch, onResolved := newTestPoller(t)
	*fetch = func(slug string) (*gjson.Result, error) {
		return mkMarket("resolved", "0.999999", "0.000001"), nil
	}
	var gotOutcome int
	*onResolved = func(cid string, outcome int) error {
		gotOutcome = outcome
		return nil
	}
	rp.Register("condC", "btc-updown-5m-3")
	rp.pollAll()
	if gotOutcome != 0 {
		t.Fatalf("outcome = %d, want 0（归一化后胜方 UP）", gotOutcome)
	}

	// 反向: 胜方为 NO → outcome 1
	rp = NewResolutionPoller(
		func(slug string) (*gjson.Result, error) {
			return mkMarket("resolved", "0.000001", "0.999999"), nil
		}, 10*time.Second, func(cid string, outcome int) error {
			gotOutcome = outcome
			return nil
		})
	rp.Register("condD", "btc-updown-5m-4")
	rp.pollAll()
	if gotOutcome != 1 {
		t.Fatalf("outcome = %d, want 1（胜方 DOWN）", gotOutcome)
	}
}

// TestPoller_ResolveTieStaysPending 验证 0.5/0.5 平局（争议/无效）保持等待。
func TestPoller_ResolveTieStaysPending(t *testing.T) {
	rp, fetch, _ := newTestPoller(t)
	*fetch = func(slug string) (*gjson.Result, error) {
		return mkMarket("resolved", "0.5", "0.5"), nil
	}
	rp.Register("condE", "btc-updown-5m-5")
	rp.pollAll()
	if rp.PendingCount() != 1 {
		t.Fatalf("平局应保持 pending, got %d", rp.PendingCount())
	}
}

// TestPoller_CallbackErrorKeepsPending 验证回调失败保持 pending 下次轮询重试。
func TestPoller_CallbackErrorKeepsPending(t *testing.T) {
	rp, fetch, onResolved := newTestPoller(t)
	*fetch = func(slug string) (*gjson.Result, error) {
		return mkMarket("resolved", "1", "0"), nil
	}
	calls := 0
	*onResolved = func(cid string, outcome int) error {
		calls++
		if calls == 1 {
			return errors.New("落盘失败")
		}
		return nil
	}
	rp.Register("condF", "btc-updown-5m-6")
	rp.pollAll()
	if rp.PendingCount() != 1 {
		t.Fatalf("回调失败应保持 pending, got %d", rp.PendingCount())
	}
	rp.pollAll()
	if rp.PendingCount() != 0 || calls != 2 {
		t.Fatalf("重试后应移除: pending=%d calls=%d", rp.PendingCount(), calls)
	}
}

// TestPoller_FetchErrorKeepsPending 验证查询失败保持 pending。
func TestPoller_FetchErrorKeepsPending(t *testing.T) {
	rp, fetch, _ := newTestPoller(t)
	*fetch = func(slug string) (*gjson.Result, error) {
		return nil, errors.New("网络错误")
	}
	rp.Register("condG", "btc-updown-5m-7")
	rp.pollAll()
	if rp.PendingCount() != 1 {
		t.Fatalf("查询失败应保持 pending, got %d", rp.PendingCount())
	}
}

// TestPoller_AgingRemovesPending 验证超过 maxPendingWait 未结算的市场被移除。
func TestPoller_AgingRemovesPending(t *testing.T) {
	rp, fetch, _ := newTestPoller(t)
	*fetch = func(slug string) (*gjson.Result, error) {
		return mkMarket("pending", "0.60", "0.40"), nil
	}
	rp.Register("condH", "btc-updown-5m-8")
	rp.mu.Lock()
	rp.pending["condH"].RegisteredAt = time.Now().Add(-25 * time.Hour) // 模拟 25 小时前注册
	rp.mu.Unlock()
	rp.pollAll()
	if rp.PendingCount() != 0 {
		t.Fatalf("超龄应移除, got %d", rp.PendingCount())
	}
}

// TestPoller_DuplicateRegister 验证同一 conditionID 重复注册被忽略。
func TestPoller_DuplicateRegister(t *testing.T) {
	rp, _, _ := newTestPoller(t)
	rp.Register("condI", "btc-updown-5m-9")
	rp.Register("condI", "btc-updown-5m-9")
	if rp.PendingCount() != 1 {
		t.Fatalf("重复注册后 pending = %d, want 1", rp.PendingCount())
	}
}

// TestPoller_EmptyPoll 验证无 pending 时 pollAll 不崩溃。
func TestPoller_EmptyPoll(t *testing.T) {
	rp, _, _ := newTestPoller(t)
	rp.pollAll() // 不 panic 即通过
}
