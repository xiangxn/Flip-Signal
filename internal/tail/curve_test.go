package tail

import (
	"math"
	"testing"
)

// TestRequiredPrice 钉住结算线公式的三条边界与一个恒等式。恒等式才是这条线的**含义**:
// 「已定局那 (60−rem) 秒的和 HistoricalSum、未来 rem 秒全按 P」拼出的 60 秒总和,
// 取平均必须正好等于 anchor。
func TestRequiredPrice(t *testing.T) {
	const span30 = 30
	cases := []struct {
		name    string
		anchor  float64
		rem     int
		histSum float64
		want    float64
	}{
		{"rem=60 一秒未定局 ⇒ 和为空 ⇒ 恰好 anchor", 100000, 60, 0, 100000},
		{"已定局的 30 秒比 anchor 低 100/秒 ⇒ 未来得高 100 才拉得回来",
			100000, 30, (100000 - 100) * span30, 100100},
		{"已定局的 30 秒比 anchor 高 100/秒 ⇒ 未来得低 100", 100000, 30, (100000 + 100) * span30, 99900},
		{"rem=59: 前 1 秒高了 1000 ⇒ (60·anchor − S)/59", 100000, 59, 101000, 100000 - 1000.0/59},
		{"rem>60 无定义", 100000, 61, 99000, 0},
		{"rem=0 发散 ⇒ 不画", 100000, 0, 99000, 0},
		{"锚缺失", 0, 30, 99000, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := RequiredPrice(c.anchor, c.rem, c.histSum)
			if diff := got - c.want; diff > 1e-6 || diff < -1e-6 {
				t.Fatalf("RequiredPrice(%v, %d, %v) = %v, 期望 %v", c.anchor, c.rem, c.histSum, got, c.want)
			}
			if c.want > 0 { // 恒等式: [HistoricalSum + P·rem]/60 == anchor
				if c.rem <= 0 || c.rem > TwapLookbackSeconds {
					return
				}
				avg := (c.histSum + got*float64(c.rem)) / TwapLookbackSeconds
				if d := avg - c.anchor; d > 1e-6 || d < -1e-6 {
					t.Fatalf("闭市 TWAP = %v, 期望 anchor = %v", avg, c.anchor)
				}
			}
		})
	}
}

// TestExtrapPrice 钉住速度外推临界价: 端点、凸组合（⇒ 永远落在 Spot 与 anchor 之间）、
// 定义域（rem ∈ [60, remMax]）与缺失输入。
func TestExtrapPrice(t *testing.T) {
	const anchor = 100000.0
	cases := []struct {
		name string
		spot float64
		rem  int
		// wantK 是期望的系数 (rem−60)/(rem−30): 值 = spot + wantK·(anchor−spot)
		wantK float64
	}{
		{"rem=150（第一个判定点）⇒ 0.75 权重压在 anchor 上", 100200, 150, 0.75},
		{"rem=120", 100200, 120, 60.0 / 90.0},
		{"rem=90", 100200, 90, 30.0 / 60.0},
		{"rem=60 ⇒ 退化成 Spot（速度来不及起作用）", 100200, 60, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			want := c.spot + c.wantK*(anchor-c.spot)
			got := ExtrapPrice(anchor, c.spot, c.rem, 150)
			if d := got - want; d > 1e-9 || d < -1e-9 {
				t.Fatalf("ExtrapPrice(%v, %v, %d) = %v, 期望 %v", anchor, c.spot, c.rem, got, want)
			}
			// 凸组合性质: 值必然夹在 Spot 与 anchor 之间（前端因此不必给它量程照顾）。
			loside, hiside := math.Min(c.spot, anchor), math.Max(c.spot, anchor)
			if got < loside-1e-9 || got > hiside+1e-9 {
				t.Fatalf("值 %v 跑出 [%v, %v]: 凸组合性质被破坏", got, loside, hiside)
			}
		})
	}

	// 边界与缺失输入
	out := []struct {
		name         string
		anchor, spot float64
		rem, remMax  int
	}{
		{"rem > remMax（策略的第一个判定点之前不画）", anchor, 100200, 151, 150},
		{"rem < 60（该段交给结算线, 不是这条线的定义域）", anchor, 100200, 59, 150},
		{"rem = 0", anchor, 100200, 0, 150},
		{"锚缺失", 0, 100200, 120, 150},
		{"现货缺失（无推送/超龄）", anchor, 0, 120, 150},
	}
	for _, c := range out {
		t.Run(c.name, func(t *testing.T) {
			if got := ExtrapPrice(c.anchor, c.spot, c.rem, c.remMax); got != 0 {
				t.Fatalf("ExtrapPrice(%v, %v, %d, %d) = %v, 期望 0", c.anchor, c.spot, c.rem, c.remMax, got)
			}
		})
	}
}

// tiePts 造**本点之前**逐秒的一段序列（Ts 递增, 与实时缓冲同序）。
// ts[i] = baseMs − (n−i)·1000 ⇒ 最后一点落在 baseMs−1000, 本点自己由调用方传。
func tiePts(baseMs int64, n int, spot func(i int) float64) []CurvePoint {
	pts := make([]CurvePoint, n)
	for i := range pts {
		pts[i] = CurvePoint{Ts: baseMs - int64(n-i)*1000, Rem: 60 + (n - i), Spot: spot(i)}
	}
	return pts
}

// TestTieAt 钉住 HistoricalSum 的取样区间与样本数闸门: 区间是**结算窗内已经定局的那
// (60−rem) 秒**（半开区间, 区间起点那一秒不算）, 样本不足期望的一半就返回 0（宁可不画）。
func TestTieAt(t *testing.T) {
	const baseMs = 1000 * 1000
	// 本点之前那 30 秒（Ts = baseMs−30s … baseMs−1s）, 现货恒 100010 —— **起点那一点
	// 故意给个异值**: 它正好压在区间起点 baseMs−30s 上, 误当成「闭区间」就会把它算进去
	// （和偏大 ⇒ 线偏下）。
	pts := tiePts(baseMs, 30, func(i int) float64 {
		if i == 0 {
			return 99000
		}
		return 100010
	})
	cur := CurvePoint{Ts: baseMs, Rem: 30, Spot: 100010}
	// 若从边界那一点起算会把 31 个样本全吃进来, 正确口径只吃 (baseMs−30s, baseMs] = 30 个
	want := 99990.0 // 已定局 30 秒全是 100010 ⇒ P = (60·100000 − 3003000)/30
	if got := TieAt(pts, 100000, cur); got != want {
		t.Fatalf("TieAt = %v, 期望 %v（和应只取结算窗内那 30 秒的现货）", got, want)
	}

	// 样本数闸门: 只剩 3 个历史点 + 本点 = 4 个, 远不足 (30+1)/2 ⇒ 不画
	thin := tiePts(baseMs, 3, func(int) float64 { return 100010 })
	if got := TieAt(thin, 100000, cur); got != 0 {
		t.Fatalf("样本不足仍出线: %v, 期望 0", got)
	}

	// 缺样本的秒要按可得样本的均值补满, 否则 (60·anchor − HistoricalSum) 会凭空少一整秒
	// 的价格。这里 30 秒里只有 15 个样本, 补满后和仍应是 30 × 均值。
	half := tiePts(baseMs, 15, func(int) float64 { return 100010 })
	if got := TieAt(half, 100000, cur); got != want {
		t.Fatalf("缺一半样本的补满 = %v, 期望 %v", got, want)
	}

	// 边界与缺读数
	if got := TieAt(pts, 100000, CurvePoint{Ts: baseMs, Rem: 61, Spot: 100010}); got != 0 {
		t.Fatalf("rem=61 应无值: %v", got)
	}
	if got := TieAt(pts, 0, cur); got != 0 {
		t.Fatalf("锚缺失应无值: %v", got)
	}
	// rem=60: span=0 ⇒ 和为空 ⇒ 恰好落在 anchor 上
	if got := TieAt(pts, 100000, CurvePoint{Ts: baseMs, Rem: 60, Spot: 100010}); got != 100000 {
		t.Fatalf("rem=60 = %v, 期望 anchor 100000", got)
	}
	// 本点自己缺现货（Spot=0）而 pts 又是空的 ⇒ 无样本可算
	if got := TieAt(nil, 100000, CurvePoint{Ts: baseMs, Rem: 30}); got != 0 {
		t.Fatalf("无现货样本应无值: %v", got)
	}
}

// TestTieAtSkipsMissingSpot 现货缺失的采样点不进 HistoricalSum（与引擎判现货缺失
// 同口径: 陈旧价不算数）, 但不妨碍其余点照常成线。
func TestTieAtSkipsMissingSpot(t *testing.T) {
	const baseMs = 2000 * 1000
	pts := tiePts(baseMs, 30, func(i int) float64 {
		if i%2 == 0 {
			return 0 // 一半的点没有现货
		}
		return 100010
	})
	// 有效样本 = 15 个历史点 + 本点 = 16, 2·16 = 32 ≥ 31 ⇒ 仍出线, 均值仍 = 100010
	got := TieAt(pts, 100000, CurvePoint{Ts: baseMs, Rem: 30, Spot: 100010})
	if d := got - 99990.0; d > 1e-6 || d < -1e-6 {
		t.Fatalf("TieAt = %v, 期望 99990（零现货的点应被跳过, 不是当 0 加进去）", got)
	}
}

// TestTieAtExcludesCurrentPoint 钉住 TieAt 的**契约**: `pts` 不含本点（本点由 cur 传）。
// 两条调用路径都照这个来——实时路径此刻还没把本点追加进缓冲, 重建路径传 pts[:i]。
//
// 这条合同**只在数据不均匀时才看得出来**: 序列全是同一个价时, 本点算一次还是两次结果
// 一模一样（历史均值与本点相同）。所以这里特意造一条逐秒递增的序列, 让两种用法的答案
// 相差几十美元——否则哪天有人「顺手」把本点留在 pts 里, 线上只会整体偏一个 tick,
// 在图上根本看不出来。
func TestTieAtExcludesCurrentPoint(t *testing.T) {
	const baseMs = 3000 * 1000
	// 本点之前 30 秒, 现货逐秒 +1000（Ts = baseMs−30s … baseMs−1s, 价格 100000…129000）,
	// 本点 130000 是这条序列的自然延续
	pts := tiePts(baseMs, 30, func(i int) float64 { return 100000 + 1000*float64(i) })
	cur := CurvePoint{Ts: baseMs, Rem: 30, Spot: 130000}

	// 正确: 结算窗 (baseMs−30s, baseMs] 内的样本 = pts[1..29] + 本点, 均值 115500
	if got := TieAt(pts, 100000, cur); got != 84500 {
		t.Fatalf("TieAt = %v, 期望 84500（和 = 30 × 115500）", got)
	}
	// 错误用法: 把本点也塞进 pts ⇒ 它被算两次, 均值被拉向本点, 线整体偏移
	if wrong := TieAt(append(pts, cur), 100000, cur); math.Abs(wrong-84500) < 1 {
		t.Fatalf("本点被算两次却与正确值相同（%v）: 这条用例的前提没了, 换一组能区分的数", wrong)
	}
}

// TestDevWalk 钉住悬停读数行那两个派生量的口径: **符号取该秒热门侧**（与引擎判定同源,
// 所以窗内翻边时读数跟着换号）, 以及三个「读不出来 ⇒ 0」的边界。
//
// 顺带钉住有效价的算法: 热门侧由 `ask > 0 ? ask : bid` 定（决策 #21 的空侧兜底）——
// 若哪天有人把这里改成「只看 ask」, 尾盘那些卖单被整侧撤空的读法会先碎在这里。
func TestDevWalk(t *testing.T) {
	// 基准点: 锚 100000 / TWAP 100040 / 现货 100060
	base := CurvePoint{Anchor: 100000, Twap: 100040, Spot: 100060}
	// 热门侧 = UP（0.94/0.95 > 0.05/0.06）
	upHot := base
	upHot.YesBid, upHot.YesAsk, upHot.NoBid, upHot.NoAsk = 0.94, 0.95, 0.05, 0.06
	// 热门侧 = DOWN（镜像同一本书）
	downHot := base
	downHot.YesBid, downHot.YesAsk, downHot.NoBid, downHot.NoAsk = 0.05, 0.06, 0.94, 0.95
	// UP 侧 ask 被整侧撤空, 只剩 bid 0.97 ⇒ 仍是热门侧（有效价 ask 优先, 没有才用 bid）
	bidOnly := base
	bidOnly.YesBid, bidOnly.YesAsk, bidOnly.NoBid, bidOnly.NoAsk = 0.97, 0, 0.02, 0.03

	noBook := base // 四档全空（窗首首份快照未到 / 采集行的收尾补采 tick）
	noAnchor := upHot
	noAnchor.Anchor = 0 // 实况里取锚通道命中前的 ~2s
	noSpot := upHot
	noSpot.Spot = 0 // 现货超龄
	noTwap := upHot
	noTwap.Twap = 0 // 尚无 TWAP 推送

	cases := []struct {
		name              string
		p                 CurvePoint
		wantDev, wantWalk float64
	}{
		{"热门侧 = UP ⇒ 两个都是正的", upHot, 60, 40},
		{"热门侧 = DOWN ⇒ 符号整个翻过来", downHot, -60, -40},
		{"UP 侧只剩 bid 也照样是热门侧", bidOnly, 60, 40},
		{"四档全空 ⇒ 两个都读不出来（不硬取 yes 造一个符号）", noBook, 0, 0},
		{"没锚 ⇒ 没有基准, 两个都读不出来", noAnchor, 0, 0},
		{"缺现货 ⇒ **只** dev 读不出来, 结算线的位移照算", noSpot, 0, 40},
		{"缺 TWAP ⇒ **只** walk 读不出来, 现货的位移照算", noTwap, 60, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			dev, walk := c.p.DevWalk()
			if dev != c.wantDev || walk != c.wantWalk {
				t.Fatalf("DevWalk() = (%v, %v), 期望 (%v, %v)", dev, walk, c.wantDev, c.wantWalk)
			}
		})
	}

	// 恒等式（曲线图上读出来的两个数必须自洽）: dev − walk = 缺口 basis = sgn·(spot − twap)
	if dev, walk := upHot.DevWalk(); dev-walk != 20 {
		t.Fatalf("dev − walk = %v, 期望 20（= 现货 100060 − TWAP 100040）", dev-walk)
	}
}
