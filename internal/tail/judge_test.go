package tail

import (
	"math"
	"testing"

	"github.com/necklace/flip-signal/internal/flip"
)

// TestBootstrapCIParity 是「Go 判决 == python 判决」的核心红线:
// 期望值由 venv python 3.13.2 用 13_tail_sweep.py:424 的 boot_days 原样跑出:
//
//	python/venv/bin/python -c "
//	    import random
//	    def boot_days(dvals, B=2000, seed=42):
//	        rnd=random.Random(seed)
//	        v=sorted(sum(dvals[rnd.randrange(len(dvals))] for _ in dvals) for _ in range(B))
//	        return v[int(0.025*B)], v[int(0.975*B)]"
//
// ⚠️ 日 P&L 的**顺序**参与结果（抽的是下标）——下面每个用例都按 python 侧的实际
// 顺序给出; 线上由 newGrid 按日期排序固定（= 逐日追加文件的首现顺序）。
func TestBootstrapCIParity(t *testing.T) {
	cases := []struct {
		name   string
		days   []float64
		lo, hi float64
	}{
		{"14 日合成样本", []float64{3.2, 2.7, -1.1, 4.5, 0.8, 2.2, 3.9, 1.4, -0.6, 2.8, 3.1, 1.9, 2.4, 3.6}, 18.7, 41.6},
		{"单日", []float64{2.5}, 2.5, 2.5},
		{"四日同日值（退化）", []float64{2.5, 2.5, 2.5, 2.5}, 10.0, 10.0},
		{"三日含负", []float64{-1.0, -2.0, 0.5}, -6.0, 1.5},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			lo, hi := BootstrapCI(tc.days, judgeBootB, judgeBootSeed)
			if lo != tc.lo || hi != tc.hi {
				t.Fatalf("BootstrapCI = (%.17g, %.17g), want (%.17g, %.17g)",
					lo, hi, tc.lo, tc.hi)
			}
		})
	}
}

// TestBootstrapCIEmpty 空样本不 panic、返回 0（「尚未有任何已结算注」的表示）。
func TestBootstrapCIEmpty(t *testing.T) {
	if lo, hi := BootstrapCI(nil, judgeBootB, judgeBootSeed); lo != 0 || hi != 0 {
		t.Fatalf("空样本 = (%v, %v), want (0, 0)", lo, hi)
	}
}

// TestVerdictFor 判决表逐档（前置门槛 → 下界>0 → 上界<0 → 28 日仍跨 0）。
func TestVerdictFor(t *testing.T) {
	cases := []struct {
		name    string
		days, n int
		lo, hi  float64
		want    string
	}{
		{"日数不足", 13, 900, 5, 20, VerdictPending},
		{"注数不足", 14, 799, 5, 20, VerdictPending},
		{"下界 > 0 通过", 14, 800, 0.1, 20, VerdictPass},
		{"上界 < 0 判负", 14, 800, -20, -0.1, VerdictFail},
		{"跨 0 不显著", 14, 800, -3, 12, VerdictInconclusive},
		{"28 日仍跨 0 判负", 28, 1600, -3, 12, VerdictFail},
		{"28 日下界 > 0 通过", 28, 1600, 2, 12, VerdictPass},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := verdictFor(tc.days, tc.n, tc.lo, tc.hi); got != tc.want {
				t.Fatalf("verdictFor = %q, want %q", got, tc.want)
			}
		})
	}
}

// TestJudgeSynthetic 用合成行驱动 Judge: 五格 + T=150 对照格的 n/WR/P&L 与手算一致、
// 样本不足时判词恒 pending、被闸行被剔除。
func TestJudgeSynthetic(t *testing.T) {
	cfg := DefaultConfig()
	// 三天 × 每天 3 条 snap 行: dev=100 的赢、dev=100 的输、dev=10 的输。
	// ⑤（dev ≥ 63）应选中每天的前两条 → n=6, 3 赢 3 输, P&L = 3×(shares−2) − 3×2。
	var recs []*Record
	for d, date := range []string{"2026-09-01", "2026-09-02", "2026-09-03"} {
		for i, dev := range []float64{100, 100, 10} {
			won := i == 0
			pnl := -2.0
			if won {
				pnl = 0.05 // 假设 0.95 成交价 → 2/0.95 − 2 ≈ 0.105; 这里只用可核对的常数
			}
			recs = append(recs, &Record{
				Observation: Observation{
					Kind: KindSnap, Side: flip.SideYes, HotAsk: 0.95, Spot: 100_100,
					Twap: 100_000, Anchor: 100_000, HistBps: 8, Dev: dev,
					Sd: 80, Rules: EvalRules(cfg, 0.95, dev, 80, true), OK: true,
				},
				EventType: "tail", Date: date, ConditionID: date, EventStart: int64(d),
				Stake: 2, Won: &won, PnL: pnl,
			})
		}
	}

	grids := Judge(recs, cfg)
	if len(grids) != len(gridLabels) {
		t.Fatalf("格数 = %d, want %d", len(grids), len(gridLabels))
	}
	byRule := map[string]Grid{}
	for _, g := range grids {
		byRule[g.Rule] = g
	}

	// ① 不过滤: 全部 9 条（价格腿 0.95 ≥ 0.80 恒真）
	if g := byRule["1"]; g.N != 9 || g.Days != 3 || g.Won != 3 {
		t.Errorf("① N/Days/Won = %d/%d/%d, want 9/3/3", g.N, g.Days, g.Won)
	}
	// ⑤ dev ≥ 63: 每天 2 条 → n=6（★ 关键: ③④⑤ 在这一组数据上等价, 因为 dev ≥ sd 恒真）
	if g := byRule["5"]; g.N != 6 || g.Days != 3 || g.Won != 3 {
		t.Errorf("⑤ N/Days/Won = %d/%d/%d, want 6/3/3", g.N, g.Days, g.Won)
	}
	// P&L = 3 x (0.05 − 2) = −5.85
	if g := byRule["5"]; math.Abs(g.PnL-(-5.85)) > 1e-9 {
		t.Errorf("⑤ P&L = %v, want -5.85", g.PnL)
	}
	// 样本远不足判决门槛 → 判词 pending + Ready=false
	for _, g := range grids {
		if g.Ready || g.Verdict != VerdictPending {
			t.Errorf("%s: Ready=%v Verdict=%q, want false/pending", g.Rule, g.Ready, g.Verdict)
		}
	}
	// 频率 2 注/日 → 低于 90, 频率闸不通过
	if g := byRule["5"]; g.FreqOK {
		t.Errorf("⑤ FreqOK = true（%.2f 注/日）, want false", g.NotesPerDay)
	}
}

// TestJudgeSkipsGatedAndUnsettled 被闸行与未结算行不计入任何格。
func TestJudgeSkipsGatedAndUnsettled(t *testing.T) {
	cfg := DefaultConfig()
	mk := func(gate string, won *bool) *Record {
		return &Record{
			Observation: Observation{
				Kind: KindSnap, Side: flip.SideYes, HotAsk: 0.95, Spot: 100_100,
				Twap: 100_000, Anchor: 100_000, HistBps: 8, Dev: 100, Sd: 80,
				Rules: EvalRules(cfg, 0.95, 100, 80, true), OK: true,
			},
			EventType: "tail", Date: "2026-09-01", ConditionID: "c", Stake: 2,
			GateReason: gate, Won: won, PnL: 1,
		}
	}
	tYes, fNo := true, false
	recs := []*Record{
		mk("", &tYes),            // 计入
		mk(GateDailyLoss, &tYes), // 被闸 → 剔除
		mk("", nil),              // 未结算 → 剔除
		mk("", &fNo),             // 计入（输）
	}
	g := Judge(recs, cfg)[0] // ① 不过滤
	if g.N != 2 || g.Won != 1 || g.PnL != 2 {
		t.Fatalf("① N/Won/PnL = %d/%d/%v, want 2/1/2", g.N, g.Won, g.PnL)
	}
}

// TestJudgeFrameGridUsesEvalRules 帧行（不带规则标记）由 EvalRules 现算,
// 且宇宙过滤与 snap 行一致（spot/twap/σ 缺一不可）。
func TestJudgeFrameGridUsesEvalRules(t *testing.T) {
	cfg := DefaultConfig()
	tYes := true
	frame := func(spot, twap, histBps, dev float64) *Record {
		return &Record{
			Observation: Observation{
				Kind: KindFrame, Side: flip.SideYes, HotAsk: 0.95, Spot: spot,
				Twap: twap, Anchor: 100_000, HistBps: histBps, Dev: dev, Sd: 80,
			},
			EventType: "tail", Date: "2026-09-01", ConditionID: "c", Stake: 2,
			Won: &tYes, PnL: 1,
		}
	}
	recs := []*Record{
		frame(100_100, 100_000, 8, 100), // 计入（⑤ 成立）
		frame(100_100, 0, 8, 100),       // twap 缺失 → 剔除（宇宙约束）
		frame(0, 100_000, 8, 100),       // spot 缺失 → 剔除
		frame(100_100, 100_000, 0, 100), // σ 不可用 → 剔除
		frame(100_100, 100_000, 8, 10),  // dev 太小 → ⑤ 不成立
	}
	grids := Judge(recs, cfg)
	var t150 Grid
	for _, g := range grids {
		if g.Rule == "t150" {
			t150 = g
		}
		// 帧行不得混进 snap 格
		if g.Rule != "t150" && g.N != 0 {
			t.Errorf("%s: N = %d, want 0（帧行只进 T=150 对照格）", g.Rule, g.N)
		}
	}
	if t150.N != 1 {
		t.Fatalf("T=150 格 N = %d, want 1", t150.N)
	}
}

// TestJudgeFrameBorrowsWindowOutcome 线上帧行（Won 恒 nil —— recorder 只给
// snap+ok+成交 的行挂结算）按 conditionID 借同窗快照行的官方结果:
//   - 同窗快照已结算 → 进 T=150 格, 且**按帧行自己的热门侧**判胜负（帧与快照的热门侧
//     可能不同, 直接搬快照的 won 会算错）;
//   - 同窗快照没结算 → 不进（该窗结果未知）;
//   - P&L 是假想注: 按 cfg.Stake 与帧行 hot_ask 现算（shares = stake / hot_ask）。
func TestJudgeFrameBorrowsWindowOutcome(t *testing.T) {
	cfg := DefaultConfig()
	tYes, tNo := true, false

	// 帧行不带 Won / Stake / Shares（= 线上形态）
	frame := func(cond, side string, hotAsk float64) *Record {
		return &Record{
			Observation: Observation{
				Kind: KindFrame, Side: side, HotAsk: hotAsk, Spot: 100_100,
				Twap: 100_000, Anchor: 100_000, HistBps: 8, Dev: 100, Sd: 80,
			},
			EventType: "tail", Date: "2026-09-01", ConditionID: cond,
		}
	}
	// 快照行：c1 押 yes 赢（→ outcome Up）, c2 押 yes 输（→ outcome Down）, c3 未结算
	snap := func(cond string, won *bool) *Record {
		return &Record{
			Observation: Observation{
				Kind: KindSnap, Side: flip.SideYes, HotAsk: 0.95, Spot: 100_100,
				Twap: 100_000, Anchor: 100_000, HistBps: 8, Dev: 100, Sd: 80,
				Rules: EvalRules(cfg, 0.95, 100, 80, true), OK: true,
			},
			EventType: "tail", Date: "2026-09-01", ConditionID: cond,
			Stake: 2, Won: won, PnL: 1,
		}
	}

	recs := []*Record{
		frame("c1", flip.SideNo, 0.95),  // 该窗 outcome=Up（快照押 yes 赢）: 帧押 no → **输**
		frame("c2", flip.SideNo, 0.95),  // 该窗 outcome=Down（快照押 yes 输）: 帧押 no → **赢**
		frame("c3", flip.SideYes, 0.95), // 该窗快照未结算 → 结果未知, 不进格
		snap("c1", &tYes), snap("c2", &tNo), snap("c3", nil),
	}

	grids := Judge(recs, cfg)
	var t150 Grid
	var g1 Grid
	for _, g := range grids {
		switch g.Rule {
		case "t150":
			t150 = g
		case "1":
			g1 = g
		}
	}
	// 快照格不受帧行影响: c1 赢 + c2 输（c3 未结算不进）
	if g1.N != 2 || g1.Won != 1 {
		t.Errorf("① N/Won = %d/%d, want 2/1", g1.N, g1.Won)
	}
	if t150.N != 2 {
		t.Fatalf("T=150 N = %d, want 2（c3 的快照未结算 → 结果未知）", t150.N)
	}
	if t150.Won != 1 {
		t.Fatalf("T=150 Won = %d, want 1（胜负按帧行自己的热门侧判, 不是搬快照的 won）", t150.Won)
	}
	// P&L = c1 输 −2 + c2 赢 (2/0.95 − 2) = −1.8947…
	wantPnL := -2 + (2/0.95 - 2)
	if math.Abs(t150.PnL-wantPnL) > 1e-9 {
		t.Fatalf("T=150 PnL = %v, want %v", t150.PnL, wantPnL)
	}
}
