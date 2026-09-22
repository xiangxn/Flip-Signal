# -*- coding: utf-8 -*-
"""扫尾盘 T=60 联合格的 σ 分桶（文档 §12.8）

用户 2026-09-23 追加要求：「为联合过滤做一下 σ 的分桶，想排除掉美元值很小的窗口，
因为风险大」。本脚本回答三件事：
  1. 按窗口 σ（1.0σ 折美元）分桶，联合格的钱到底来自哪一档；
  2. 「排除小美元窗」的两种实现（整窗下限 vs 只抬 σ 腿门槛）各要付什么代价；
  3. 判别量到底是「美元位移小」还是别的（§6 价格分桶）。

口径全部复用 13_tail_sweep.py（尾盘快照/成交/结算）与 14_tail_sweep_sigma.py
（σ 历史窗/日级 bootstrap/pl）。⚠️ 本脚本用独立 rng（seed 20260925），
bootstrap 区间的小数位与 14 的 §7 输出**不同**（共享 rng 的抽签次序随调用序变），
点值（n/WR/价/EV/P&L）与 14 逐位一致。

运行：python/venv/bin/python python/v4/15_tail_sweep_union_sigma.py
"""
import sys
import importlib.util
import random
import collections
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from v2.lib import load_events                                    # noqa: E402

T = 60                    # 尾盘起点（联合格只在 T=60 定义，见 §12.7(e)）
SEED = 20260925
BOOT = 2000


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ts = load("ts13", BASE / "13_tail_sweep.py")
s14 = load("s14", BASE / "14_tail_sweep_sigma.py")
pad = s14.pad


def sgn(r):
    return 1.0 if r["side"] == "yes" else -1.0


def build(ev, hr, T):
    """每窗保留身份（逐窗单采，不能整批采——13 会跳过不合格窗，索引对不上）"""
    out = []
    for e in ev:
        g = ts.snapshots([e], hr, T)
        if not g:
            continue
        r = g[0]
        r["dev"] = sgn(r) * r["raw_spot"]                 # 位移（美元，正 = 押注侧）
        r["sig"] = r.get("s_spot")                        # 位移 / 1.0σ
        r["sd"] = r["hist_bps"] * r["anchor"] / 1e4 if r["hist_bps"] else None   # 1.0σ 折美元
        out.append(r)
    return [r for r in out if r["hot"]]                   # 热门侧 = 该窗买热门侧


def qq(a, p):
    a = sorted(a)
    return a[min(len(a) - 1, int(p * len(a)))]


def by_date(rows):
    d = collections.defaultdict(list)
    for r in rows:
        d[r["date"]].append(r)
    return d


def profile(lab, g, days, rng, show_days=False, width=30):
    """一行画像: n/WR/价/EV每注/P&L/日级CI/单日最亏/日注数/落门/前半后半/剔4天"""
    if len(g) < 5:
        print("  " + pad(lab, width) + f"n={len(g)} 样本薄")
        return
    n = len(g)
    pl = s14.pl(g)
    wr = sum(r["settle_won"] for r in g) / n
    px = sum(r["buy"] for r in g) / n
    ds = sorted(set(r["date"] for r in g))
    (plo, phi), _ = s14.day_bootstrap(g, ds, BOOT, rng)
    d = by_date(g)
    worst = min(s14.pl(d[k]) for k in days if k in d)
    ns = [len(d.get(k, [])) for k in days]
    gate = sum(1 for x in ns if 90 <= x <= 120)
    cut = ds[len(ds) // 2]
    h1 = s14.pl([r for r in g if r["date"] < cut])
    h2 = s14.pl([r for r in g if r["date"] >= cut])
    t4 = sorted(d, key=lambda k: -s14.pl(d[k]))[:4]
    rest = [r for k in d if k not in t4 for r in d[k]]
    print("  " + pad(lab, width) + pad(f"n={n}", 8) + pad(f"WR {wr*100:.1f}%", 12)
          + pad(f"价 {px:.3f}", 10) + pad(f"EV/注 {pl/n:+.4f}", 15) + pad(f"P&L {pl:+.1f}U", 12)
          + pad(f"[{plo:+.1f},{phi:+.1f}]", 16) + pad(f"单日最亏 {worst:+.1f}U", 15)
          + pad(f"日注数 {min(ns)}~{max(ns)}", 15) + pad(f"落门 {gate}/13", 11)
          + pad(f"前半 {h1:+.1f}", 11) + pad(f"后半 {h2:+.1f}", 11)
          + f"剔4天 {s14.pl(rest):+.1f}U")
    if show_days:
        print("     逐日 " + " ".join(f"{k[5:]} {s14.pl(v):+.1f}" for k, v in sorted(d.items())
                                       if k != "2026-08-31"))


def main():
    ev = load_events("data/btc")
    hr = ts.hist_ranges(ev)
    H = build(ev, hr, T)
    rng = random.Random(SEED)
    days = [d for d in sorted(set(r["date"] for r in H)) if d != "2026-08-31"]   # 08-31 仅 1 窗

    R63 = [r for r in H if r["dev"] >= 63]                                    # 纯美元尺子
    RSIG = [r for r in H if r["sig"] is not None and r["sig"] >= 1.0]         # 纯 σ 尺子
    UN = [r for r in H if r["dev"] >= 63 or (r["sig"] is not None and r["sig"] >= 1.0)]

    def sigma_leg(F):
        """定向: 美元腿照旧，σ 腿只在 1.0σ ≥ F 美元 的窗放行"""
        return [r for r in H if r["dev"] >= 63
                or (r["sd"] is not None and r["sd"] >= F and r["sig"] is not None and r["sig"] >= 1.0)]

    def window_floor(F):
        """字面: 整窗 1.0σ < F 美元 → 该窗所有行丢掉（含美元腿）"""
        return [r for r in UN if r["sd"] is not None and r["sd"] >= F]

    # ---- §1 联合格按窗口 σ（1.0σ 折美元）分桶
    print("=" * 100)
    print(f"§1 联合格 n={len(UN)} 按「窗口 σ（1.0σ 折美元）」分桶")
    print("=" * 100)
    tot = s14.pl(UN)
    print("  " + pad("窗口 1.0σ", 16) + pad("n", 7) + pad("WR", 9) + pad("成交价", 9)
          + pad("EV/注", 11) + pad("P&L", 10) + "占联合 P&L")
    for lo, hi, lab in ((0, 40, "<40 美元"), (40, 63, "40~63"), (63, 90, "63~90"),
                        (90, 130, "90~130"), (130, 10**9, "≥130")):
        g = [r for r in UN if r["sd"] is not None and lo <= r["sd"] < hi]
        if not g:
            continue
        pl = s14.pl(g)
        print("  " + pad(lab, 16) + pad(str(len(g)), 7)
              + pad(f"{sum(r['settle_won'] for r in g)/len(g)*100:.1f}%", 9)
              + pad(f"{sum(r['buy'] for r in g)/len(g):.3f}", 9)
              + pad(f"{pl/len(g):+.4f}", 11) + pad(f"{pl:+.1f}U", 10) + f"{pl/tot*100:+.0f}%")

    # ---- §2 二维: 窗口 σ × 位移
    print("\n" + "=" * 100)
    print("§2 二维: 窗口 σ × 位移（单元格 = EV/注 | n）—— 右上角结构性为空 = σ 尺子的排除面")
    print("=" * 100)
    SDL = [(0, 40, "<40"), (40, 63, "40~63"), (63, 90, "63~90"), (90, 10**9, "≥90")]
    DVL = [(0, 63, "<63"), (63, 100, "63~100"), (100, 200, "100~200"), (200, 10**9, "≥200")]
    print("  " + pad("位移＼窗口σ", 14) + "".join(pad(x[2] + "美元", 18) for x in SDL))
    for dlo, dhi, dlab in DVL:
        line = "  " + pad(dlab + "美元", 14)
        for slo, shi, _ in SDL:
            g = [r for r in UN if r["sd"] is not None and slo <= r["sd"] < shi
                 and dlo <= r["dev"] < dhi]
            line += pad(f"{s14.pl(g)/len(g):+.4f}|{len(g)}" if len(g) >= 12 else f"n={len(g)} 样本薄", 18)
        print(line)

    # ---- §3 规则同台 + σ 下限两种实现
    print("\n" + "=" * 100)
    print("§3 规则同台（T=60）: σ 下限的两种实现")
    print("=" * 100)
    profile("① 不过滤(全部热门侧)", H, days, rng)
    profile("② 纯美元 dev≥63 美元", R63, days, rng)
    profile("③ 纯σ dev≥1.0σ", RSIG, days, rng)
    profile("④ 联合 dev≥63 或 ≥1.0σ", UN, days, rng)
    profile("⑤ 定向: σ腿门槛≥40 美元", sigma_leg(40), days, rng)
    profile("⑥ 字面: 整窗 σ下限 40 美元", window_floor(40), days, rng)
    profile("⑦ 字面: 整窗 σ下限 63 美元", window_floor(63), days, rng)

    print("\n  σ 腿门槛（定向, 不动美元腿）扫描:")
    for F in (0, 25, 30, 35, 40, 45, 50, 55, 63):
        profile(f"    F={F} 美元" if F else "    F=无(=联合)", sigma_leg(F), days, rng, width=18)

    print("\n  误杀对照: 字面下限会连带丢掉的「美元腿」行（位移≥63 且 窗σ<F）:")
    for F in (40, 63):
        profile(f"    窗σ<{F} 的美元腿行", [r for r in H if r["dev"] >= 63
                                          and r["sd"] is not None and r["sd"] < F], days, rng, width=22)

    # ---- §4 两个「σ 腿放行、美元腿不放行」的格子
    print("\n" + "=" * 100)
    print("§4 联合格里唯一的结构性负值格（A）与它隔壁最好的格（B）")
    print("=" * 100)
    A = [r for r in UN if r["sd"] is not None and r["sd"] < 40 and r["dev"] < 63]
    B = [r for r in UN if r["sd"] is not None and 40 <= r["sd"] < 63 and r["dev"] < 63]
    profile("A: 窗σ<40 且 位移<63 美元", A, days, rng, show_days=True)
    profile("B: 窗σ40~63 且 位移<63 美元", B, days, rng, show_days=True)
    if A:
        print("  A 内部按窗 σ 细分:")
        for lo, hi in ((0, 25), (25, 30), (30, 35), (35, 40)):
            g = [r for r in A if lo <= r["sd"] < hi]
            if len(g) < 5:
                print("    " + pad(f"σ {lo}~{hi} 美元", 18) + f"n={len(g)} 样本薄")
                continue
            pl = s14.pl(g)
            print("    " + pad(f"σ {lo}~{hi} 美元", 18) + pad(f"n={len(g)}", 8)
                  + pad(f"WR {sum(r['settle_won'] for r in g)/len(g)*100:.1f}%", 12)
                  + pad(f"价 {sum(r['buy'] for r in g)/len(g):.3f}", 11)
                  + pad(f"EV/注 {pl/len(g):+.4f}", 15) + f"P&L {pl:+.1f}U")
    for lab, g in (("A 窗σ<40", A), ("B 窗σ40~63", B)):
        if not g:
            continue
        k = sum(r["settle_won"] for r in g)
        n = len(g)
        px = sum(r["buy"] for r in g) / n
        z = 1.96
        cen = (k / n + z * z / (2 * n)) / (1 + z * z / n)
        hw = z * ((k / n * (1 - k / n) / n + z * z / (4 * n * n)) ** 0.5) / (1 + z * z / n)
        print(f"  {lab} Wilson: n={n} 胜 {k} 价 {px:.4f} WR 95%CI [{cen-hw:.4f},{cen+hw:.4f}] "
              f"→ EV/股 区间 [{cen-hw-px:+.4f},{cen+hw-px:+.4f}] "
              f"{'含 0' if cen-hw-px < 0 < cen+hw-px else '不含 0'}"
              f" | 窗σ中位 {qq([r['sd'] for r in g],0.5):.1f} 美元"
              f" 位移中位 {qq([r['dev'] for r in g],0.5):.1f} 美元"
              f" 位移/σ中位 {qq([r['dev']/r['sd'] for r in g],0.5):.2f}")

    # ---- §5 配对差额
    print("\n" + "=" * 100)
    print("§5 配对差额（日级 bootstrap）—— 两条规则的差完全由不重叠集承载")
    print("=" * 100)
    R40 = [r for r in H if r["dev"] >= 40]
    for lab, A_, B_ in (("⑤ − ②  (=B)", sigma_leg(40), R63),
                        ("④ − ②  (=σ 腿独有)", UN, R63),
                        ("纯美元40 − ⑤", R40, sigma_leg(40)),
                        ("纯美元40 − ②  (=40~63 段)", R40, R63)):
        ids = set(id(r) for r in B_)
        diff = [r for r in A_ if id(r) not in ids]
        d = by_date(diff)
        ds = sorted(d)
        (plo, phi), _ = s14.day_bootstrap(diff, ds, BOOT, rng)
        print("  " + pad(lab, 28) + pad(f"n={len(diff)}", 9) + pad(f"P&L {s14.pl(diff):+.1f}U", 13)
              + pad(f"95%CI [{plo:+.1f},{phi:+.1f}]", 22)
              + pad("含 0（不显著）" if plo < 0 < phi else "不含 0", 16)
              + f"日正 {sum(1 for k in ds if s14.pl(d[k]) > 0)}/{len(ds)}")

    # ---- §6 判别量: 价格而不是美元位移
    print("\n" + "=" * 100)
    print("§6 判别量: 成交价分桶（若「美元位移小 = 风险大」成立，这里应看到反向关系）")
    print("=" * 100)
    print("  " + pad("成交价", 14) + pad("n", 7) + pad("WR", 9) + pad("EV/股", 10)
          + pad("EV/注", 11) + pad("P&L", 11) + "位移中位 美元")
    for lo, hi in ((0.80, 0.90), (0.90, 0.95), (0.95, 0.98), (0.98, 0.99), (0.99, 1.01)):
        g = [r for r in H if lo <= r["buy"] < hi]
        if len(g) < 5:
            continue
        n = len(g)
        pl = s14.pl(g)
        wr = sum(r["settle_won"] for r in g) / n
        print("  " + pad(f"{lo:.2f}~{hi:.2f}", 14) + pad(str(n), 7) + pad(f"{wr*100:.1f}%", 9)
              + pad(f"{wr-sum(r['buy'] for r in g)/n:+.4f}", 10) + pad(f"{pl/n:+.4f}", 11)
              + pad(f"{pl:+.1f}U", 11) + f"{qq([r['dev'] for r in g],0.5):.1f}")
    print("\n  对照: 「位移<40 美元」那一段（用户想排除的小位移行）")
    for lab, g in (("  位移<40 美元 且 ≥1.0σ", [r for r in H if r["dev"] < 40
                                            and r["sig"] is not None and r["sig"] >= 1.0]),
                   ("  位移<40 美元 (全部)", [r for r in H if r["dev"] < 40])):
        if len(g) < 5:
            continue
        pl = s14.pl(g)
        print("  " + pad(lab, 24) + pad(f"n={len(g)}", 8)
              + pad(f"WR {sum(r['settle_won'] for r in g)/len(g)*100:.1f}%", 12)
              + pad(f"价 {sum(r['buy'] for r in g)/len(g):.3f}", 11)
              + pad(f"EV/注 {pl/len(g):+.4f}", 15) + f"P&L {pl:+.1f}U")
    print("\n  联合格 + 成交价上限（价格闸能不能替代 σ 腿的取舍）:")
    for cap in (1.01, 0.995, 0.99, 0.985, 0.98):
        profile(f"    价 ≤ {cap:.3f}" if cap < 1.01 else "    无价闸", [r for r in UN if r["buy"] <= cap],
                days, rng, width=20)

    # ---- §7 T 稳定性
    print("\n" + "=" * 100)
    print("§7 同一条 σ 腿修正放到其它尾盘起点（机制稳定性）")
    print("=" * 100)
    for T_ in (150, 90, 60):
        H_ = build(ev, hr, T_)
        days_ = [d for d in sorted(set(r["date"] for r in H_)) if d != "2026-08-31"]
        print(f"  T={T_}s 热门侧 n={len(H_)}")
        profile("    ② 纯美元 dev≥63 美元", [r for r in H_ if r["dev"] >= 63], days_, rng, width=26)
        profile("    ④ 联合", [r for r in H_ if r["dev"] >= 63
                            or (r["sig"] is not None and r["sig"] >= 1.0)], days_, rng, width=26)
        profile("    ⑤ 定向 σ腿门槛≥40 美元", [r for r in H_ if r["dev"] >= 63
                                       or (r["sd"] is not None and r["sd"] >= 40
                                           and r["sig"] is not None and r["sig"] >= 1.0)],
                days_, rng, width=26)


    # ---- §8 ⑤ 的"最优"是选出来的还是推出来的
    print("\n" + "=" * 100)
    print("§8 选择稳定性: σ 腿门槛 F 家族（F=0 ≡ 联合格④, F=63 ≡ 纯美元格②）")
    print("=" * 100)
    FS = [0, 25, 30, 35, 40, 45, 50, 55, 63]
    DPL = {}
    for F in FS:
        d = by_date(sigma_leg(F))
        DPL[F] = {k: s14.pl(v) for k, v in d.items()}
    tt = lambda F, ks: sum(DPL[F].get(k, 0.0) for k in ks)          # noqa: E731
    h1 = [d for d in days if d <= "2026-08-24"]
    h2 = [d for d in days if d > "2026-08-24"]
    print("  " + pad("F", 12) + pad("全期", 11) + pad("前半", 11) + pad("后半", 11) + "逐日")
    for F in FS:
        print("  " + pad(f"{F}" if F else "0(=联合④)", 12) + pad(f"{tt(F,days):+.1f}U", 11)
              + pad(f"{tt(F,h1):+.1f}U", 11) + pad(f"{tt(F,h2):+.1f}U", 11)
              + " ".join(f"{DPL[F].get(k,0.0):+.0f}" for k in days))
    bestF = max(FS, key=lambda F: tt(F, days))
    print(f"  → 全期 argmax = F={bestF}（{tt(bestF,days):+.1f}U）；F=40 {tt(40,days):+.1f}U、"
          f"② {tt(63,days):+.1f}U、④ {tt(0,days):+.1f}U")

    print("\n  1) 分半选择（一半上挑 F，去看另一半）:")
    for fit, oos, lab in ((h1, h2, "前半挑→后半用"), (h2, h1, "后半挑→前半用")):
        Fh = max(FS, key=lambda F: tt(F, fit))
        print(f"     {lab}: 挑中 F={Fh} → 应用段 {tt(Fh,oos):+.1f}U"
              f" | 固定 F=40 {tt(40,oos):+.1f}U | 固定②(63) {tt(63,oos):+.1f}U"
              f" | 应用段事后最优 F{max(FS,key=lambda F: tt(F,oos))}"
              f" {max(tt(F,oos) for F in FS):+.1f}U")

    print("\n  2) 逐日前瞻（只用历史日挑 F，做下一天；前 5/6/7 日做种子不计入）:")
    for seedn in (5, 6, 7):
        acc = collections.Counter()
        picks = []
        for i in range(seedn, len(days)):
            Fh = max(FS, key=lambda F: tt(F, days[:i]))
            picks.append((days[i][5:], Fh))
            for F in {Fh, 40, 63, 0}:                # set: 挑中的值可能是 40/63/0 之一
                acc[F] += DPL[F].get(days[i], 0.0)
        print(f"     种子 {seedn}: 前瞻选择 {acc[Fh]:+.1f}U | 固定 F=40 {acc[40]:+.1f}U"
              f" | 固定②(63) {acc[63]:+.1f}U | 固定④(0) {acc[0]:+.1f}U"
              f" | 每日挑中: {' '.join(f'{d}:{F}' for d, F in picks)}")

    cnt = collections.Counter()
    for _ in range(2000):
        pool = [rng.choice(days) for _ in days]
        cnt[max(FS, key=lambda F: tt(F, pool))] += 1
    print("\n  3) 重采样 13 个日期 2000 次，argmax 落在哪个 F:")
    for F, c in sorted(cnt.items(), key=lambda x: -x[1]):
        print(f"     F={F:<3} {c/20:.1f}%")


if __name__ == "__main__":
    main()
