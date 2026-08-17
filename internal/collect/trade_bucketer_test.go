package collect

import "testing"

// TestTradeBucketer_HashDedupe 验证 WS 重连重放的重复笔按 transaction_hash
// 去重只计一次；无 hash 的笔不参与去重（兼容测试/降级路径）。
func TestTradeBucketer_HashDedupe(t *testing.T) {
	b := NewTradeBucketer(1000)

	b.Add("YES", 1500, "BUY", 0.5, 10, 0.49, 0.51, "hash-1")
	b.Add("YES", 1500, "BUY", 0.5, 10, 0.49, 0.51, "hash-1") // 重放，应丢弃
	b.Add("YES", 1500, "BUY", 0.5, 5, 0.49, 0.51, "hash-2")
	b.Add("YES", 1500, "BUY", 0.5, 5, 0.49, 0.51, "") // 无 hash，不去重

	aggs := b.Snapshot()
	if len(aggs) != 1 {
		t.Fatalf("期望 1 个聚合行，实际 %d", len(aggs))
	}
	if aggs[0].NBuy != 3 || aggs[0].BuySize != 20 {
		t.Fatalf("去重后应为 3 笔/20 股，实际 %d 笔/%.1f 股", aggs[0].NBuy, aggs[0].BuySize)
	}
}

// TestTradeBucketer_WindowBounds 窗口边界外的笔（含远端时戳偏移）直接丢弃。
func TestTradeBucketer_WindowBounds(t *testing.T) {
	b := NewTradeBucketer(1000)

	b.Add("YES", 999, "BUY", 0.5, 1, 0, 0, "h0")            // 起点前 1ms → 丢
	b.Add("YES", 1000, "BUY", 0.5, 1, 0, 0, "h1")            // 起点 → idx 0
	b.Add("YES", 1999, "SELL", 0.5, 2, 0, 0, "h2")           // 同秒桶末尾 → idx 0
	b.Add("YES", 1000+WindowSec*1000, "SELL", 0.5, 4, 0, 0, "h3") // 终点 → 丢

	aggs := b.Snapshot()
	if len(aggs) != 1 {
		t.Fatalf("期望 1 个聚合行，实际 %d", len(aggs))
	}
	if aggs[0].NBuy != 1 || aggs[0].NSell != 1 || aggs[0].BuySize != 1 || aggs[0].SellSize != 2 {
		t.Fatalf("聚合内容不符: %+v", aggs[0])
	}
}
