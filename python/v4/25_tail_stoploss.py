#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘：**持仓止损界限**搜索 + 翻盘解剖（2026-09-25）

问题两连：① ⑤ 买入热门侧（≥0.80）后，持仓期间「dev 过低」该低到多少才止损？
          ② 翻盘那一刻发生在什么时间、什么价格、伴随多大的 BTC 成交量？

## 一、结构恒等式（其余全是它的推论）

买价 `fill`、出场价 `p`、每股兑 1U（赢）/ 0U（输），`shares = STAKE/fill`：

    留场  赢 = shares − STAKE        输 = −STAKE
    出场      = shares·p − STAKE
    Δ(最终赢) = shares·(p − 1)   ← 杀在赢家上：亏掉「本该拿满的 1」
    Δ(最终输) = shares·p         ← 杀在输家上：捞回残值

⇒ **盈亏平衡所需的「触发集里输家占比」= 1 − p̄**（p̄ = 触发时均出场价）。
   出场价越高越宽容，但价格高＝还没崩 ⇒ 杀赢家概率也高。止损的全部矛盾在这一行里。

## 二、三条硬结论（14 天 2026-08-18~31, 2U/注, n=2135, 基线 +35.67U）

1. **单腿都不行**：仅 `dev < −20` → Δ+7.16（h1 +12.45 / h2 −5.29，半样本翻符号）；
   仅 `bid < 0.30` → Δ+0.83（h1 −5.57 / h2 +6.40）。两腿合用才稳。
2. **可用界限 = 平台区不是尖峰**：`持仓侧 bid < 0.25~0.30 ∧ dev < −20 美元(≈−0.25σ)`
   整片 +8~+11.5U，峰点 `bid<0.25 ∧ dev<−20` = **Δ+11.53U（CI [+2.72,+20.88]）**，
   触发 66/2135 = 3.1%，杀赢家 3 / 救输家 63。但这是 60 格扫描里挑出来的格子。
3. **翻盘由市场先动、现货后认**：持仓侧 bid 下穿 0.5 时（= 市场把持仓判成少数侧）
   现货 dev 仍是 **+10~14 美元（还在押注方向）**——市场领先现货，故 dev 腿天然迟。
   等到两腿都翻（价 ≤0.25 ∧ dev <−20）残值只剩 **0.12~0.14**，之后 rem<30 归零。

## 三、⚠️ 严格性（样本只有 86 个输家，必须按最严的口径读）

* 峰点二项检验 P(X≥63 | p=平衡点 0.878, n=66) ≈ **0.027**——**未做多重比较校正**，
  扫描前定的α=0.05 经 60 格挑选后不成立（Bonferroni ≈ 0.0008）。
* 翻转那一刻**没有任何可用信息能区分输赢家**（AUC 表）：BTC 量 0.485~0.499（无区分）、
  翻转秒 bid 0.580、翻转秒 dev 0.548。能区分的只有**翻转后 3 秒**的读数（AUC 0.65~0.69）——
  那时价格已经又掉了 0.05，是滞后信息。
* **实盘红线**：本模拟假设触发那一秒持仓侧有 bid 可吃。决策 #21 活体取证：尾盘**输家侧
  bid 被整侧撤空**（探针 rem=12/28/45/47；ETH 首采 rem ∈ [0,34] / [0,79]）——止损要出场的
  那一刻正是持仓变输家的那一刻。历史 14 天里持仓侧 bid==0 出现 **0 次**（227k 个持仓后
  tick）——不是市场属性，是旧采集守卫丢弃空侧消息的**构造性产物**（决策 #21/#23）。
  ⇒ 所有 Δ 都是**上界**。

## 四、结论

**不建议把止损落进引擎**：唯一稳健的候选也只在 3.1% 的信号上触发、捞回 13% 的残值、
且依赖一个离线不可验证的「有 bid 可吃」假设。真要落，就用平台中心
`持仓侧 bid < 0.30 ∧ dev < −20 美元`，并按落袋口径重定义该行的胜负。

用法: python/venv/bin/python python/v4/25_tail_stoploss.py
"""
import sys
import math
import random
import datetime
import argparse
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from v2.lib import load_events                                    # noqa: E402

STAKE = 2.0
MAX_LAT = 300
T150, T60 = 150, 60
FIELDS = ("yes_bid", "yes_ask", "no_bid", "no_ask")
FLIP_LV = 0.50                     # 「市场翻盘」判据：持仓侧 bid 下穿此价位
GRID_X = (-35, -30, -25, -20, -15, -10, -5, 0)
GRID_P = (0.40, 0.35, 0.30, 0.25, 0.20, 0.15)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s23 = load("s23", BASE / "23_tail_integrated.py")
s13 = s23.s13


# ── 信号重建（与 oracle 23 逐条同口径，只多带 tick 下标供路径回放） ─────────

def win_ticks_idx(e):
    """本窗可判定 tick + 它在全量 ticks 里的下标（镜像 s23.win_ticks）。"""
    out = []
    anchor = e.get("twap_open_price")
    if not anchor:
        return out
    for i, x in enumerate(e.get("ticks") or []):
        p = x.get("pm") or {}
        if not p or (p.get("book_latency_ms") or 0) > MAX_LAT:
            continue
        rem = x.get("rem")
        if rem is None or rem <= 0 or rem > T150:
            continue
        if not all((p.get(k) or 0) > 0 for k in FIELDS):
            continue
        spot = (x.get("bin") or {}).get("price")
        if not spot:
            continue
        ya, na = p.get("yes_ask") or 0, p.get("no_ask") or 0
        side = "yes" if ya >= na else "no"
        out.append({"rem": rem, "side": side, "fill": (ya if side == "yes" else na),
                    "spot": spot, "twap": (x.get("twap") or {}).get("price"), "i": i})
    return out


def chain_idx(ticks, anchor, sd, date, outcome):
    """镜像 s23.chain，每行带 tick 下标 i。"""
    if not ticks:
        return []
    head = ticks[0]
    if head["rem"] > T60:
        r = s23.row(head, anchor, sd, date, outcome, "t150")
        r["i"] = head["i"]
        if s23.r5(r):
            r["ok"] = True
            return [r]
        rest = [x for x in ticks if x["rem"] <= T60]
    else:
        rest = ticks
    if rest:
        t2 = rest[0]
        r2 = s23.row(t2, anchor, sd, date, outcome, "t60")
        r2["i"] = t2["i"]
        if s23.r5(r2):
            r2["ok"] = True
            return [r2]
        for x in rest[1:]:
            rl = s23.row(x, anchor, sd, date, outcome, "listen")
            rl["i"] = x["i"]
            if s23.r2(rl):
                rl["ok"] = True
                return [rl]
    return []


def load_signals(data_dir):
    ev = load_events(data_dir)
    hr = s13.hist_ranges(ev)
    out = []
    for e in ev:
        outcome, anchor = e.get("outcome"), e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        h = hr.get(e["start_time"])
        if h is None:
            continue                              # σ 未就绪整窗跳过（决策 #13）
        sd = h / anchor * 1e4 * anchor / 1e4        # 与 Go 侧同运算序列（保浮点逐位）
        date = datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")
        for r in chain_idx(win_ticks_idx(e), anchor, sd, date, outcome):
            if r.get("ok"):
                out.append({"e": e, "r": r, "sd": sd, "date": date,
                            "sh": STAKE / r["fill"]})
    return out


def post(s):
    """持仓后可评估 tick 序列 [(rem, dev, 持仓侧 bid, bid 深度)]。

    可评估 = 延迟 ≤ 300ms ∧ spot 在场 ∧ **持仓侧 bid > 0** ∧ rem > 0。
    最后一条是止损的硬前提：没有 bid 就卖不掉（实盘红线）。
    """
    e, r = s["e"], s["r"]
    anchor = e["twap_open_price"]
    sgn = 1.0 if r["side"] == "yes" else -1.0
    bidk, szk = r["side"] + "_bid", r["side"] + "_bid_top5"
    out = []
    for x in e["ticks"][r["i"] + 1:]:
        rem = x.get("rem")
        if rem is None or rem <= 0:
            continue
        p = x.get("pm") or {}
        if not p or (p.get("book_latency_ms") or 0) > MAX_LAT:
            continue
        spot = (x.get("bin") or {}).get("price")
        if not spot:
            continue
        b = p.get(bidk) or 0.0
        if b <= 0:
            continue
        out.append((rem, sgn * (spot - anchor), b, p.get(szk) or 0.0))
    return out


def q(a, p):
    return sorted(a)[min(len(a) - 1, int(p * len(a)))] if a else float("nan")


# ── 止损模拟 ──────────────────────────────────────────────────────────────

def sim(sigs, x_usd=None, p_max=None, k_sigma=None, lv=None, ksec=1):
    """逐笔回放持仓路径。

    触发 = 首个满足 [dev < x_usd 或 dev < k_sigma·sd]（若给）∧ [bid < p_max]（若给）
           且此前已连续 ksec 个可评估 tick 都满足 (bid < lv)（若给 lv）。
    出场 pnl = shares·bid − STAKE；未触发 = 持有到结算。
    """
    det = []
    for s in sigs:
        r, sh = s["r"], s["sh"]
        hold = (sh - STAKE) if r["settle_won"] else -STAKE
        ex, run = None, 0
        for rem, dev, b, sz in post(s):
            hit = True
            if x_usd is not None or k_sigma is not None:
                hit &= (x_usd is not None and dev < x_usd) or \
                       (k_sigma is not None and dev < k_sigma * s["sd"])
            if p_max is not None:
                hit &= b < p_max
            if lv is not None:
                run = run + 1 if b < lv else 0
                hit &= run >= ksec
            if x_usd is None and k_sigma is None and p_max is None and lv is None:
                hit = False
            if hit:
                ex = (rem, dev, b, sz)
                break
        det.append({"s": s, "ex": ex, "hold": hold,
                    "pnl": (sh * ex[2] - STAKE) if ex else hold})
    return det


def brief(det, reps=2000, seed=42):
    fire = [d for d in det if d["ex"]]
    n = len(fire)
    tot = sum(d["pnl"] for d in det) - sum(d["hold"] for d in det)
    d1 = [d for d in det if d["s"]["date"] < "2026-08-25"]
    d2 = [d for d in det if d["s"]["date"] >= "2026-08-25"]
    a1 = sum(d["pnl"] - d["hold"] for d in d1)
    a2 = sum(d["pnl"] - d["hold"] for d in d2)
    byd = {}
    for d in det:
        byd.setdefault(d["s"]["date"], []).append(d)
    rng, days, bs = random.Random(seed), sorted(byd), []
    for _ in range(reps):
        pool = [x for k in (rng.choice(days) for _ in days) for x in byd[k]]
        bs.append(sum(x["pnl"] - x["hold"] for x in pool))
    bs.sort()
    pbar = sum(d["ex"][2] for d in fire) / n if n else 0.0
    nlose = n - sum(1 for d in fire if d["s"]["r"]["settle_won"])
    f = nlose / n if n else 0.0
    # 二项检验：精度是否显著高于平衡点 1−p̄（单侧正态近似）
    star = 1 - pbar
    z = 0.0
    if n and 0 < star < 1:
        se = math.sqrt(star * (1 - star) / n)
        z = (f - star) / se if se else 0.0
    return dict(n=n, nw=n - nlose, nl=nlose, pbar=pbar, prec=f, star=star,
                edge=f - (1 - pbar), z=z, d=tot, d1=a1, d2=a2,
                ci=(bs[int(.025 * len(bs))], bs[int(.975 * len(bs))]),
                dpos=sum(1 for v in byd.values()
                         if sum(x["pnl"] - x["hold"] for x in v) > 0),
                nday=len(byd))


def line(lab, b):
    if b["n"] == 0:
        print(f"  {lab:<24} 零触发")
        return
    print(f"  {lab:<24} n={b['n']:<4} 杀赢 {b['nw']:<3} 救输 {b['nl']:<3} 均出场 {b['pbar']:.3f} "
          f"精度 {b['prec']*100:5.1f}%（平衡需 {b['star']*100:5.1f}%）z={b['z']:+.2f} "
          f"Δ {b['d']:+6.2f} h1 {b['d1']:+6.2f} h2 {b['d2']:+6.2f} "
          f"CI [{b['ci'][0]:+6.2f},{b['ci'][1]:+6.2f}] 日正 {b['dpos']}/{b['nday']}")


def main():
    ap = argparse.ArgumentParser(description="扫尾盘持仓止损界限搜索 + 翻盘解剖")
    ap.add_argument("--data", default="data/btc")
    ap.add_argument("--flip", type=float, default=FLIP_LV, help="翻盘判据（持仓侧 bid 下穿）")
    args = ap.parse_args()

    sigs = load_signals(args.data)
    W = [s for s in sigs if s["r"]["settle_won"]]
    L = [s for s in sigs if not s["r"]["settle_won"]]
    PATH = {id(s): post(s) for s in sigs}
    base = sum(s23.pl([s["r"]]) for s in sigs)
    win_u = sum((s["sh"] - STAKE) for s in W)
    los_u = -STAKE * len(L)

    print("=" * 100)
    print("零、基线与 P&L 来源（止损值不值得做，取决于亏损端有多大）")
    print("=" * 100)
    print(f"  信号 n = {len(sigs)}  WR = {len(W)/len(sigs)*100:.2f}%  基线 P&L = {base:+.2f}U"
          f"   （+{win_u:.1f}U 赢 − {abs(los_u):.1f}U 输）")
    print(f"  ⇒ 亏损端 {abs(los_u):.0f}U = 赢利端 {win_u:.0f}U 的 {abs(los_u)/win_u*100:.0f}%："
          f"**单笔亏 −2U / 单笔赢 +0.10U**，砍亏损笔是这条策略唯一的 P&L 杠杆。")
    sds = sorted(s["sd"] for s in sigs)
    print(f"  σ(sd) 分位 p25 {sds[len(sds)//4]:.1f} / p50 {sds[len(sds)//2]:.1f} / "
          f"p75 {sds[len(sds)*3//4]:.1f} 美元")

    # ── 一、持仓后路径 ────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("一、持仓后路径：dev 与持仓侧 bid 各走到哪（赢家 vs 输家）")
    print("=" * 100)
    for lab, arr in (("最终赢家", W), ("最终输家", L)):
        ds = [min(t[1] for t in PATH[id(s)]) for s in arr if PATH[id(s)]]
        ps = [min(t[2] for t in PATH[id(s)]) for s in arr if PATH[id(s)]]
        print(f"  {lab} n={len(ds):<5} min-dev p1 {q(ds,.01):+7.1f} p5 {q(ds,.05):+7.1f} "
              f"p10 {q(ds,.10):+7.1f} p25 {q(ds,.25):+7.1f} p50 {q(ds,.50):+7.1f}   "
              f"min-bid p10 {q(ps,.10):.3f} p25 {q(ps,.25):.3f} p50 {q(ps,.50):.3f}")
    npost = sum(len(PATH[id(s)]) for s in sigs)
    print(f"  持仓后 tick {npost} 个，其中持仓侧 bid == 0 的 0 个"
          f" ← ⚠️ 构造性 0（旧守卫丢空侧消息，决策 #21），实盘不是这样")

    # ── 二、翻盘解剖 ──────────────────────────────────────────────────
    lv = args.flip
    print("\n" + "=" * 100)
    print(f"二、翻盘解剖：持仓侧 bid 下穿 {lv} 的时间与价格（用户重点）")
    print("=" * 100)
    print(f"  {'下穿':>6}{'输家命中':>9}{'赢家命中':>9}{'输家rem中位':>12}{'赢家rem中位':>12}"
          f"{'输家下穿价中位':>15}{'输家dev中位':>12}{'赢家dev中位':>12}")
    for L2 in (0.90, 0.80, 0.70, 0.60, 0.50, 0.40, 0.30, 0.20, 0.10):
        def cross(s):
            for rem, dev, b, sz in PATH[id(s)]:
                if b < L2:
                    return (rem, dev, b)
            return None
        cl = [c for c in (cross(s) for s in L) if c]
        cw = [c for c in (cross(s) for s in W) if c]
        print(f"  {L2:>6.2f}{len(cl):>9}{len(cw):>9}{q([c[0] for c in cl],.5):>12.0f}"
              f"{q([c[0] for c in cw],.5):>12.0f}{q([c[2] for c in cl],.5):>15.3f}"
              f"{q([c[1] for c in cl],.5):>12.1f}{q([c[1] for c in cw],.5):>12.1f}")
    print(f"  ⇒ 每个输家都下穿过 {lv}（86/86），赢家只有 {sum(1 for s in W if any(b<lv for _,_,b,_ in PATH[id(s)]))}"
          f" 个假下穿 = 假信号率 {sum(1 for s in W if any(b<lv for _,_,b,_ in PATH[id(s)]))/len(W)*100:.2f}%")
    fL = [next(((rem, dev, b) for rem, dev, b, sz in PATH[id(s)] if b < lv), None) for s in L]
    fL = [c for c in fL if c]
    rms = sorted(c[0] for c in fL)
    print(f"  输家下穿 rem 分布: p10 {q(rms,.1):.0f} p25 {q(rms,.25):.0f} p50 {q(rms,.5):.0f} "
          f"p75 {q(rms,.75):.0f} p90 {q(rms,.9):.0f}   ← 中位 = 闭市前 {q(rms,.5):.0f} 秒")
    print(f"  下穿那一秒 **现货 dev 仍是 +{q([c[1] for c in fL],.5):.1f} 美元**（还在押注方向）"
          f"——市场领先现货，dev 腿天然迟。")

    print("\n  [滑下去还是跳下去：以下穿那一秒为 0 的 bid 中位轨迹]")
    print(f"  {'相对秒':>7}{'输家bid中位':>12}{'赢家bid中位':>12}")
    for k in range(-6, 7):
        def at(s, c, k):
            p = PATH[id(s)]
            idx = next((i for i, t in enumerate(p) if t[0] <= c[0]), None)
            return p[idx + k][2] if idx is not None and 0 <= idx + k < len(p) else None
        vl = [v for v in (at(s, c, k) for s, c in ((s, next(((r, d, b) for r, d, b, _ in PATH[id(s)] if b < lv), None)) for s in L) if c) if v is not None]
        vw = [v for v in (at(s, c, k) for s, c in ((s, next(((r, d, b) for r, d, b, _ in PATH[id(s)] if b < lv), None)) for s in W) if c) if v is not None]
        print(f"  {k:>+7}{q(vl,.5):>12.3f}{q(vw,.5):>12.3f}")
    print("  ⇒ 下穿那一秒两边几乎同价（判别量不在这一刻），分歧从 +2s 才开始")

    print("\n  [输家残值：每个 rem 区间**起始**的持仓侧 bid 中位（决定止损能捞回多少）]")
    print(f"  {'rem区间':>10}{'输家n':>7}{'输家bid中位':>12}{'赢家n':>7}{'赢家bid中位':>12}")
    for lo_, hi_ in ((150, 120), (120, 90), (90, 60), (60, 30), (30, 10), (10, 0)):
        vl = [next((b for rem, d, b, _ in PATH[id(s)] if lo_ >= rem > hi_), None) for s in L]
        vw = [next((b for rem, d, b, _ in PATH[id(s)] if lo_ >= rem > hi_), None) for s in W]
        vl = [v for v in vl if v is not None]
        vw = [v for v in vw if v is not None]
        print(f"  {f'{hi_}-{lo_}':>10}{len(vl):>7}{q(vl,.5):>12.3f}{len(vw):>7}{q(vw,.5):>12.3f}")
    print("  ⇒ 输家残值在 rem 60→30 之间从 ~0.44 掉到 ~0.01：**止损必须赶在 rem>30 出手**")

    # ── 三、翻盘前后的 BTC 成交量 ─────────────────────────────────────
    print("\n" + "=" * 100)
    print("三、翻盘前后的 **BTC 现货成交量**（tick 的 bin.buy_vol/sell_vol/ticks 逐秒聚合）")
    print("=" * 100)
    TK = {id(s): {x.get("rem"): x for x in s["e"]["ticks"]} for s in sigs}

    def flip_rem(s):
        for rem, dev, b, sz in PATH[id(s)]:
            if b < lv:
                return rem
        return None
    FB = {id(s): flip_rem(s) for s in sigs}

    def show(lab, arr):
        acc = {k: {"adv": [], "fav": [], "ticks": [], "dev": []} for k in range(-8, 9)}
        for s in arr:
            f = FB[id(s)]
            if f is None:
                continue
            yes = s["r"]["side"] == "yes"
            for k in range(-8, 9):
                x = TK[id(s)].get(f - k)
                if not x:
                    continue
                bi = x.get("bin") or {}
                bv, sv = bi.get("buy_vol") or 0, bi.get("sell_vol") or 0
                acc[k]["adv"].append(sv if yes else bv)      # 对持仓不利的主动流
                acc[k]["fav"].append(bv if yes else sv)
                acc[k]["ticks"].append(bi.get("ticks") or 0)
                acc[k]["dev"].append((1 if yes else -1) * ((bi.get("price") or 0)
                                                           - s["e"]["twap_open_price"]))
        print(f"\n  {lab} n={sum(1 for s in arr if FB[id(s)] is not None)}")
        print(f"  {'相对秒':>7}{'不利主动量BTC':>14}{'有利主动量':>12}{'成交笔数':>10}{'dev(美元)':>11}")
        for k in range(-8, 9):
            a = acc[k]
            mark = "  ← 下穿" if k == 0 else ""
            print(f"  {k:>+7}{q(a['adv'],.5):>14.4f}{q(a['fav'],.5):>12.4f}"
                  f"{q(a['ticks'],.5):>10.1f}{q(a['dev'],.5):>11.1f}{mark}")
    show("【最终输家】", L)
    show("【最终赢家·假下穿】", W)

    def ratio(s):
        f = FB[id(s)]
        if f is None:
            return None
        allv = [(x.get("bin") or {}) for x in s["e"]["ticks"]]
        med = q([(v.get("buy_vol") or 0) + (v.get("sell_vol") or 0) for v in allv], .5)
        x = TK[id(s)].get(f)
        if not x or not med:
            return None
        bi = x.get("bin") or {}
        return ((bi.get("buy_vol") or 0) + (bi.get("sell_vol") or 0)) / med
    for lab, arr in (("输家", L), ("赢家", W)):
        r = [v for v in (ratio(s) for s in arr) if v is not None]
        print(f"  {lab} 下穿那一秒总量/本窗中位秒量: p25 {q(r,.25):.2f}× p50 {q(r,.5):.2f}× "
              f"p75 {q(r,.75):.2f}× p90 {q(r,.9):.2f}×")
    print("  ⇒ 下穿那一秒确实是爆量（~7× 中位），但**赢家输家一模一样** ⇒ 量本身不含判别信息")

    # ── 四、翻转特征的判别力（AUC） ───────────────────────────────────
    print("\n" + "=" * 100)
    print("四、翻转那一刻可用的信息，能不能区分「真翻盘 / 假摔」？（AUC）")
    print("=" * 100)
    FL = []
    for s in sigs:
        f = FB[id(s)]
        if f is None:
            continue
        yes = s["r"]["side"] == "yes"
        sgn = 1 if yes else -1
        anchor = s["e"]["twap_open_price"]

        def bt(rem):
            x = TK[id(s)].get(rem)
            if not x:
                return None
            bi = x.get("bin") or {}
            return ((bi.get("buy_vol") or 0), (bi.get("sell_vol") or 0),
                    (bi.get("ticks") or 0), (bi.get("price") or 0))
        a = bt(f)
        if not a:
            continue
        adv = lambda t: (t[1] if yes else t[0])                          # noqa: E731
        allv = [(x.get("bin") or {}) for x in s["e"]["ticks"]]
        med = q([(v.get("buy_vol") or 0) + (v.get("sell_vol") or 0) for v in allv], .5)
        medn = q([(v.get("ticks") or 0) for v in allv], .5)
        five = [t for t in (bt(f - k) for k in (-2, -1, 0, 1, 2)) if t]
        px = {r: bb for r, d, bb, sz in PATH[id(s)]}
        b6, a3 = bt(f + 6), bt(f - 3)
        FL.append(({
            "翻转秒总量比": ((a[0] + a[1]) / med) if med else 0.0,
            "翻转秒不利量比": (adv(a) / med) if med else 0.0,
            "翻转秒笔数比": a[2] / max(1, medn),
            "±2s不利量累计比": (sum(adv(t) for t in five) / (5 * med)) if med else 0.0,
            "翻转秒dev": sgn * (a[3] - anchor),
            "翻转前6秒现货跌幅": sgn * (a[3] - b6[3]) if b6 else 0.0,
            "翻转秒rem": f,
            "翻转秒bid": px.get(f) or 0.0,
            "入场fill": s["r"]["fill"],
            "入场dev": s["r"]["dev"],
            "翻转后3秒bid": px.get(f - 3) or 0.0,          # ← 决策点之后（滞后）
            "翻转后3秒dev变化": sgn * (a3[3] - a[3]) if a3 else 0.0,
        }, bool(s["r"]["settle_won"])))
    nW = sum(1 for _, w in FL if w)
    nL = len(FL) - nW

    def auc(vals, labs):
        pairs, n = sorted(zip(vals, labs)), len(vals)
        r, i = [0.0] * n, 0
        while i < n:
            j = i
            while j + 1 < n and pairs[j + 1][0] == pairs[i][0]:
                j += 1
            for k in range(i, j + 1):
                r[k] = (i + j) / 2 + 1
            i = j + 1
        pos = [r[k] for k in range(n) if pairs[k][1]]
        neg = [r[k] for k in range(n) if not pairs[k][1]]
        return 0.5 if not pos or not neg else \
            (sum(pos) / len(pos) - (len(pos) + 1) / 2) / len(neg)
    print(f"  翻转样本 n={len(FL)}（输家 {nL} / 赢家 {nW}）; AUC=0.5 无区分, label=1 是赢家")
    print(f"  {'特征':<20}{'AUC':>7}{'输家中位':>12}{'赢家中位':>12}   解读")
    for k in FL[0][0]:
        a_ = auc([f[k] for f, _ in FL], [w for _, w in FL])
        lo_ = q([f[k] for f, w in FL if not w], .5)
        hi_ = q([f[k] for f, w in FL if w], .5)
        tag = "**无区分**" if abs(a_ - 0.5) < 0.06 else ("输家更高" if a_ < 0.5 else "赢家更高")
        print(f"  {k:<20}{a_:>7.3f}{lo_:>12.3f}{hi_:>12.3f}   {tag}")
    print("  ⇒ 决策点（下穿那一秒）**没有一个可用特征能区分**：BTC 量 ~0.49（纯噪声）、"
          "翻转秒 bid 0.58、翻转秒 dev 0.55。")
    print("     能区分的（bid 0.69 / dev变化 0.65）都在**下穿后 3 秒**——那时价格又掉了 0.05。")

    # ── 五、止损界限扫描 ──────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("五、止损界限扫描（Δ = 相对「持有到结算」基线的 P&L 变化，单位 U/14 天）")
    print("=" * 100)
    print("  [单腿：仅 dev / 仅价格]")
    for X in (-60, -40, -20, -10, 0, 20):
        line(f"dev < {X}", brief(sim(sigs, x_usd=X)))
    for P in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.50, 0.60):
        line(f"bid < {P:.2f}", brief(sim(sigs, p_max=P)))

    print("\n  [两腿合用：持仓侧 bid < P ∧ dev < X]")
    print(f"  Δ 敏感性图（列 = dev 阈值 USD，行 = 价格阈值）")
    print(f"  {'P\\X':>7}" + "".join(f"{x:>9}" for x in GRID_X))
    for P in GRID_P:
        row = [sum(d["pnl"] - d["hold"] for d in sim(sigs, x_usd=X, p_max=P))
               for X in GRID_X]
        print(f"  {P:>7.2f}" + "".join(f"{v:>+9.2f}" for v in row))
    print("  （整片同号 = 平台；单点凸起 = 噪声尖峰）")
    print()
    for P in (0.30, 0.25):
        for X in (-30, -25, -20, -15, -10):
            line(f"bid<{P:.2f} ∧ dev<{X}", brief(sim(sigs, x_usd=X, p_max=P)))
        print()

    print("  [翻转持续 K 秒才出（下穿 0.5 后连续 K 个可评估 tick 都在 0.5 之下）]")
    for L2 in (0.60, 0.50, 0.40):
        for K in (1, 3, 5, 8):
            line(f"bid<{L2:.2f} ×{K}s", brief(sim(sigs, lv=L2, ksec=K)))
        print()

    # ── 六、严格性检验 ────────────────────────────────────────────────
    print("=" * 100)
    print("六、严格性：候选规则的稳健性（样本只有 86 个输家，逐条卡）")
    print("=" * 100)
    for lab, kw in (("bid<0.30 ∧ dev<−20", dict(x_usd=-20, p_max=0.30)),
                    ("bid<0.25 ∧ dev<−20", dict(x_usd=-20, p_max=0.25)),
                    ("bid<0.30 ∧ dev<−0.25σ", dict(k_sigma=-0.25, p_max=0.30))):
        det = sim(sigs, **kw)
        b = brief(det)
        line(lab, b)
        # 留一天法
        byd = {}
        for d in det:
            byd.setdefault(d["s"]["date"], []).append(d)
        loo = []
        for k in sorted(byd):
            rest = [x for kk, v in byd.items() if kk != k for x in v]
            loo.append(sum(x["pnl"] - x["hold"] for x in rest))
        # 逐日
        per = " ".join(f"{k[5:]} {sum(x['pnl']-x['hold'] for x in v):+.1f}/{len(v)}笔"
                       for k, v in sorted(byd.items()))
        print(f"     二项 z={b['z']:+.2f}（p≈{(1-0.5*(1+math.erf(abs(b['z'])/math.sqrt(2)))):.3f}，"
              f"未做多重比较校正）; 留一天法最小 Δ {min(loo):+.2f}U;")
        print(f"     逐日 Δ/笔数: {per}")

    # ── 七、实盘红线 ──────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("七、⚠️ 实盘红线：出场那一秒有没有对手方")
    print("=" * 100)
    det = sim(sigs, x_usd=-20, p_max=0.30)
    rms2 = sorted(d["ex"][0] for d in det if d["ex"])
    szs = sorted(d["ex"][3] for d in det if d["ex"])
    print(f"  触发时点 rem: p10 {q(rms2,.1):.0f} p25 {q(rms2,.25):.0f} p50 {q(rms2,.5):.0f} "
          f"p75 {q(rms2,.75):.0f} p90 {q(rms2,.9):.0f}")
    print(f"  触发时 bid 深度（股, top5）p10 {q(szs,.1):.0f} p50 {q(szs,.5):.0f} — 历史数据里够吃")
    print("  但决策 #21 活体取证：尾盘**输家侧 bid 被整侧撤空**（探针 rem=12/28/45/47；")
    print("  ETH 首采 rem ∈ [0,34] / [0,79]）——止损要出场那一刻正是持仓变输家那一刻。")
    print("  历史里持仓侧 bid==0 出现 0 次 = 构造性产物 ⇒ **所有 Δ 都是上界**，")
    print("  实盘能收回多少离线无法验证，须先纸面实跑记账（或 bookprobe 单独取证）。")

    print("\n" + "=" * 100)
    print("八、结论")
    print("=" * 100)
    print("  1. 界限：**dev < −20 美元（≈−0.25σ）∧ 持仓侧 bid < 0.25~0.30**（平台中心取 0.30）")
    print("     —— 单腿都不成立，必须两腿同时确认。触发 3.1%，Δ 上界 +10~+11.5U。")
    print("  2. 时点与价格：输家下穿 0.5 在闭市前中位 75 秒、价 0.44，**那一刻现货仍在押注侧**")
    print("     （dev +10），市场领先现货；等到两腿都确认（价 0.14）残值已所剩无几，")
    print("     rem<30 归零 ⇒ 这是「精度↑必然残值↓」的结构性两难。")
    print("  3. BTC 量：下穿那一秒是 7× 中位爆量，但赢家输家同幅（AUC 0.49）⇒ 不构成触发条件。")
    print("  4. 严格结论：**不建议落引擎**。峰点二项 p≈0.03 未经 60 格多重比较校正、")
    print("     实盘可执行性未验证（输家侧无 bid 风险）、且按落袋口径 WR 必然下降")
    print("     （官方 outcome 口径则 WR 不变，但会出现「赢却负 P&L」的行，须先定口径）。")


if __name__ == "__main__":
    main()
