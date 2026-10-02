package tail

// 本文件的四个函数是**本窗曲线图**上派生量的全部数学（用户口径）:
//   - RequiredPrice = **结算线**（rem ≤ 60）: 现货从此刻起守在 P 不动 ⇒ 闭市 TWAP-60
//     恰好压在 anchor 上 ⇒ 结算 Up;
//   - ExtrapPrice   = **速度外推线**（rem ∈ [60, 150]）: 现货保持当前速度线性走,
//     进入最后 60s 那一刻须站上的价;
//   - TieAt         = 上面第一条的**输入装配**（把逐秒现货和拼成 HistoricalSum）;
//   - DevWalk       = 悬停读数行里那两个**策略派生量**（dev / walk, 美元）。
//
// 放在这里而不是各调用方各写一份: 实时曲线（`cmd/tail` 的 curveBuf）与历史窗曲线
// （`internal/dashboard` 从 events 重建）都要算同样两条线——**一份公式, 两处调用**,
// 否则页面上同一张图会有两种口径（决策 #31）。本包零外部依赖, 纯函数可独立测试。
//
// ⚠️ 三条都不参与判定、不进 P&L: 它们是看图用的派生量（决策 #30）。

const (
	// WindowSec 是 btc-updown-5m 窗口长度（秒）。
	WindowSec = 300
	// TwapLookbackSeconds 是结算口径 TWAP 回看窗口秒数（Chainlink TWAP-60）。
	TwapLookbackSeconds = 60
)

// RequiredPrice 是**结算线**: 从现在到闭市现货一直保持在 P, 闭市那一刻的 TWAP-60
// 恰好等于 anchor。等价说法: 现货守在 P 之上 ⇒ 结算 Up, 之下 ⇒ Down。
//
//	P = (60·anchor − HistoricalSum) / rem
//
// HistoricalSum = 结算窗里**已经定局那 (60−rem) 秒**的价格之和。推导: 闭市 TWAP-60 就是
// 最近 60 秒的均价 ⇒ 要求 [HistoricalSum + P·rem]/60 = anchor, 解出 P 即上式。
// 与展开式 `anchor + (anchor − past)·(60−rem)/rem`（past = HistoricalSum/(60−rem)）恒等,
// 这里取**部分和**形式: HistoricalSum 正是手上直接攒着的量, 不必先除成均价再乘回去。
// 三条边界:
//   - rem = 60: 结算窗一秒都还没定局, HistoricalSum = 0 ⇒ P = 60·anchor/60 = anchor;
//   - rem → 0: 只剩几秒要扳回整段偏差 ⇒ 分母发散（这是真的, 不是数值毛病——
//     只剩 1 秒时要把 60 秒均值拉回 anchor 就得靠一根大阳线/大阴线）;
//   - rem > 60 / rem ≤ 0 / anchor ≤ 0: 无定义, 返 0（前端按「0 = 无读数」断线）。
//
// ⚠️ HistoricalSum 由**逐秒现货**（Binance）估, 而结算走的是 Chainlink 口径: 两者有已知
// 的水平差（两族共用 spot 口径, 见 memory「spot − TWAP 水平差」）, 所以这条线是**看图
// 用的近似**——它的绝对位置带一个小幅系统偏移。想要严格口径得手上有 Chainlink 的逐秒价
// （上游只推 TWAP-60 本身, 反解不出逐秒价: TWAP60(t) 的相邻差分只给出 x(t) − x(t−60)）。
// 这条注释就是那个洞的存档。
func RequiredPrice(anchor float64, rem int, histSum float64) float64 {
	if anchor <= 0 || rem <= 0 || rem > TwapLookbackSeconds {
		return 0
	}
	return (float64(TwapLookbackSeconds)*anchor - histSum) / float64(rem)
}

// ExtrapPrice 是**按当前速度外推**的临界价（用户口径, 推导见 `docs/Price_required.md`）:
// rem ∈ [60, remMax] 之外无定义返 0。
//
//	S_required = Spot + (rem−60)/(rem−30) · (anchor − Spot)
//
// 含义: 假设现货从此刻起保持当前速度 v 线性运行, 则**进入最后 60s 那一刻**的价格必须
// 达到 S_required, 闭市 TWAP-60 才刚好等于 anchor。推导: 最后 60s 的中心点距现在
// rem−30 秒 ⇒ TWAP_final = S₀ + v·(rem−30); 令它等于 anchor 得临界速度
// v_critical = (anchor − S₀)/(rem−30), 而进入最后 60s 那一刻（距现在 rem−60 秒）的
// 价格就是上式。等价说法: **当前速度必须 ≥ v_critical**。
//
// 两条边界给形状: rem = remMax(150) ⇒ 0.25·Spot + 0.75·anchor（离结算最远, 权重大）;
// rem → 60 ⇒ → Spot（只剩「现在」, 速度来不及起作用 —— 所以这条线天然收回现货线上）。
//
// ⚠️ 它与 RequiredPrice **不是同一个量**, 两条线合起来拼满整窗: 本支管
// rem ∈ [60, remMax]（从第一个判定点 T=150 起画）, RequiredPrice 管 rem ∈ (0, 60]
// （第二个判定点起）——正好对上三段链的两个检查点。两者的假设**正好相反**: 本支假设
// 「运动继续」, RequiredPrice 假设「运动停住、守住一个价」。rem = 60 处二者不相等
// （本支 = Spot, RequiredPrice = anchor）: 不是断线, 是两个问题的答案本来就不同。
//
// ⚠️ 它与三条实测线的关系: 系数 (rem−60)/(rem−30) ∈ [0, 0.75] ⇒ 它恒是 **Spot 与
// anchor 的凸组合**, 必然落在两条实测线之间 ⇒ 前端**不需要**给它任何量程照顾
// （对比 RequiredPrice 在 rem→0 发散、要靠 clip/TIE_ROOM 收着）。
func ExtrapPrice(anchor, spot float64, rem, remMax int) float64 {
	if anchor <= 0 || spot <= 0 || rem > remMax || rem < TwapLookbackSeconds {
		return 0
	}
	half := TwapLookbackSeconds / 2                           // 30: 最后 60s 的中心点
	k := float64(rem-TwapLookbackSeconds) / float64(rem-half) // 分母 ≥ 30: rem ≥ 60 保证不为零
	return spot + k*(anchor-spot)
}

// DevWalk 是本点在**该秒热门侧**方向上的两个美元派生量（曲线图悬停读数用; 读不出来时返 0）:
//
//	dev  = sgn·(spot − anchor) —— 现货相对锚的位移（策略里 `dev ≥ 63 美元` 那一条）
//	walk = sgn·(twap − anchor) —— 结算线**已经写进去**的那部分位移（T=150 段入场闸 `≥ 43`）
//
// sgn 取**该秒盘口的热门侧**（UP +1 / DOWN −1）, 来源与引擎判定同一条路（HotBook → SgnFor）
// ⇒ 曲线上的读数与那一刻引擎看到的量同口径。⚠️ 因此市场若在窗内翻边, 这两条读数会跟着
// **换号**——那是「当时看到的」, 不是事后按最终赢家重算的; 两者在定局前可以不同。
//
// 三个「读不出来 ⇒ 0」的边界（前端按「—」显示, 与信号表 dev 列同一个约定）:
//   - 四档全空（窗首首份快照未到 / 采集行的收尾补采 tick, 决策 #25）⇒ **两个都返 0**:
//     不知道该在哪个方向上谈位移, 硬取 yes 会凭空造出一个符号;
//   - anchor ≤ 0（实况里取锚通道命中前 ~2s, 决策 #15）⇒ 没有基准, 两个都返 0;
//   - 单项输入缺失（spot ≤ 0 现货超龄 / twap ≤ 0 尚无推送）⇒ **只有那一项**返 0, 另一项
//     照算——两者互相独立, 不该因为一条线断了就把另一个读数也抹掉。
func (p CurvePoint) DevWalk() (dev, walk float64) {
	side, px, _ := HotBook(p.YesBid, p.YesAsk, p.NoBid, p.NoAsk)
	if px <= 0 || p.Anchor <= 0 {
		return 0, 0
	}
	if p.Spot > 0 {
		dev = DevUSD(side, p.Spot, p.Anchor)
	}
	if p.Twap > 0 {
		walk = WalkUSD(side, p.Twap, p.Anchor)
	}
	return dev, walk
}

// TieAt 是本点当刻的结算线读数（= RequiredPrice 的输入装配; 不适用时返 0）。
//
// pts 是**本点之前**的逐点序列, cur 是本点。HistoricalSum 取结算窗内**已经定局那
// (60−rem) 秒**（半开区间 (Ts−span·1000, 现在]: 含本点这一秒、不含区间起点那一秒）
// 的现货和; 样本数不足期望的一半就放弃本点——宁可不画, 不拿半截数据画一条看起来很像
// 结论的线。缺样本的秒用可得样本的均值代表（sum/n = 「每秒」的估计值）再补满 span 秒,
// 否则缺一秒就让 (60·anchor − HistoricalSum) 凭空多出一整秒的价格。
//
// ⚠️ **契约: pts 不含本点**（本点由 cur 传入）。两条调用路径都天然满足——
// 实时路径此刻还没把本点追加进缓冲, 重建路径传 pts[:i]。重复计入会让本点被算两次,
// 线整体偏一个 tick, 且**只在有信号的那一两秒看得出来**, 极难发现。
//
// 现货缺失（Spot ≤ 0）的点不进和（与引擎判现货缺失同口径: 陈旧价不算数）。
func TieAt(pts []CurvePoint, anchor float64, cur CurvePoint) float64 {
	if anchor <= 0 || cur.Rem <= 0 || cur.Rem > TwapLookbackSeconds {
		return 0
	}
	span := TwapLookbackSeconds - cur.Rem // 已经定局、会被计进闭市 TWAP 的秒数
	fromMs := cur.Ts - int64(span)*1000
	sum, n := 0.0, 0
	for i := range pts {
		if pts[i].Ts <= fromMs {
			continue // 半开区间: 区间起点那一秒不算
		}
		if pts[i].Spot > 0 {
			sum += pts[i].Spot
			n++
		}
	}
	if cur.Spot > 0 {
		sum, n = sum+cur.Spot, n+1
	}
	if n == 0 || 2*n < span+1 { // span=0（rem=60）时恒成立: 该点只需 anchor
		return 0
	}
	return RequiredPrice(anchor, cur.Rem, sum/float64(n)*float64(span))
}
