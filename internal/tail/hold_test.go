package tail

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/necklace/flip-signal/internal/flip"
)

// baseHoldIdent 返回一个锚可用、持仓侧为 no 的身份（各用例按需改 Side/Anchor）。
func baseHoldIdent() HoldIdent {
	return HoldIdent{
		ConditionID: "0xabc", Slug: "btc-updown-5m-1000", Stage: StageT150,
		Side: flip.SideNo, EntryFill: 0.87, EntryRem: 148, Anchor: 60000, HistBps: 9.5,
	}
}

// baseHoldTick 返回一个门控全过的 tick（各用例按需改）。
func baseHoldTick() flip.Tick {
	return flip.Tick{
		Ts: 1_700_000_000_000, Rem: 100, BookLatMs: 12,
		UpBid: 0.10, UpAsk: 0.11, DownBid: 0.88, DownAsk: 0.89,
		BinPrice: 60080, SpotAgeMs: 300,
	}
}

// TestHoldWatchRowGate 钉住门控——**与判定路径刻意不同的那几条**。
//
// 判据是「持仓监察要能看见 26 号脚本看不见的东西」: bid == 0、缺对手侧、rem > 150 的
// tick 都必须留下行来, 否则本次加监察就白加了（那些正是要观测的时刻）。
func TestHoldWatchRowGate(t *testing.T) {
	cases := []struct {
		name string
		id   HoldIdent
		tick flip.Tick
		want bool // nil 与否
	}{
		{"正常 tick", baseHoldIdent(), baseHoldTick(), true},

		// ── 判定路径会丢、本监察必须留的 ──
		{"持仓侧 bid == 0（决策 #21 的输家侧撤空）", baseHoldIdent(),
			func() flip.Tick { t := baseHoldTick(); t.DownBid = 0; return t }(), true},
		{"持仓侧 bid/ask 全 0（整簿空）", baseHoldIdent(),
			func() flip.Tick { t := baseHoldTick(); t.DownBid, t.DownAsk = 0, 0; return t }(), true},
		{"对手侧全 0（只看持仓侧，不该被对手侧拖累）", baseHoldIdent(),
			func() flip.Tick { t := baseHoldTick(); t.UpBid, t.UpAsk = 0, 0; return t }(), true},
		{"rem > 150（持仓后一路看到闭市）", baseHoldIdent(),
			func() flip.Tick { t := baseHoldTick(); t.Rem = 200; return t }(), true},

		// ── 无有效读数（nil）──
		{"rem == 0 终 tick", baseHoldIdent(),
			func() flip.Tick { t := baseHoldTick(); t.Rem = 0; return t }(), false},
		{"rem < 0 越界", baseHoldIdent(),
			func() flip.Tick { t := baseHoldTick(); t.Rem = -1; return t }(), false},
		{"盘口延迟超阈", baseHoldIdent(),
			func() flip.Tick { t := baseHoldTick(); t.BookLatMs = 301; return t }(), false},
		{"盘口延迟恰在阈值", baseHoldIdent(),
			func() flip.Tick { t := baseHoldTick(); t.BookLatMs = 300; return t }(), true},
		{"spot 缺失", baseHoldIdent(),
			func() flip.Tick { t := baseHoldTick(); t.BinPrice = 0; return t }(), false},
		{"锚不可用", func() HoldIdent { i := baseHoldIdent(); i.Anchor = 0; return i }(),
			baseHoldTick(), false},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := HoldWatchRow(c.id, 300, c.tick)
			if (got != nil) != c.want {
				t.Fatalf("HoldWatchRow 非 nil = %v, 期望 %v", got != nil, c.want)
			}
			if got == nil {
				return
			}
			// 身份字段必须原样透传（分析要按 condition_id 把监察行与信号行对上）。
			if got.Kind != holdKind {
				t.Errorf("Kind = %q, 期望 %q", got.Kind, holdKind)
			}
			if got.ConditionID != c.id.ConditionID || got.Slug != c.id.Slug ||
				got.Stage != c.id.Stage || got.Side != c.id.Side ||
				got.EntryFill != c.id.EntryFill || got.EntryRem != c.id.EntryRem ||
				got.Anchor != c.id.Anchor || got.HistBps != c.id.HistBps {
				t.Errorf("身份字段未原样透传: %+v", got)
			}
			if got.Date == "" {
				t.Error("Date 未填（按日切分的依据）")
			}
		})
	}
}

// TestHoldWatchRowSideReads 钉住「读的是持仓侧那一档」——yes 读 UP、no 读 DOWN。
//
// 写反了会得到一个看着完全正常的监察数据集（价格量级一样），却全程记着**对手侧**
// 的盘口, 而且直到分析时才会发现。故用一个两侧差异极大的 tick 钉死。
func TestHoldWatchRowSideReads(t *testing.T) {
	tick := func() flip.Tick {
		t := baseHoldTick()
		t.UpBid, t.UpAsk = 0.31, 0.32
		t.DownBid, t.DownAsk = 0.88, 0.89
		return t
	}()

	yes := HoldWatchRow(func() HoldIdent { i := baseHoldIdent(); i.Side = flip.SideYes; return i }(),
		300, tick)
	if yes.HoldBid != 0.31 || yes.HoldAsk != 0.32 {
		t.Errorf("yes 侧应读 UP 档, 得到 bid=%v ask=%v", yes.HoldBid, yes.HoldAsk)
	}
	// dev 的符号必须跟着侧别翻（`sgn·(spot − anchor)`，sgn: yes +1 / no −1）。
	// 现货从 60000 涨到 60080: 押 yes 是顺风 ⇒ dev 正; 押 no 是逆风 ⇒ dev 负。
	if yes.Dev <= 0 {
		t.Errorf("押 yes 且 spot(60080) > anchor(60000) ⇒ dev 应为正, 得到 %v", yes.Dev)
	}

	no := HoldWatchRow(baseHoldIdent(), 300, tick)
	if no.HoldBid != 0.88 || no.HoldAsk != 0.89 {
		t.Errorf("no 侧应读 DOWN 档, 得到 bid=%v ask=%v", no.HoldBid, no.HoldAsk)
	}
	if no.Dev >= 0 {
		t.Errorf("押 no 且 spot > anchor ⇒ dev 应为负, 得到 %v", no.Dev)
	}
}

// TestHoldWatchRowStopCand 钉住 StopCand 的判据 = `bid < 0.30 ∧ dev < −20`。
//
// 它是**纯派生标记**（引擎不会因为它做任何事），但分析脚本要靠它一步筛出
// 「若真挂了止损，这一秒会不会触发」，判据写错会让整个监察白跑。
func TestHoldWatchRowStopCand(t *testing.T) {
	// 押 no ⇒ sgn = −1 ⇒ dev = −(spot − anchor)。要让 dev < −20 需 spot > 60020。
	mk := func(bid, spot float64) *HoldRow {
		t := baseHoldTick()
		t.DownBid, t.BinPrice = bid, spot
		return HoldWatchRow(baseHoldIdent(), 300, t)
	}
	cases := []struct {
		name string
		bid  float64
		spot float64
		want bool
	}{
		{"两条腿都过", 0.25, 60030, true},
		{"价格腿不过（bid 恰 0.30）", 0.30, 60030, false},
		{"价格腿不过（bid 0.31）", 0.31, 60030, false},
		{"位移腿不过（dev 恰 −20）", 0.25, 60020, false},
		{"位移腿不过（dev = −19）", 0.25, 60019, false},
		{"bid == 0（最有价值的候选）", 0.0, 60030, true},
		{"两条腿都不过", 0.85, 59990, false}, // 押 no 而现货跌 = dev 正 = 有利方向
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := mk(c.bid, c.spot); got.StopCand != c.want {
				t.Errorf("bid=%v spot=%v ⇒ StopCand=%v, 期望 %v（dev=%v）",
					c.bid, c.spot, got.StopCand, c.want, got.Dev)
			}
		})
	}
}

// TestLogHoldTick 钉住落盘: 独立前缀 + kind 双保险 + 行内不带任何引擎/判定字段。
func TestLogHoldTick(t *testing.T) {
	dir := t.TempDir()
	r, err := NewRecorder(dir)
	if err != nil {
		t.Fatalf("NewRecorder: %v", err)
	}
	row := *HoldWatchRow(baseHoldIdent(), 300, baseHoldTick())
	row.HoldBid5, row.HoldAsk5 = 123.5, 0
	if err := r.LogHoldTick(row); err != nil {
		t.Fatalf("LogHoldTick: %v", err)
	}
	if err := r.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}

	matches, _ := filepath.Glob(filepath.Join(dir, holdPrefix+"*.jsonl"))
	if len(matches) != 1 {
		t.Fatalf("期望 1 个 %s 文件, 得到 %v", holdPrefix, matches)
	}
	// 前缀隔离红线: 监察行**不得**落进 tail_/tailwin_/tailstats_（决策 #9）。
	for _, p := range []string{recordPrefix, windowPrefix, statsPrefix} {
		if m, _ := filepath.Glob(filepath.Join(dir, p+"*.jsonl")); len(m) != 0 {
			t.Errorf("监察行污染了 %s: %v", p, m)
		}
	}

	raw, err := os.ReadFile(matches[0])
	if err != nil {
		t.Fatalf("读回: %v", err)
	}
	var back map[string]any
	if err := json.Unmarshal([]byte(strings.TrimSpace(string(raw))), &back); err != nil {
		t.Fatalf("解析落盘行: %v（原文 %s）", err, raw)
	}
	if back["kind"] != holdKind {
		t.Errorf("kind = %v, 期望 %q", back["kind"], holdKind)
	}
	if back["condition_id"] != "0xabc" {
		t.Errorf("condition_id = %v", back["condition_id"])
	}
	if back["hold_bid5"] != 123.5 {
		t.Errorf("hold_bid5 = %v（深度没落盘 ⇒ 无法回答「够不够吃」）", back["hold_bid5"])
	}
	// 监察行**不是**判定行: 不该带 ok/rules/exec_status 这些引擎字段。
	for _, k := range []string{"ok", "rules", "reject_reason", "exec_status", "pnl", "won"} {
		if _, has := back[k]; has {
			t.Errorf("监察行不应带引擎/判定字段 %q", k)
		}
	}
	if back["date"] == "" || back["date"] == nil {
		t.Error("date 未落盘")
	}
}

// TestLogHoldTickKeepsZeroBid 钉住本监察**存在的理由**: bid == 0 的行必须原样落盘。
//
// 这是整条链路上唯一会丢它的地方——json 的 `omitempty` 会让 0 消失, 分析侧就永远
// 看不到「没有对手方」这条关键读数（历史 14 天里它恒为 0 正是被这么构造出来的）。
func TestLogHoldTickKeepsZeroBid(t *testing.T) {
	dir := t.TempDir()
	r, _ := NewRecorder(dir)

	tk := baseHoldTick()
	tk.DownBid, tk.DownAsk = 0, 0 // 持仓侧整簿空
	if err := r.LogHoldTick(*HoldWatchRow(baseHoldIdent(), 300, tk)); err != nil {
		t.Fatalf("LogHoldTick: %v", err)
	}
	r.Close()

	matches, _ := filepath.Glob(filepath.Join(dir, holdPrefix+"*.jsonl"))
	raw, _ := os.ReadFile(matches[0])
	var back map[string]any
	if err := json.Unmarshal([]byte(strings.TrimSpace(string(raw))), &back); err != nil {
		t.Fatalf("解析: %v", err)
	}
	for _, k := range []string{"hold_bid", "hold_ask"} {
		v, has := back[k]
		if !has {
			t.Fatalf("%q 被 omitempty 吃掉了 —— 监察失去意义（原文 %s）", k, raw)
		}
		if v != 0.0 {
			t.Errorf("%q = %v, 期望 0", k, v)
		}
	}
}
