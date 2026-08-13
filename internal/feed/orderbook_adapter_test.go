package feed

import (
	"slices"
	"testing"
)

// TestAddTokens 验证订阅列表追加去重（含重复追加、空输入）。
func TestAddTokens(t *testing.T) {
	cases := []struct {
		name   string
		list   []string
		add    []string
		expect []string
	}{
		{"追加到空列表", nil, []string{"a", "b"}, []string{"a", "b"}},
		{"去重", []string{"a"}, []string{"a", "b", "a"}, []string{"a", "b"}},
		{"空追加", []string{"a", "b"}, nil, []string{"a", "b"}},
		{"全空", nil, nil, nil},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := addTokens(c.list, c.add...)
			if !sameSet(got, c.expect) {
				t.Fatalf("addTokens(%v, %v) = %v, want %v", c.list, c.add, got, c.expect)
			}
		})
	}
}

// TestRemoveTokens 验证订阅列表移除（含部分移除、移除不存在项、移除全部）。
func TestRemoveTokens(t *testing.T) {
	cases := []struct {
		name   string
		list   []string
		remove []string
		expect []string
	}{
		{"部分移除", []string{"a", "b"}, []string{"a"}, []string{"b"}},
		{"移除不存在项", []string{"a"}, []string{"x"}, []string{"a"}},
		{"移除全部", []string{"a", "b"}, []string{"a", "b"}, nil},
		{"空移除", []string{"a"}, nil, []string{"a"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := removeTokens(c.list, c.remove...)
			if !sameSet(got, c.expect) {
				t.Fatalf("removeTokens(%v, %v) = %v, want %v", c.list, c.remove, got, c.expect)
			}
		})
	}
}

// TestTokenTracking_SubscribeUnsubscribe 验证 adapter 订阅副本的增删语义。
func TestTokenTracking_SubscribeUnsubscribe(t *testing.T) {
	o := NewOrderBookAdapter("", nil)
	o.SubscribeTokens("yes", "no")
	o.SubscribeTokens("yes") // 重复订阅应去重
	if !sameSet(o.tokens, []string{"yes", "no"}) {
		t.Fatalf("tokens after subscribe = %v, want [yes no]", o.tokens)
	}
	o.UnsubscribeTokens("yes")
	if !sameSet(o.tokens, []string{"no"}) {
		t.Fatalf("tokens after unsubscribe = %v, want [no]", o.tokens)
	}
}

// sameSet 无序比较两个字符串切片是否相等。
func sameSet(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	aa := slices.Clone(a)
	bb := slices.Clone(b)
	slices.Sort(aa)
	slices.Sort(bb)
	return slices.Equal(aa, bb)
}
