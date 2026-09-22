#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v4 扫尾盘 σ 口径深挖（2026-09-23 用户追加）—— 尺子 / 窗长 / 日级 bootstrap 判定。

建立在 13_tail_sweep.py 的「尾盘快照」口径之上（每窗取 rem ≤ T 的第一个有效 tick，
买 ask 较高的一侧），本脚本只回答四个新问题：

  §1 σ 窗长扫描 N ∈ {6,10,14,18,24,36} + 估计量替换（平均绝对变动 MAD vs 均方根 RMS）
     结论: N=14 与 N=18 的 ρ=0.967（逐窗比值 p10~p90 仅 0.84~1.14）= 同一个变量；
     换窗长/换估计量都不改变任何格位的符号 ⇒ σ 怎么估不是杠杆（同 2026-09-10 的 N=18 宽峰结论）。
  §2 σ 尺子 vs 绝对美元尺子（同 T 对照）—— 谁赢随 T 翻转，没有一方稳定占优。
  §3 T=60 上两种尺子的精确分解（交集 / 只在σ / 只在美元）—— 解释 §2 的 T 依赖。
  §4 候选格的**日级 bootstrap** 排名（重采样「日期」而非「注」）—— 纸面判据的分辨力基准。
  §5 控价检验（固定价格带内只看过滤器）T=60 vs T=150 —— 两格机制不同。

日级 bootstrap: 每轮有放回抽 14 个日期，保留被抽中日期内的全部注再算 P&L ——
  等价于假设「同一日内的注高度相关」，是 P&L 区间最保守的做法（比按注重采样宽得多）。

用法: python 14_tail_sweep_sigma.py [--data DIR] [--stake 2] [--boot 2000] [--seed 20260923]
"""
import argparse
import importlib.util
import random
import sys
from math import sqrt
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from v2.lib import load_events  # noqa: E402

STAKE = 2.0
BOOT = 2000
SEED = 20260923
NS = (6, 10, 14, 18, 24, 36)          # σ 窗长候选（现行 18）
TS = (240, 150, 120, 90, 60)          # 尾盘起点（240 只为 §12.2 的对照行）
# 登记候选格: (T, 腿, 尺子, 阈值) —— 尺子 sig = σ 倍数 / usd = 绝对美元
# T=120/T=240 四行是「§12.2 表里"所有 T=120/T=240 格下界 ≤0"」这句断言的对照（2026-09-23 补，
# 此前该断言引用的行不在本列表里，文档无法由本脚本复现）
CAND = [(60, "spot", "sig", 1.0), (60, "spot", "usd", 63),
        (60, "spot", "usd", 100), (60, "spot", "sig", 1.6),
        (60, "twap", "sig", 1.0), (60, "twap", "usd", 63),
        (150, "twap", "sig", 1.0), (150, "spot", "sig", 1.0),
        (150, "twap", "sig", 1.6), (150, "spot", "sig", 1.6),
        (90, "spot", "sig", 1.6), (90, "spot", "usd", 100), (90, "twap", "sig", 1.6),
        (120, "spot", "sig", 1.0), (120, "twap", "sig", 1.0), (120, "spot", "usd", 63),
        (240, "spot", "sig", 1.0), (240, "twap", "sig", 1.0), (240, "spot", "usd", 63)]


def load_ts():
    """载入 13_tail_sweep.py（文件名以数字开头，需 importlib）—— 复用它全部口径。"""
    spec = importlib.util.spec_from_file_location("ts13", BASE / "13_tail_sweep.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def hist_n(events, n, mode="mad"):
    """前 ≤n 窗的波动估计（美元）。mad = 平均绝对变动（= 现行 σ 口径）; rms = 均方根。"""
    out = {}
    for i, e in enumerate(events):
        v = []
        for j in range(max(0, i - n), i):
            o, c = events[j].get("twap_open_price"), events[j].get("twap_close_price")
            if o and c:
                v.append(abs(c - o))
        if len(v) >= 3:
            out[e["start_time"]] = (sum(v) / len(v) if mode == "mad"
                                    else sqrt(sum(x * x for x in v) / len(v)))
        else:
            out[e["start_time"]] = None
    return out


def pad(s, width):
    """按**显示宽度**左对齐（CJK 与 σ/ρ 等算 2 列）—— 直接用 str.ljust 会错位。"""
    w = sum(2 if ord(c) > 0x2E7F else 1 for c in s)
    return s + " " * max(0, width - w)


def pl(rows, stake=None):
    """样本 P&L（U）。stake 从模块级 STAKE 读（不在定义时绑定，便于 --stake 覆盖）。"""
    s = STAKE if stake is None else stake
    return sum((s / r["buy"] - s) if r["settle_won"] else -s for r in rows)


def by_day(rows):
    d = {}
    for r in rows:
        d.setdefault(r["date"], []).append(r)
    return d


def select(hot, leg, kind, th):
    """按尺子选格: kind='sig' 用 σ 倍数（s_*）, 'usd' 用按侧归一后的美元位移。"""
    if kind == "sig":
        return [r for r in hot if r.get(f"s_{leg}") is not None and r[f"s_{leg}"] >= th]
    return [r for r in hot
            if (1.0 if r["side"] == "yes" else -1.0) * r[f"raw_{leg}"] >= th]


def day_bootstrap(rows, ds, reps, rng, stake=STAKE):
    """日级 bootstrap: 有放回抽 len(ds) 个日期，保留日内全部注 → P&L / EV每注 的分位区间。"""
    d = by_day(rows)
    bp, be, n = [], [], len(rows)
    for _ in range(reps):
        pool = [r for k in (rng.choice(ds) for _ in ds) for r in d[k]]
        v = pl(pool, stake)
        bp.append(v)
        be.append(v / n)          # 每注 EV（注数固定为原样本 n，区间只反映日期抽样）
    bp.sort()
    be.sort()
    q = lambda a, p: a[int(p * len(a))]                      # noqa: E731
    return (q(bp, 0.025), q(bp, 0.975)), (q(be, 0.025), q(be, 0.975))


def line(ts, lab, g, extra=""):
    if len(g) < 5:
        print(f"  {pad(lab, 24)} n<5")
        return
    s = ts.stat(g)
    d = by_day(g)
    t4 = sorted(d, key=lambda k: -pl(d[k]))[:4]
    rest = [r for k in d if k not in t4 for r in d[k]]
    print(f"  {pad(lab, 24)} n={s['n']:4d} WR {s['wr']*100:5.1f}% 价 {s['fill']:.3f} "
          f"EV {s['ev']:+.4f} P&L {s['pl']:+6.1f}U 日正 "
          f"{sum(1 for x in d.values() if pl(x) > 0):2d}/{len(d)} 剔4天 {pl(rest):+6.1f}U{extra}")


def main():
    global STAKE, BOOT, SEED
    ap = argparse.ArgumentParser(description="扫尾盘 σ 口径深挖")
    ap.add_argument("--data", default=str(BASE.parent.parent / "data" / "btc"))
    ap.add_argument("--stake", type=float, default=STAKE)
    ap.add_argument("--boot", type=int, default=BOOT)
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()
    STAKE, BOOT, SEED = args.stake, args.boot, args.seed

    ts = load_ts()
    ev = load_events(args.data)
    hr = ts.hist_ranges(ev)                     # 现行 σ（N=18, MAD）
    S = {T: ts.snapshots(ev, hr, T) for T in TS}
    HOT = {T: [r for r in S[T] if r["hot"]] for T in TS}
    nwin = len(ev)
    print(f"数据 {args.data}: {nwin} 窗   尾盘快照 T ∈ {TS}   注额 {STAKE}U/注\n")
    allhot = [r for T in TS for r in HOT[T]]
    hb = sorted(x["hist_bps"] for x in allhot if x["hist_bps"])
    anc = sorted(x["anchor"] for x in allhot)[len(allhot) // 2]
    sig_usd = hb[len(hb) // 2] * anc / 1e4
    print(f"σ 中位 {hb[len(hb)//2]:.2f}bps × 锚中位 {anc:.0f} 美元 → "
          f"1.0σ ≈ {sig_usd:.0f} 美元, 1.6σ ≈ {sig_usd*1.6:.0f} 美元, "
          f"2.4σ ≈ {sig_usd*2.4:.0f} 美元（下文的「美元」阈值按此量级对照）\n")

    # ---- §1 σ 窗长 / 估计量 ------------------------------------------------
    print("=" * 100)
    print("§1 σ 窗长扫描 + 估计量替换（各格 EV/股 | P&L | 剔4天）")
    print("=" * 100)
    for mode in ("mad", "rms"):
        tag = "平均绝对变动 MAD（现行）" if mode == "mad" else "均方根 RMS（真波动率）"
        print(f"\n  估计量 = {tag}      （单元格 = EV/股 | P&L | 剔4天）")
        print(f"    {'':4s}  " + "  ".join(pad(lab, 24) for lab in
              ("T150 spot≥1.0σ", "T150 twap≥1.0σ", "T90 spot≥1.6σ", "T90 twap≥1.6σ")))
        for n in NS:
            hn = hist_n(ev, n, mode)
            SS = {T: ts.snapshots(ev, hn, T) for T in (150, 90)}
            out = []
            for T, leg, th in ((150, "spot", 1.0), (150, "twap", 1.0),
                               (90, "spot", 1.6), (90, "twap", 1.6)):
                g = select([r for r in SS[T] if r["hot"]], leg, "sig", th)
                st = ts.stat(g)
                d = by_day(g)
                t4 = sorted(d, key=lambda k: -pl(d[k]))[:4]
                rest = [r for k in d if k not in t4 for r in d[k]]
                out.append(f"{st['ev']:+.4f}|{st['pl']:+6.1f}|{pl(rest):+6.1f}")
            mark = "  ←现行" if (n == 18 and mode == "mad") else (
                "  ←用户问的" if n == 14 and mode == "mad" else "")
            print(f"    N={n:2d}  " + "  ".join(f"{o:>24}" for o in out) + mark)
    print("\n  σ 序列相关性（与 N=18 对照，公共窗口）:")
    keys = [e["start_time"] for e in ev]
    H = {n: hist_n(ev, n) for n in NS}
    base = [k for k in keys if H[18][k]]
    for n in (6, 10, 14, 24, 36):
        ks = [k for k in base if H[n][k]]
        a = [H[18][k] for k in ks]
        b = [H[n][k] for k in ks]
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        sa = sqrt(sum((x - ma) ** 2 for x in a))
        sb = sqrt(sum((x - mb) ** 2 for x in b))
        rho = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)
        rat = sorted(H[n][k] / H[18][k] for k in ks)
        print(f"    N={n:2d} ρ={rho:.4f}  比值 p10 {rat[len(rat)//10]:.2f} / "
              f"p50 {rat[len(rat)//2]:.2f} / p90 {rat[len(rat)*9//10]:.2f}")

    # ---- §2 σ 尺子 vs 美元尺子 ---------------------------------------------
    print("\n" + "=" * 100)
    print("§2 σ 尺子 vs 绝对美元尺子（同 T，同腿）")
    print("=" * 100)
    for T in TS:
        print(f"\n  T={T}s（热门侧 n={len(HOT[T])}）")
        for leg in ("spot", "twap"):
            for lab, kind, th in ((f"{leg}≥1.0σ", "sig", 1.0), (f"{leg}≥63美元", "usd", 63),
                                  (f"{leg}≥1.6σ", "sig", 1.6), (f"{leg}≥100美元", "usd", 100),
                                  (f"{leg}≥2.4σ", "sig", 2.4), (f"{leg}≥150美元", "usd", 150)):
                line(ts, lab, select(HOT[T], leg, kind, th))

    # ---- §3 T=60 精确分解 --------------------------------------------------
    print("\n" + "=" * 100)
    print("§3 T=60 上两种尺子的精确分解（解释 §2 的 T 依赖）")
    print("=" * 100)
    rows = []
    for e in ev:                       # 逐事件调用权威 snapshots() 以保留事件身份
        got = ts.snapshots([e], hr, 60)
        if got:
            r = dict(got[0])
            r["_k"] = e["start_time"]
            rows.append(r)
    hot60 = [r for r in rows if r["hot"]]
    mm = {r["_k"]: r for r in hot60}
    A = set(r["_k"] for r in select(hot60, "spot", "sig", 1.0))
    B = set(r["_k"] for r in select(hot60, "spot", "usd", 63))
    print(f"  T=60 热门侧 n={len(hot60)}   spot≥1.0σ n={len(A)}   spot≥63美元 n={len(B)}")
    print(f"  交集 {len(A&B)}   只在σ {len(A-B)}   只在美元 {len(B-A)}")
    for lab, ks in (("只在σ", A - B), ("只在美元", B - A), ("交集", A & B)):
        if not ks:
            continue
        sub = [mm[k] for k in ks]
        st = ts.stat(sub)
        print(f"    {pad(lab, 8)} n={len(ks):4d}  P&L {pl(sub):+6.1f}U  WR {st['wr']*100:5.1f}%  "
              f"价 {st['fill']:.3f}  EV/注 {pl(sub)/len(ks):+.4f}U")

    # ---- §4 日级 bootstrap 排名 --------------------------------------------
    print("\n" + "=" * 100)
    print(f"§4 候选格日级 bootstrap 排名（重采样日期 {BOOT} 次，seed={SEED}）")
    print("=" * 100)
    rng = random.Random(SEED)
    print(f"  {pad('格位', 22)} {'n':>5} {'WR':>7} {'价':>6} {'EV/股':>9} {'P&L':>8} "
          f"{'EV/注 95%CI':>21} {'P&L 95%CI':>19} {'前半':>7} {'后半':>7} {'剔4天':>7}")
    res = []
    for T, leg, kind, th in CAND:
        g = select(HOT[T], leg, kind, th)
        if len(g) < 150:
            continue
        st = ts.stat(g)
        d = by_day(g)
        ds = sorted(d)
        (plo, phi), (elo, ehi) = day_bootstrap(g, ds, BOOT, rng)
        cut = ds[len(ds) // 2]
        h1 = pl([r for r in g if r["date"] < cut])
        h2 = pl([r for r in g if r["date"] >= cut])
        t4 = sorted(ds, key=lambda k: -pl(d[k]))[:4]
        rest = [r for k in ds if k not in t4 for r in d[k]]
        lab = f"T={T} {leg}≥{th}{'σ' if kind == 'sig' else '美元'}"
        res.append((plo, lab, st, phi, elo, ehi, h1, h2, pl(rest)))
    for plo, lab, st, phi, elo, ehi, h1, h2, r4 in sorted(res, reverse=True):
        print(f"  {pad(lab, 22)} {st['n']:5d} {st['wr']*100:6.1f}% {st['fill']:.3f} "
              f"{st['ev']:+9.4f} {st['pl']:+8.1f} [{elo:+7.3f},{ehi:+7.3f}]U/注 "
              f"[{plo:+6.1f},{phi:+6.1f}]U {h1:+7.1f} {h2:+7.1f} {r4:+7.1f}")
    print("  （按 P&L 95% 区间下界降序；'前半/后半'= 按日期序切两半）")

    # ---- §5 控价检验 -------------------------------------------------------
    print("\n" + "=" * 100)
    print("§5 控价检验: 固定价格带内只看过滤器（EV/股 是否随偏离度升）")
    print("=" * 100)
    for T, bands in ((60, ((0.80, 0.90), (0.90, 0.95), (0.95, 0.98), (0.98, 1.01))),
                     (150, ((0.85, 0.92), (0.92, 0.97), (0.97, 1.01)))):
        print(f"\n  T={T}s")
        for lo, hi in bands:
            band = [r for r in HOT[T] if lo <= r["buy"] < hi]
            st = ts.stat(band)
            if not st:
                continue
            print(f"    价格[{lo:.2f},{hi:.2f}) 全部 n={st['n']:4d} WR {st['wr']*100:5.1f}% "
                  f"价 {st['fill']:.3f} EV {st['ev']:+.4f}")
            for leg, kind, th in (("spot", "sig", 1.0), ("spot", "sig", 1.6),
                                  ("spot", "usd", 63), ("spot", "usd", 100),
                                  ("twap", "sig", 1.0), ("twap", "sig", 1.6),
                                  ("twap", "usd", 63)):
                sub = select(band, leg, kind, th)
                s2 = ts.stat(sub)
                if s2 and s2["n"] >= 25:
                    print(f"      + {leg}≥{th}{'σ' if kind=='sig' else '美元':<2s} "
                          f"n={s2['n']:4d} WR {s2['wr']*100:5.1f}% 价 {s2['fill']:.3f} "
                          f"EV {s2['ev']:+.4f} EV@中价 {s2['evmid']:+.4f}")

    # ---- §7 机制: σ 是「每窗一个分母」，以及两把尺子何时分歧 ----------------
    print("\n" + "=" * 100)
    print("§7 机制: σ 不是常数（每窗一个分母）; 高波动窗的折价够不够付反转风险，随 T 变")
    print("=" * 100)
    sgn = lambda r: 1.0 if r["side"] == "yes" else -1.0

    def retag(rows_, N):
        """按 N 窗 σ 重算 s_spot，并把「该窗 1.0σ = 多少美元」挂到 sigusd。"""
        h = hist_n(ev, N)
        out = []
        for r in rows_:
            hh = h.get(r["_k"])
            if not hh:
                continue
            rr = dict(r)
            rr["sigusd"] = hh
            rr["s_spot"] = r["d_spot"] / (hh / r["anchor"] * 1e4)
            out.append(rr)
        return out

    def qq(a, p):
        a = sorted(a)
        return a[min(len(a) - 1, int(p * len(a)))]

    print("\n  7.1 σ 的离散度（把 1.0σ 折成美元）—— 若 σ 是个常数，这行不应有跨度")
    for N in (5, 18, len(ev)):
        gg = retag(hot60, N)
        su = [r["sigusd"] for r in gg]
        lo, md, hi = qq(su, .1), qq(su, .5), qq(su, .9)
        print(f"      N={N:<5d} p10 {lo:5.0f} / p50 {md:5.0f} / p90 {hi:5.0f} 美元"
              f"   p90/p10 = {hi/lo:.2f}×")

    print("\n  7.2 T=60 三组画像（σ 按 N=18）—— 两把尺子在同一美元水位上选了不同的窗")
    g18 = retag(hot60, 18)
    SA = set(r["_k"] for r in g18 if r["s_spot"] >= 1.0)
    UB = set(r["_k"] for r in g18 if sgn(r) * r["raw_spot"] >= 63)
    print("      " + pad("组", 12) + pad("n", 6) + pad("σ中位", 8) + pad("位移中位", 10)
          + pad("位移/σ", 8) + pad("成交价", 8) + "WR")
    for lab, ks in (("只在σ", SA - UB), ("只在美元", UB - SA), ("交集", SA & UB)):
        gg = [r for r in g18 if r["_k"] in ks]
        st = ts.stat(gg)
        sg = qq([r["sigusd"] for r in gg], .5)
        dv = qq([abs(r["raw_spot"]) for r in gg], .5)
        rt = qq([abs(r["raw_spot"]) / r["sigusd"] for r in gg], .5)
        print("      " + pad(lab, 12) + pad(str(len(gg)), 6) + pad(f"{sg:.0f}U", 8)
              + pad(f"{dv:.0f}U", 10) + pad(f"{rt:.2f}", 8)
              + pad(f"{st['fill']:.3f}", 8) + f"{st['wr']*100:.1f}%")

    print("\n  7.3 固定位移带 × σ 高低（全部 T）—— 高波动窗的折价与它的反转代价")
    print("      「便宜」= 低波动档成交价 − 高波动档成交价（>0 = 高波动窗更便宜）")
    for T in (150, 90, 60):
        if T == 60:
            gg = list(g18)
        else:
            raw = []
            for e in ev:
                got = ts.snapshots([e], hr, T)
                if got:
                    r = dict(got[0])
                    r["_k"] = e["start_time"]
                    raw.append(r)
            gg = retag([r for r in raw if r["hot"]], 18)
        gg = [r for r in gg if sgn(r) * r["raw_spot"] > 0]
        med = qq([r["sigusd"] for r in gg], .5)
        print(f"      T={T}s  n={len(gg)}   σ 分界 {med:.0f} 美元")
        for lo, hi in ((40, 63), (63, 90), (90, 130), (130, 200), (200, 10 ** 9)):
            band = [r for r in gg if lo <= sgn(r) * r["raw_spot"] < hi]
            if len(band) < 60:
                continue
            a = [r for r in band if r["sigusd"] < med]
            b = [r for r in band if r["sigusd"] >= med]
            sa, sb = ts.stat(a), ts.stat(b)
            if not sa or not sb or sa["n"] < 15 or sb["n"] < 15:
                continue
            his = "∞" if hi > 10 ** 8 else str(hi)
            print(f"        [{lo},{his}) 低 n={sa['n']:4d} 价 {sa['fill']:.3f} WR {sa['wr']*100:5.1f}% "
                  f"EV {sa['ev']:+.4f}  |  高 n={sb['n']:4d} 价 {sb['fill']:.3f} WR {sb['wr']*100:5.1f}% "
                  f"EV {sb['ev']:+.4f}  |  便宜 {sa['fill']-sb['fill']:+.3f} "
                  f"WR 罚 {(sb['wr']-sa['wr'])*100:+.1f}pp 净 {sb['ev']-sa['ev']:+.4f}")

    print("\n  7.4 σ 周期缩到 5 窗（用户 2026-09-23 提议）—— 三个登记格在 N=5 / N=18 下")
    for N in (5, 18):
        gg = retag(hot60, N)
        print(f"      ── N={N} ──")
        for lab, sel in (("T=60 spot≥1.0σ（主格）", [r for r in gg if r["s_spot"] >= 1.0]),
                         ("T=60 spot≥63美元（对照1）", [r for r in gg if sgn(r) * r["raw_spot"] >= 63]),
                         ("T=60 spot≥100美元", [r for r in gg if sgn(r) * r["raw_spot"] >= 100])):
            st = ts.stat(sel)
            ds = sorted(by_day(sel))
            (plo, phi), (elo, ehi) = day_bootstrap(sel, ds, BOOT, rng)
            print("      " + pad(lab, 26) + pad(f"n={st['n']}", 9) + pad(f"WR {st['wr']*100:.1f}%", 11)
                  + pad(f"EV/股 {st['ev']:+.4f}", 15) + pad(f"P&L {pl(sel):+.1f}U", 13)
                  + f"P&L 95%CI [{plo:+.1f},{phi:+.1f}]")

    # ---- §6 三个登记格的逐日 -----------------------------------------------
    print("\n" + "=" * 100)
    print("§6 登记格逐日（纸面对照用）")
    print("=" * 100)
    for T, leg, kind, th in ((60, "spot", "sig", 1.0), (60, "spot", "usd", 63),
                             (150, "twap", "sig", 1.0)):
        g = select(HOT[T], leg, kind, th)
        d = by_day(g)
        ks = sorted(d)
        print(f"\n  T={T} {leg}≥{th}{'σ' if kind=='sig' else '美元'}  n={len(g)}  "
              f"{len(g)/len(ks):.0f} 注/日  单日最大亏损 {min(pl(d[k]) for k in ks):+.1f}U")
        for k in ks:
            nl = sum(1 for r in d[k] if not r["settle_won"])
            print(f"    {k} n={len(d[k]):3d} 亏损 {nl:2d} 笔 P&L {pl(d[k]):+6.1f}U")


if __name__ == "__main__":
    main()
