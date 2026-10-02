package tail

import (
	"encoding/json"
	"path/filepath"
	"testing"
	"time"

	"github.com/necklace/flip-signal/internal/flip"
)

// ── 纯函数（stop.go）──

// TestStopTrigger 止损触发三条口径: 严格小于、bid==0 不是触发（整侧撤空=卖不掉,
// 触发只会产出注定被拒的卖单）、总开关。边界值 0.30 恰好**不触发**。
func TestStopTrigger(t *testing.T) {
	cfg := DefaultConfig()
	cases := []struct {
		name string
		bid  float64
		cfg  Config
		want bool
	}{
		{"典型触发 0.29", 0.29, cfg, true},
		{"深跌 0.05", 0.05, cfg, true},
		{"边界 0.30 不触发（严格小于）", 0.30, cfg, false},
		{"安全区 0.31", 0.31, cfg, false},
		{"bid==0 不是触发（无人接=卖不掉）", 0, cfg, false},
		{"负值（异常读数）", -1, cfg, false},
		{"开关关闭 ⇒ 恒不触发", 0.01, func() Config { c := cfg; c.StopLossEnabled = false; return c }(), false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := StopTrigger(c.cfg, c.bid); got != c.want {
				t.Fatalf("StopTrigger(bid=%.3f) = %v, 期望 %v", c.bid, got, c.want)
			}
		})
	}
}

// TestStopArmed 止损门控: 只认 rem>0 与盘口延迟; **不要求 spot/anchor**
// （纯价格腿, 与判定路径的半有效 tick 口径的唯一差异——有意为之）。
func TestStopArmed(t *testing.T) {
	mk := func(rem int, lat int64, spot float64) flip.Tick {
		return flip.Tick{Rem: rem, BookLatMs: lat, BinPrice: spot}
	}
	if !StopArmed(300, mk(40, 300, 0)) {
		t.Fatal("缺 spot 也应 armed（止损不看结算线）")
	}
	if !StopArmed(300, mk(40, 300, 100000)) {
		t.Fatal("常规 tick 应 armed")
	}
	if StopArmed(300, mk(0, 50, 100000)) {
		t.Fatal("rem==0（闭市）应不 armed")
	}
	if StopArmed(300, mk(40, 301, 100000)) {
		t.Fatal("盘口延迟超闸应不 armed")
	}
}

// TestHoldBidOf 持仓侧买价取值: 押 yes 看 UpBid / 押 no 看 DownBid。
func TestHoldBidOf(t *testing.T) {
	tk := flip.Tick{UpBid: 0.11, DownBid: 0.88}
	if got := HoldBidOf(flip.SideYes, tk); got != 0.11 {
		t.Fatalf("yes 侧应取 UpBid, 得到 %.3f", got)
	}
	if got := HoldBidOf(flip.SideNo, tk); got != 0.88 {
		t.Fatalf("no 侧应取 DownBid, 得到 %.3f", got)
	}
}

// ── 编排（ExecState.StopSell）──

// fakeSell 记录卖出调用并返回预设结果（默认: 全额成交 @ 调用时的限价）。
type fakeSell struct {
	calls  int
	token  string
	shares float64
	limit  float64
	res    flip.SellResult
	onCall func()
}

func (f *fakeSell) Sell(tokenID string, shares, limit float64) *flip.SellResult {
	f.calls++
	f.token, f.shares, f.limit = tokenID, shares, limit
	if f.onCall != nil {
		f.onCall()
	}
	r := f.res
	if r.Status == "" {
		r = flip.SellResult{Status: flip.ExecStatusFilled, Shares: shares, Price: limit}
	}
	return &r
}

// newStopFixture 造一个「纸面已成交的持仓 + 卖出执行器」的编排现场。
func newStopFixture(t *testing.T, sell *fakeSell) (*ExecState, *Recorder, string, *Record) {
	t.Helper()
	x, rec, dir := newTestExec(t, false, &fakeExec{})
	x.Sell = sell
	r := x.HandleDecision(okSnap(1780000255000, flip.SideYes), "0xc", "slug", 1780000000)
	if r == nil || !r.HasPosition() {
		t.Fatalf("夹具应有已成交的纸面持仓: %+v", r)
	}
	return x, rec, dir, r
}

// TestStopSellFilled 全额卖出: 成交落盘 + 结算按实际落袋算（赢/输都不含剩余兑付）。
func TestStopSellFilled(t *testing.T) {
	fs := &fakeSell{}
	x, rec, dir, r := newStopFixture(t, fs)
	shares := r.Shares // 2/0.92

	if out := x.StopSell(r, 0.26, 40, 1780000270000); out != StopSold {
		t.Fatalf("全额成交应为 StopSold, 得到 %v", out)
	}
	if fs.calls != 1 || fs.shares != shares || fs.limit != 0.26 {
		t.Fatalf("卖出调用应为 %.4f 股 @0.26, 得到 %.4f @%.3f（%d 次）", shares, fs.shares, fs.limit, fs.calls)
	}
	if fs.token != "" {
		t.Fatalf("纸面不该传 token: %q", fs.token)
	}
	if !r.IsStopped() || !near(r.ExitShares, shares) || r.ExitPrice != 0.26 || r.ExitRem != 40 {
		t.Fatalf("行内卖出字段不对: exit=%.4f @%.3f rem=%d", r.ExitShares, r.ExitPrice, r.ExitRem)
	}
	if r.RemainingShares() != 0 {
		t.Fatalf("全卖后剩余应为 0, 得到 %.4f", r.RemainingShares())
	}

	// 结算回填 won 后: 全卖 ⇒ 只剩落袋（不兑付剩余）。
	if !rec.Resolve("0xc", flip.OutcomeUp, time.Unix(1780000300, 0), "push") {
		t.Fatal("Resolve 应命中")
	}
	want := shares*0.26 - 2 // 卖出所得 − cost（paper 用 Stake 兜底）
	if !near(r.PnL, want) {
		t.Fatalf("P&L 应为 %.4f, 得到 %.4f", want, r.PnL)
	}

	// 磁盘真相: 卖出字段落盘（exit_shares/exit_price/exit_rem）。
	lines := readLines(t, filepath.Join(dir, "tail_"+utcDate(1780000255000)+".jsonl"))
	var got Record
	json.Unmarshal([]byte(lines[0]), &got)
	if !near(got.ExitShares, shares) || got.ExitPrice != 0.26 || got.ExitRem != 40 || got.ExitTs == 0 {
		t.Fatalf("卖出字段未落盘: %+v", got)
	}
}

// TestStopSellPartialThenRest 部分成交累计: 两次卖出（不同价）→ 加权均价;
// ExitRem/ExitTs 只记**第一次**（页面显示「止损发生于 rem=x」）。
func TestStopSellPartialThenRest(t *testing.T) {
	fs := &fakeSell{res: flip.SellResult{Status: flip.ExecStatusPartial, Shares: 1.0, Price: 0.25}}
	x, _, _, r := newStopFixture(t, fs)

	if out := x.StopSell(r, 0.25, 40, 1780000270000); out != StopSold {
		t.Fatalf("部分成交也应是 StopSold（有成交即落盘）: %v", out)
	}
	if !near(r.ExitShares, 1.0) || r.ExitPrice != 0.25 || r.RemainingShares() <= 0 {
		t.Fatalf("首笔部分成交字段不对: %.4f @%.3f", r.ExitShares, r.ExitPrice)
	}

	// 第二次卖剩余, 价格不同 ⇒ 加权平均。
	rest := r.RemainingShares()
	fs.res = flip.SellResult{Status: flip.ExecStatusFilled, Shares: rest, Price: 0.27}
	if out := x.StopSell(r, 0.27, 35, 1780000280000); out != StopSold {
		t.Fatalf("第二笔应成交: %v", out)
	}
	wantAvg := (1.0*0.25 + rest*0.27) / (1.0 + rest)
	if !near(r.ExitShares, 1.0+rest) || !near(r.ExitPrice, wantAvg) {
		t.Fatalf("累计应为 %.4f 股 @%.4f（加权）, 得到 %.4f @%.4f", 1.0+rest, wantAvg, r.ExitShares, r.ExitPrice)
	}
	if r.ExitRem != 40 || r.ExitTs != 1780000270000 {
		t.Fatalf("ExitRem/ExitTs 应停在首次卖出: rem=%d ts=%d", r.ExitRem, r.ExitTs)
	}
	if r.RemainingShares() != 0 {
		t.Fatalf("两笔卖完剩余应为 0: %.4f", r.RemainingShares())
	}
}

// TestStopSellUnfilledRetries 未吃到对手方: **不落盘不冻结**——下一次调用（下一 tick）
// 照常再试（FAK 的重试语义）。
func TestStopSellUnfilledRetries(t *testing.T) {
	fs := &fakeSell{res: flip.SellResult{Status: flip.ExecStatusUnfilled, Note: "FAK 无对价"}}
	x, _, _, r := newStopFixture(t, fs)

	if out := x.StopSell(r, 0.29, 41, 1780000270000); out != StopRetry {
		t.Fatalf("未成交应为 StopRetry: %v", out)
	}
	if r.IsStopped() || r.ExitNote != "" {
		t.Fatalf("未成交不该写任何字段: exit=%.4f note=%q", r.ExitShares, r.ExitNote)
	}
	if out := x.StopSell(r, 0.28, 40, 1780000271000); out != StopRetry {
		t.Fatalf("第二次未成交仍应 StopRetry: %v", out)
	}
	if fs.calls != 2 {
		t.Fatalf("未成交应可重试, 卖出被调用 %d 次", fs.calls)
	}
}

// TestStopSellRejectedFreezes 拒单/未知结果 ⇒ 冻结: 写一次 exit_note, 此后
// StopSell 恒 StopNone（**绝不重试**——可能已成交, 重试就是卖两次）。
func TestStopSellRejectedFreezes(t *testing.T) {
	fs := &fakeSell{res: flip.SellResult{
		Status: flip.ExecStatusRejected,
		Note:   flip.ExecNoteUnknown + ": POST(SELL) 超时",
	}}
	x, _, _, r := newStopFixture(t, fs)

	if out := x.StopSell(r, 0.29, 41, 1780000270000); out != StopFrozen {
		t.Fatalf("拒单应为 StopFrozen: %v", out)
	}
	if r.ExitNote == "" {
		t.Fatal("冻结必须留下 exit_note（人工核对线索）")
	}
	if out := x.StopSell(r, 0.20, 30, 1780000271000); out != StopNone {
		t.Fatalf("冻结后应恒 StopNone: %v", out)
	}
	if fs.calls != 1 {
		t.Fatalf("冻结后不得再卖, 卖出被调用 %d 次", fs.calls)
	}
}

// TestStopSellLiveMinSharesFreeze live 下残仓 < 5 股（CLOB 最小单量）⇒ 冻结且
// **不发起卖出调用**（必被交易所拒）; paper 不套这条（默认 stake 整仓 < 5 股）。
func TestStopSellLiveMinSharesFreeze(t *testing.T) {
	fs := &fakeSell{}
	x, _, _, r := newStopFixture(t, fs)
	x.Live = true // 2/0.92 ≈ 2.17 股 < 5

	if out := x.StopSell(r, 0.29, 41, 1780000270000); out != StopFrozen {
		t.Fatalf("live 残仓不足最小单量应冻结: %v", out)
	}
	if fs.calls != 0 {
		t.Fatalf("不该发起注定被拒的卖单: %d 次", fs.calls)
	}
	if r.ExitNote == "" {
		t.Fatal("应留下冻结说明")
	}

	// paper 同一持仓照常卖出（同一夹具、Live=false）。
	xp, _, _, rp := newStopFixture(t, fs)
	if out := xp.StopSell(rp, 0.29, 41, 1780000270000); out != StopSold {
		t.Fatalf("paper 不受最小单量约束: %v", out)
	}
}

// TestStopSellGuards 编排的四条静默防线: 无持仓/已卖完/未注入 Sell 器。
func TestStopSellGuards(t *testing.T) {
	fs := &fakeSell{}
	x, rec, _, r := newStopFixture(t, fs)

	if out := x.StopSell(nil, 0.2, 40, 1); out != StopNone {
		t.Fatalf("nil 行应 StopNone: %v", out)
	}
	// 判定行（无仓位）不得卖出。
	dec := okSnap(1780000300000, flip.SideYes)
	dec.OK, dec.Shares = false, 0
	drec := x.HandleDecision(dec, "0xdec", "slug", 1780000000)
	if out := x.StopSell(drec, 0.2, 40, 1); out != StopNone {
		t.Fatalf("无仓位行应 StopNone: %v", out)
	}
	// 卖完后（ExitShares 打满）再调用 ⇒ StopNone。
	r.ExitShares, r.ExitPrice = r.Shares, 0.3
	if out := x.StopSell(r, 0.2, 40, 1); out != StopNone {
		t.Fatalf("已卖完应 StopNone: %v", out)
	}
	// 未注入卖出器 ⇒ 静默不臆造（有声日志, 返回 StopNone）。
	x.Sell = nil
	r2 := x.HandleDecision(okSnap(1780000400000, flip.SideNo), "0xnosell", "slug", 1780000000)
	if out := x.StopSell(r2, 0.2, 40, 1); out != StopNone {
		t.Fatalf("Sell 未注入应 StopNone: %v", out)
	}
	if fs.calls != 0 {
		t.Fatalf("所有防线都不该触发卖出: %d 次", fs.calls)
	}
	_ = rec
}

// TestRecordExitClampsAndGuards Recorder.RecordExit 的输入口径: 超量卖出被**夹到剩余**
// （交易所最多卖出手上的股数）; 参数非法/无仓位/无行各自报错。
func TestRecordExitClampsAndGuards(t *testing.T) {
	_, rec, _, r := newStopFixture(t, &fakeSell{})
	all := r.Shares

	// 超量: 请求 2 倍剩余 ⇒ 夹到剩余。
	got, err := rec.RecordExit("0xc", all*2, 0.25, 40, 1780000270000)
	if err != nil {
		t.Fatalf("超量卖出应夹取而非报错: %v", err)
	}
	if !near(got.ExitShares, all) {
		t.Fatalf("应夹到 %.4f, 得到 %.4f", all, got.ExitShares)
	}
	// 已卖完再卖 ⇒ 报错（无剩余）。
	if _, err := rec.RecordExit("0xc", 1, 0.25, 40, ts(1)); err == nil {
		t.Fatal("已卖完再卖应报错")
	}
	// 参数非法。
	if _, err := rec.RecordExit("0xc", 0, 0.25, 40, ts(1)); err == nil {
		t.Fatal("shares=0 应报错")
	}
	if _, err := rec.RecordExit("0xc", 1, 0, 40, ts(1)); err == nil {
		t.Fatal("price=0 应报错")
	}
	// 不存在的行。
	if _, err := rec.RecordExit("0xnope", 1, 0.25, 40, ts(1)); err == nil {
		t.Fatal("无落盘行应报错")
	}
}

func ts(ms int64) int64 { return 1780000270000 + ms }
