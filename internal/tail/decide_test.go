package tail

import (
	"math"
	"testing"

	"github.com/necklace/flip-signal/internal/flip"
)

// TestEvalRules 覆盖四条原始腿与五格的派生。
//
// 关键红线（写在用例名里）:
//   - 价格腿是**全部五格的前置**——hotAsk < 0.80 时哪怕 dev/σ 腿全过, 五格皆 false。
//   - hasSigma=false 时 sd 必须被忽略——哪怕 dev 远大于 sd, ③ 也不得放行
//     （sd=0 时 `dev >= sd` 恒真, 这是最容易写出「冷启动期全放行」的地方）。
func TestEvalRules(t *testing.T) {
	cfg := DefaultConfig()
	cases := []struct {
		name     string
		hotAsk   float64
		dev, sd  float64
		hasSigma bool
		want     Rules
	}{
		{
			name: "全过: 美元腿与σ腿同时成立", hotAsk: 0.92, dev: 100, sd: 50, hasSigma: true,
			want: Rules{Price: true, Dev63: true, Sigma: true, SigmaUSD40: true},
		},
		{
			// 价格腿前置: dev=200（远超 63）、σ 腿也够, 但 ask=0.79 → 一格都不成立。
			// 漏掉这个 AND 就会去交易便宜 ask 的窗口 —— 那正是 dog@0.2 族的地盘。
			name: "价格腿拦截: ask=0.79 时 dev=200/σ齐备也不放行", hotAsk: 0.79, dev: 200, sd: 50, hasSigma: true,
			want: Rules{Price: false, Dev63: true, Sigma: true, SigmaUSD40: true},
		},
		{
			name: "价格腿边界: ask=0.80 恰好放行", hotAsk: 0.80, dev: 0, sd: 50, hasSigma: true,
			want: Rules{Price: true, Dev63: false, Sigma: false, SigmaUSD40: false},
		},
		{
			// σ 不可用（冷启动: 已完窗 <3）——dev=200 让 `dev >= sd(=0)` 恒真,
			// 若用 sd>0 判可用性, ③④⑤ 会在这批最危险的窗口全放行。
			name: "σ不可用: sd=0 且 dev=200 不得放行 σ 腿", hotAsk: 0.92, dev: 200, sd: 0, hasSigma: false,
			want: Rules{Price: true, Dev63: true, Sigma: false, SigmaUSD40: false},
		},
		{
			name: "σ不可用且美元腿也没过: 五格皆 false", hotAsk: 0.92, dev: 10, sd: 0, hasSigma: false,
			want: Rules{Price: true, Dev63: false, Sigma: false, SigmaUSD40: false},
		},
		{
			name: "美元腿边界: dev=63 恰好放行", hotAsk: 0.92, dev: 63, sd: 50, hasSigma: true,
			want: Rules{Price: true, Dev63: true, Sigma: true, SigmaUSD40: true},
		},
		{
			// 美元腿差一点（62.9 < 63）但 σ 腿补上（dev ≥ sd=50 且 sd ≥ 40）
			// ——正是 ⑤ 里 `dev ≥ 63 ∨ σ腿` 那个「或」的意义。
			name: "美元腿差一点: dev=62.9 但 σ 腿过", hotAsk: 0.92, dev: 62.9, sd: 50, hasSigma: true,
			want: Rules{Price: true, Dev63: false, Sigma: true, SigmaUSD40: true},
		},
		{
			// dev=50 ≥ sd=50 → ③ 成立; sd=50 ≥ 40 → ⑤ 的 σ 腿也成立。
			// 注意 ⑤ 此时**不靠**美元腿（dev < 63）, 走的是 σ 腿分支。
			name: "σ腿内部: dev≥sd 且 sd≥40 → ⑤ 成立", hotAsk: 0.92, dev: 50, sd: 50, hasSigma: true,
			want: Rules{Price: true, Dev63: false, Sigma: true, SigmaUSD40: true},
		},
		{
			// sd=39.9 < 40 → ⑤ 的 σ 腿被门挡住（这正是 ⑤ 相对 ④ 的唯一差别）,
			// 但 dev=39.9 ≥ sd → ③④ 仍然成立。⑤ (Rule5) = false 而 ④ (Rule4) = true。
			name: "⑤与④的差别: sd=39.9 时 ④ 过而 ⑤ 不过", hotAsk: 0.92, dev: 39.9, sd: 39.9, hasSigma: true,
			want: Rules{Price: true, Dev63: false, Sigma: true, SigmaUSD40: false},
		},
		{
			name: "σ腿下限边界: sd=40 恰好放行", hotAsk: 0.92, dev: 40, sd: 40, hasSigma: true,
			want: Rules{Price: true, Dev63: false, Sigma: true, SigmaUSD40: true},
		},
		{
			name: "dev 差一点到 sd: dev=49.9 < sd=50", hotAsk: 0.92, dev: 49.9, sd: 50, hasSigma: true,
			want: Rules{Price: true, Dev63: false, Sigma: false, SigmaUSD40: false},
		},
		{
			// 负位移: 押注方向反向 —— 三条腿全 false（⑤ 不下单, 与回测一致）。
			name: "负位移: dev=-50 sd=50", hotAsk: 0.92, dev: -50, sd: 50, hasSigma: true,
			want: Rules{Price: true, Dev63: false, Sigma: false, SigmaUSD40: false},
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := EvalRules(cfg, c.hotAsk, c.dev, c.sd, c.hasSigma)
			if got != c.want {
				t.Fatalf("EvalRules(ask=%.2f dev=%.1f sd=%.1f hasSigma=%v)\n  得到 %+v\n  期望 %+v",
					c.hotAsk, c.dev, c.sd, c.hasSigma, got, c.want)
			}
			// 五格派生（价格腿的 AND 由 RuleN 负责, 这里逐格钉住作用域）。
			wantRules := []bool{
				c.want.Price,                                        // ①
				c.want.Price && c.want.Dev63,                        // ②
				c.want.Price && c.want.Sigma,                        // ③
				c.want.Price && (c.want.Dev63 || c.want.Sigma),      // ④
				c.want.Price && (c.want.Dev63 || c.want.SigmaUSD40), // ⑤
			}
			gotRules := []bool{got.Rule1(), got.Rule2(), got.Rule3(), got.Rule4(), got.Rule5()}
			for i, want := range wantRules {
				if gotRules[i] != want {
					t.Fatalf("Rule%d() = %v, 期望 %v（原始腿 %+v）", i+1, gotRules[i], want, c.want)
				}
			}
			// 前置性本身: 价格腿不过 ⇒ 五格皆 false（不依赖上面逐格推导的写法）。
			if !got.Price {
				for i, r := range gotRules {
					if r {
						t.Fatalf("价格腿 false 但 Rule%d() = true —— 价格腿前置被破坏", i+1)
					}
				}
			}
		})
	}
}

// TestRule5IsTheEngineRule 钉住「引擎只下单 ⑤」这一条: ⑤ 是 ④ 的**真子集**
// （σ 腿多了 sd ≥ 40 的门），且 ④∖⑤ 的样本正是文档 §4.3 里被砍掉的那批。
func TestRule5IsTheEngineRule(t *testing.T) {
	cfg := DefaultConfig()
	// sd=50 ≥ 40 → 两格都过; sd=39.9 → 只过 ④。
	for _, sd := range []float64{50, 39.9} {
		r := EvalRules(cfg, 0.92, sd, sd, true) // dev = sd ⇒ σ 腿刚好成立
		if !r.Rule4() {
			t.Fatalf("sd=%.1f: dev=sd 时 ④ 应成立, 得到 %+v", sd, r)
		}
		if want := sd >= cfg.SigmaMinUSD; r.Rule5() != want {
			t.Fatalf("sd=%.1f: ⑤ 应为 %v, 得到 %v（原始腿 %+v）", sd, want, r.Rule5(), r)
		}
	}
}

// TestHotBook 热门侧与**有效价**的取值口径（a.md 第 2 条: 每侧 ask 优先、bid 兜底,
// 热门侧 = 有效价高的一侧、平局取 yes; 四档全空才是「无有效盘口」）。
//
// 与回测的对应: python 的宇宙要求四档齐全（`all(k > 0)`）, 而本函数在空一侧时仍
// 返回有效价——历史 14 天的 542800 个 tick 里这种情形**一次都没有**（见
// parity_test.go 文件头）, 故两套门同源; 差异只在 live（决策 #21: 赢家侧 ask 被
// 整侧撤空时, 旧口径会连 tick 一起丢掉, 尾盘最确定的那段行情一行都不产）。
func TestHotBook(t *testing.T) {
	cases := []struct {
		name                     string
		upBid, upAsk, dnBid, dnA float64
		wantSide, wantSrc        string
		wantPx                   float64
	}{
		{"四档齐全, yes 高", 0.90, 0.92, 0.07, 0.09, flip.SideYes, BookSrcAsk, 0.92},
		{"四档齐全, no 高", 0.07, 0.09, 0.90, 0.93, flip.SideNo, BookSrcAsk, 0.93},
		// 平局取 yes（与 python 的 `ya >= na` 同）
		{"平局取 yes", 0.90, 0.92, 0.06, 0.92, flip.SideYes, BookSrcAsk, 0.92},
		// bid 兜底: 热门侧的 ask 被整侧撤空（决策 #21 的实盘形态: 赢家侧空 asks）
		{"yes ask 空 → 用 yes bid", 0.97, 0, 0.03, 0.04, flip.SideYes, BookSrcBid, 0.97},
		{"no ask 空 → 用 no bid", 0.03, 0.04, 0.97, 0, flip.SideNo, BookSrcBid, 0.97},
		// 两侧各空一个: 有效价 0.99(yes bid) vs 0.98(no bid)
		{"两侧 ask 皆空", 0.99, 0, 0.98, 0, flip.SideYes, BookSrcBid, 0.99},
		// 四档全空 = 无有效盘口（调用方以 px > 0 判该 tick 无效）
		{"四档全空", 0, 0, 0, 0, flip.SideYes, "", 0},
		// 单侧只有 bid 而对手侧有 ask: 仍按有效价比大小, 不因缺 ask 就判无效
		{"yes 只有 bid vs no 有 ask", 0.85, 0, 0.10, 0.11, flip.SideYes, BookSrcBid, 0.85},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			side, px, src := HotBook(c.upBid, c.upAsk, c.dnBid, c.dnA)
			if side != c.wantSide || src != c.wantSrc || px != c.wantPx {
				t.Fatalf("HotBook(%v,%v,%v,%v) = (%s, %.4f, %q), 期望 (%s, %.4f, %q)",
					c.upBid, c.upAsk, c.dnBid, c.dnA, side, px, src, c.wantSide, c.wantPx, c.wantSrc)
			}
			// 成交量口径: 股数 = stake/有效价 —— 价格腿与执行器都吃这个值（px ≤ 0 时
			// 调用方根本不该走到这里, 故只在 px > 0 时检查来源标记合法）。
			if px > 0 && src != BookSrcAsk && src != BookSrcBid {
				t.Fatalf("有效价 > 0 却给了未知来源 %q", src)
			}
		})
	}
}

// TestDevAndSigmaUSD 派生量的单位与符号: dev 是**美元**位移（押 no 时取负）、
// sd 是 bps 换算的美元值——传错单位会得到量级完全错误的族（decide.go 的注释警告）。
func TestDevAndSigmaUSD(t *testing.T) {
	// spot 在锚上方 60 美元: 押 yes（+）= +60, 押 no（−）= −60
	if got := DevUSD(flip.SideYes, 100060, 100000); got != 60 {
		t.Fatalf("DevUSD(yes) = %.4f, 期望 +60", got)
	}
	if got := DevUSD(flip.SideNo, 100060, 100000); got != -60 {
		t.Fatalf("DevUSD(no) = %.4f, 期望 −60", got)
	}
	// σ = hist_bps·anchor/1e4: 9.26bps × 100000 = 92.6 美元
	if got := SigmaUSD(9.26, 100000); math.Abs(got-92.6) > 1e-9 {
		t.Fatalf("SigmaUSD = %.6f, 期望 92.6", got)
	}
	// σ 不可用（≤0）恒 0 —— 决不能让 sd = 0 让 dev ≥ sd 恒真（EvalRules 的 hasSigma）
	if got := SigmaUSD(0, 100000); got != 0 {
		t.Fatalf("σ 不可用时应为 0, 得到 %.4f", got)
	}
	if got := SigmaUSD(-1, 100000); got != 0 {
		t.Fatalf("σ 为负时应为 0（不取绝对值）, 得到 %.4f", got)
	}
}
