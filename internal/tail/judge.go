package tail

import (
	"math"
	"sort"

	"github.com/necklace/flip-signal/internal/flip"
)

// 判决速览——把 docs/tail_sweep_2026-09-22.md §5.2 的预设判据做成纯函数, 供
// dashboard 直接给结论（口径 = python/v4/13_tail_sweep.py 的 boot_days, 逐位一致）。
//
// 判据（先定后看, 不许事后挑格）:
//   - 样本门槛: UTC 日满 judgeMinDays 个 **且** 该格已结算注数 ≥ judgeMinN;
//   - 主判据 = 日级 bootstrap（按日重采样 judgeBootB 次）P&L 的 95% 区间:
//     下界 > 0 → 通过 / 上界 < 0 → 判负 / 跨 0 → 不显著（延到 judgeDays2 日,
//     届时仍跨 0 即判负）;
//   - 辅助闸门: 频率应落 freqLo~freqHi 注/日（越界 = 行情结构变了, 先查原因）。
//
// 本文件不碰判定/执行路径, 只读 Record。
const (
	judgeMinDays  = 14    // 判决时点: UTC 日数
	judgeMinN     = 800   // 判决时点: 该格注数
	judgeDays2    = 28    // 「不显著」的延长判决时点
	judgeBootB    = 2000  // bootstrap 次数（= python boot_days 默认值）
	judgeBootSeed = 42    // bootstrap 种子（= python boot_days 默认值）
	freqLo        = 90.0  // 频率闸下界（注/日）
	freqHi        = 120.0 // 频率闸上界（注/日）
)

// 判词（ASCII, 前端映射成中文——同 flip 的 reject_reason 处理方式）。
const (
	VerdictPending      = "pending"      // 未到判决时点（日数或注数不足）
	VerdictPass         = "pass"         // 95% 区间下界 > 0
	VerdictFail         = "fail"         // 95% 区间上界 < 0（或 28 日仍跨 0）
	VerdictInconclusive = "inconclusive" // 跨 0 → 延长到 28 日
)

// JudgeMeta 是判决口的公开读数（Dashboard 的「判决口径」行 + python 对账说明用）。
// 只是把上面那组常量导出成一个值——单一真相仍在包内, 前端不得自己写死 14/800/2000。
type JudgeMeta struct {
	MinDays  int     `json:"min_days"`  // 判决时点: UTC 日数
	MinN     int     `json:"min_n"`     // 判决时点: 注数
	Days2    int     `json:"days2"`     // 「不显著」的延长判决时点
	BootB    int     `json:"boot_b"`    // bootstrap 次数
	BootSeed int     `json:"boot_seed"` // bootstrap 种子
	FreqLo   float64 `json:"freq_lo"`   // 频率闸下界（注/日）
	FreqHi   float64 `json:"freq_hi"`   // 频率闸上界
}

// Meta 返回判决口径常量（供 dashboard 展示; 口径来源见本文件顶部）。
func Meta() JudgeMeta {
	return JudgeMeta{
		MinDays: judgeMinDays, MinN: judgeMinN, Days2: judgeDays2,
		BootB: judgeBootB, BootSeed: judgeBootSeed, FreqLo: freqLo, FreqHi: freqHi,
	}
}

// Grid 是一格的判决读数（①~⑤ 之一, 或 T=150 帧行对照格）。
type Grid struct {
	Rule  string `json:"rule"`  // "1".."5" / "t150"
	Label string `json:"label"` // 过滤腿口径（前端副标题）

	N           int     `json:"n"`             // 已结算注数
	Days        int     `json:"days"`          // 有已结算注的 UTC 日数
	Won         int     `json:"won"`           // 赢的注数
	WR          float64 `json:"wr"`            // 胜率（won/n）
	PnL         float64 `json:"pnl"`           // 累计 P&L（USDC）
	LosingDays  int     `json:"losing_days"`   // 亏损日数（当日合计 P&L < 0）
	CILo        float64 `json:"ci_lo"`         // 日级 bootstrap P&L 95% 区间下界
	CIHi        float64 `json:"ci_hi"`         // 上界
	Verdict     string  `json:"verdict"`       // 见 Verdict* 常量
	Ready       bool    `json:"ready"`         // 样本门槛是否已过（Days ≥ 14 ∧ N ≥ 800）
	NotesPerDay float64 `json:"notes_per_day"` // 频率（注/日）——与 freqLo~freqHi 对照
	FreqOK      bool    `json:"freq_ok"`       // 频率闸是否在带内
}

// 五格 + 两个对照格的口径描述（顺序即展示顺序）。
var gridLabels = []struct {
	rule  string
	label string
}{
	{"1", "① 不过滤（hot_ask ≥ 0.80）"},
	{"2", "② 纯美元（dev ≥ 63）"},
	{"3", "③ 纯 σ（dev ≥ sd）"},
	{"4", "④ 联合（dev ≥ 63 或 dev ≥ sd）"},
	{"5", "⑤ 定向（dev ≥ 63 或 40 ≤ sd ≤ dev）← 引擎下单格"},
	{"t150", "T=150 的 ⑤（帧行复算, 对照；T 不可再调。只覆盖同窗快照已结算的窗口）"},
	{"scan", "监听增量 B∖A（快照未达标后首个 ⑤ 达标 tick；只记录、无仓位）"},
}

// Judge 计算五格与两个对照格（T=150 / 监听增量）的判决读数。
//
// 输入是 Recorder.Observations() 的全量行（frame + snap + scan 混装）:
//   - 主格用 kind=snap 行, 规则取落盘时已算好的 Rules.RuleN();
//   - T=150 对照格用 kind=frame 行, 规则由 EvalRules 现算（帧行不带规则标记）,
//     结算取自**同窗快照行**（见 outcomeByCond 注释）;
//   - 监听增量格用 kind=scan 行（自带结算与仓位字段, 直接聚合——见 pickScans）;
//   - **被闸行（gate_reason 非空）一律剔除**（映射文档 §2.2: 纸面被闸行照记照结算,
//     分析须显式过滤——它们是「不熔断会怎样」的反事实, 不是策略样本）。scan 行
//     **从不过闸**（HandleScan 不碰 gate）, 故这一条对它天然无效;
//   - 未结算行（Won == nil）不计入 n/胜率/P&L（判决只吃已结算样本）。
func Judge(recs []*Record, cfg Config) []Grid {
	snaps := make([]*Record, 0, len(recs))
	frames := make([]*Record, 0, len(recs))
	scans := make([]*Record, 0, len(recs))
	// 窗口 → 官方结果（0=Up / 1=Down）。**含被闸行**: gate 只决定「这一注算不算
	// 策略样本」, 不改变那个窗口市场的真实结果。
	//
	// 为什么要这张表: **帧行自己不挂结算**（recorder 只对 snap+ok+成交的行注册结算
	// 轮询, 帧行 Won 恒 nil）, 而 T=150 对照格的判定仍需要「那个窗口最后谁赢」。
	// 同一窗口的帧与快照是同一次结算的对象（同一 conditionID、同一次 TWAP 收盘）,
	// 故按 conditionID 借同窗快照行的结果即可——这也是该格**只覆盖同窗快照已结算
	// 的窗口**的原因（python 侧是全样本离线复算, 宇宙更宽）。
	outcomeByCond := map[string]int{}
	for _, rec := range recs {
		if rec.Kind == KindSnap && rec.Won != nil && rec.ConditionID != "" {
			out := flip.OutcomeUp
			if *rec.Won != flip.WonFor(rec.Side, flip.OutcomeUp) {
				out = flip.OutcomeDown
			}
			outcomeByCond[rec.ConditionID] = out
		}
		if rec.GateReason != "" {
			continue
		}
		switch rec.Kind {
		case KindSnap:
			snaps = append(snaps, rec)
		case KindFrame:
			frames = append(frames, rec)
		case KindScan:
			scans = append(scans, rec)
		}
	}

	out := make([]Grid, 0, len(gridLabels))
	for _, g := range gridLabels {
		var picked []*Record
		switch g.rule {
		case "t150":
			picked = pickFrames(frames, cfg, outcomeByCond, func(r Rules) bool { return r.Rule5() })
		case "scan":
			picked = pickScans(scans)
		default:
			n := int(g.rule[0] - '0')
			picked = pickSnaps(snaps, n)
		}
		out = append(out, newGrid(g.rule, g.label, picked))
	}
	return out
}

// pickSnaps 取 kind=snap 行里第 n 格成立且已结算的注（n ∈ 1..5）。
func pickSnaps(recs []*Record, n int) []*Record {
	out := make([]*Record, 0, len(recs))
	for _, rec := range recs {
		if rec.Won == nil {
			continue
		}
		var ok bool
		switch n {
		case 1:
			ok = rec.Rules.Rule1()
		case 2:
			ok = rec.Rules.Rule2()
		case 3:
			ok = rec.Rules.Rule3()
		case 4:
			ok = rec.Rules.Rule4()
		case 5:
			ok = rec.Rules.Rule5()
		}
		if ok {
			out = append(out, rec)
		}
	}
	return out
}

// pickScans 取已结算的 kind=scan 行（监听增量格）。
//
// 与 pickSnaps 的两点不同:
//   - **不再判规则**: scan 行是引擎只在那个 tick 达标时才产出的（OK 恒真, 落盘前已过
//     `Rules.Rule5() && spot>0 && histBps>0`）, 被拒的 tick 根本不落行——没有
//     RejectReason 段可读, 也就无需重判;
//   - **不需要 frameAsBet**: scan 行落盘时就按 cfg.Stake 与 hot_ask 折好了
//     Stake/Shares（假想仓位）, 故直接聚合即可。⚠️ 这些字段是**反事实记账**, 不代表
//     账户里真有仓位——这也是它不进 DailyPnl 的原因（见 recorder.DailyPnl 注）。
//
// 与 snap 的互斥性保证这一格恰是 **B∖A**: 引擎在 snap 达标时不再产监听行, 故 scan
// 行的窗口集合 = 「快照没成交、监听段成交」的那些窗（B = A 格 + 本格, 离线脚本
// python/v4/18_tail_scan_register.py 据此配对算增量与区间）。
func pickScans(recs []*Record) []*Record {
	out := make([]*Record, 0, len(recs))
	for _, rec := range recs {
		if rec.Won != nil {
			out = append(out, rec)
		}
	}
	return out
}

// pickFrames 取 kind=frame 行里现算规则成立且结果已知的注（T=150 对照格）。
//
// 宇宙过滤与 snap 行一致: 快照 tick 上 spot 与 twap **同时在场**（python 的宇宙约束,
// 映射文档 §2.3 第 2 条）+ σ 可用——缺任一项的窗在 python 里根本不进样本。
// 帧行没有 RejectReason 可供判读（它只记录）, 故这里按输入齐备性复现该约束。
//
// 结果来源两条路:
//   - 行里已带结算（Won != nil, 只出现在测试合成行里）→ 原样用;
//   - 线上帧行（Won 恒 nil）→ 按 conditionID 借同窗快照的官方结果, 并**按帧行自己的
//     热门侧**重算胜负与 P&L——帧行的热门侧可能与快照行不同（rem≤150 时 ask 高的一侧
//     到 rem≤60 可能已经反过来）, 直接搬快照行的 won 会把这件事抹掉。
//     这一支的 P&L 是**假想注**: 帧行没有仓位（Stake/Shares 全 0）, 按 cfg.Stake 与
//     帧行的 hot_ask 现算（shares = stake/hot_ask, 与回测 shares = stake/fill 同口径）。
func pickFrames(recs []*Record, cfg Config, outcomeByCond map[string]int, pass func(Rules) bool) []*Record {
	out := make([]*Record, 0, len(recs))
	for _, rec := range recs {
		if rec.Won == nil {
			outcome, known := outcomeByCond[rec.ConditionID]
			if !known {
				continue // 该窗结果未知（快照行没成交/没结算）→ 不进对照格
			}
			rec = frameAsBet(rec, cfg, outcome)
		}
		if !(rec.Spot > 0 && rec.Twap > 0 && rec.HistBps > 0) {
			continue
		}
		rules := EvalRules(cfg, rec.HotAsk, rec.Dev, rec.Sd, rec.HistBps > 0)
		if pass(rules) {
			out = append(out, rec)
		}
	}
	return out
}

// frameAsBet 把一条帧行按「T=150 那一刻真下了 stake」折算成一条可聚合的注。
// **返回副本**——Judge 的输入是只读全量行, 不得回填到原行上（那会让下一次读取
// 看到一条并不存在的成交）。
func frameAsBet(rec *Record, cfg Config, outcome int) *Record {
	stake := cfg.Stake
	if stake <= 0 {
		stake = rec.Stake // 配置未给 stake 时的兜底（回测口径 2U）
	}
	shares := 0.0
	if rec.HotAsk > 0 {
		shares = stake / rec.HotAsk
	}
	won := flip.WonFor(rec.Side, outcome)
	pnl := -stake
	if won {
		pnl = shares - stake // 每股兑 1U（与回测/纸面同口径）
	}
	cp := *rec
	cp.Stake, cp.Shares, cp.Won, cp.PnL = stake, shares, &won, pnl
	return &cp
}

// newGrid 由选中的注聚合出一格读数（含 bootstrap 区间与判词）。
func newGrid(rule, label string, picked []*Record) Grid {
	g := Grid{Rule: rule, Label: label, N: len(picked)}

	dayPnl := map[string]float64{}
	for _, rec := range picked {
		g.PnL += rec.PnL
		dayPnl[rec.Date] += rec.PnL
		if rec.Won != nil && *rec.Won {
			g.Won++
		}
	}
	g.Days = len(dayPnl)
	if g.N > 0 {
		g.WR = float64(g.Won) / float64(g.N)
	}
	days := make([]string, 0, len(dayPnl))
	for d := range dayPnl {
		days = append(days, d)
	}
	sort.Strings(days) // 固定顺序: bootstrap 抽的是**下标**, 顺序必须可复现
	pnls := make([]float64, 0, len(days))
	for _, d := range days {
		if dayPnl[d] < 0 {
			g.LosingDays++
		}
		pnls = append(pnls, dayPnl[d])
	}

	g.CILo, g.CIHi = BootstrapCI(pnls, judgeBootB, judgeBootSeed)
	g.Ready = g.Days >= judgeMinDays && g.N >= judgeMinN
	g.Verdict = verdictFor(g.Days, g.N, g.CILo, g.CIHi)
	if g.Days > 0 {
		g.NotesPerDay = float64(g.N) / float64(g.Days)
		g.FreqOK = g.NotesPerDay >= freqLo && g.NotesPerDay <= freqHi
	}
	return g
}

// verdictFor 是判决表本体（纯函数, 表驱动测试见 judge_test.go）。
func verdictFor(days, n int, lo, hi float64) string {
	if days < judgeMinDays || n < judgeMinN {
		return VerdictPending
	}
	switch {
	case lo > 0:
		return VerdictPass
	case hi < 0:
		return VerdictFail
	case days >= judgeDays2:
		return VerdictFail // 28 日仍跨 0 ⇒ 判负
	default:
		return VerdictInconclusive
	}
}

// BootstrapCI 复刻 python/v4/13_tail_sweep.py:424 的 boot_days:
// 按日**有放回**抽 len(dayPnls) 个日、每次取抽中日全部注的 P&L 之和, 做 b 次;
// 返回排序后第 int(0.025·b) 与 int(0.975·b) 项（b=2000 → 第 50 / 1950 项）。
//
// dayPnls 的顺序必须固定（调用方按日期排序）——抽的是下标, 顺序变了结果就变。
func BootstrapCI(dayPnls []float64, b, seed int) (lo, hi float64) {
	if len(dayPnls) == 0 || b <= 0 {
		return 0, 0
	}
	rnd := newMT19937(int64(seed))
	v := make([]float64, b)
	for i := range v {
		draws := make([]float64, 0, len(dayPnls))
		for range dayPnls {
			draws = append(draws, dayPnls[rnd.randrange(len(dayPnls))])
		}
		v[i] = neumaierSum(draws)
	}
	sort.Float64s(v)
	return v[int(0.025*float64(b))], v[int(0.975*float64(b))]
}

// neumaierSum 复刻 CPython 3.12+ 内建 `sum()` 对 float 的补偿（Neumaier）求和。
//
// ⚠️ 这不是过度讲究: python 的 boot_days 写的是 `sum(...)`, 而 3.12 起该实现改用了
// 补偿求和——朴素左到右累加会在**最后几位**给出不同的双精度值（实测差 4e-15 量级）。
// CI 取的是排序后的分位元素, 只要有一个再抽样的和落在分位点上, 差异就会原样输出;
// 判决又直接看下界是否 > 0。逐位一致比省一个补偿项便宜得多。
//
// 与 CPython 的循环逐条对应（结果非有限时不加补偿项, 同 CPython 的注释所述）。
func neumaierSum(xs []float64) float64 {
	f, c := 0.0, 0.0
	for _, x := range xs {
		t := f + x
		if math.Abs(f) >= math.Abs(x) {
			c += (f - t) + x
		} else {
			c += (x - t) + f
		}
		f = t
	}
	if c != 0 && !math.IsInf(c, 0) && !math.IsNaN(c) {
		f += c
	}
	return f
}
