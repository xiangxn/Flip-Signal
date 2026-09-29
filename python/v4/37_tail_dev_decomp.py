#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘：**dev 的两项分解**——`spot − twap`（缺口）与 `twap − twap_open`（已结算位移）

用户命题（2026-09-29）：

    「当前 dev = spot − twap_open。你再看看，在**翻转亏损**的那些单里，
      `spot − twap`、`twap − twap_open` 是否有特征？」

两个量正好把 `dev` 拆成两项（带符号，符号按持仓方向）：

    dev      = sgn·(spot − anchor)          ← 引擎现行口径（现货）
      ├─ basis = sgn·(spot − feed_now)      ← 用户说的 `spot − twap`：**还没写进结算线的缺口**
      └─ walk  = sgn·(feed_now − anchor)    ← 用户说的 `twap − twap_open`：**已经写进结算线的位移**

恒等式 `dev ≡ basis + walk` 逐 tick 成立。这个拆法不是记号游戏——
结算值是 feed 在收盘那一刻的瞬时值（36 号 §1 已证：不是末 60 秒均值），
所以只有 `walk` 那部分**真的落在账上**，`basis` 要等 feed 自己走过来才算数。

## 本脚本回答四件事

- §2 入场那一刻的分解：输单和赢单的构成差在哪（分 stage）
- §3 持有全程的分解轨迹：输单是**哪一项先动**
- §4 领先性：把每条的「首次穿越」按 rem 排出来，看分解能不能抢在报价前面
- §5 能不能变成钱：i) 当止损/出场触发（对照 36 号现行规则）
                   ii) 当入场闸（控制成交价/σ/段 后还有没有残差；分半稳不稳）

用法:
  python/venv/bin/python python/v4/37_tail_dev_decomp.py
"""
import sys
import math
import random
import collections
import importlib.util
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
STAKE = 2.0


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s36 = load("s36", BASE / "36_tail_stoploss_timing.py")     # 路径缓存 + 回放器 + 置换/bootstrap


def sigof(rows):
    return [r for r in rows if r["ok"]]


# ── §0 口径自检 ───────────────────────────────────────────────────────────

def sec0(rows):
    sig = sigof(rows)
    base = 0.0
    for r in sig:
        sh = STAKE / r["fill"]
        base += (sh - STAKE) if r["won"] else -STAKE
    print("=" * 100)
    print("§0 口径自检（必须与 oracle 23 现行 pin 逐位一致）")
    wr = sum(1 for r in sig if r["won"]) / len(sig) * 100
    print(f"   信号 n={len(sig)}（pin 2133）  WR {wr:.6f}%（pin 96.061885%）  "
          f"P&L {base:+.6f}U（pin +35.092748）"
          f"   {'✅' if len(sig) == 2133 and abs(base - 35.092748) < 1e-4 else '❌'}")
    return sig


# ── §1 分解恒等式 ────────────────────────────────────────────────────────

def sec1(sig):
    print("\n" + "=" * 100)
    print("§1 分解恒等式  dev ≡ basis + walk（逐 tick 残差最大值）")
    mx = 0.0
    for r in sig:
        p = r["path"]
        m = p["bm"] < 0
        mx = max(mx, float(np.abs(p["dev"][m] - (p["basis"][m] + p["devf"][m])).max()))
    print(f"   max |dev − (basis + walk)| = {mx:.2e}")
    print("   basis = sgn·(spot − feed_now)（**尚未**写进结算线的缺口）   "
          "walk = sgn·(feed_now − anchor)（**已经**写进结算线的位移）")


# ── §2 入场那一刻的分解 ─────────────────────────────────────────────────

def sec2(sig):
    print("\n" + "=" * 100)
    print("§2 入场那一刻的分解（中位数；walk/basis 单位 = 美元，已按持仓方向带符号）")
    print("   组               n     dev0     walk0    basis0    fill      sd     rem0")
    for st in ("总", "t150", "t60", "listen"):
        for wn, nm in ((True, "赢"), (False, "输")):
            g = [r for r in sig if r["won"] == wn and (st == "总" or r["stage"] == st)]
            if len(g) < 3:
                continue
            med = lambda f: np.median([f(r) for r in g])                # noqa: E731
            print(f"   {st + '-' + nm:<10} {len(g):>5} {med(lambda r: r['dev0']):>8.1f} "
                  f"{med(lambda r: r['path']['devf'][0]):>8.1f} "
                  f"{med(lambda r: r['path']['basis'][0]):>8.1f} "
                  f"{med(lambda r: r['fill']):>7.3f} {med(lambda r: r['sd']):>7.1f} "
                  f"{med(lambda r: r['rem0']):>6.0f}")
    print("   注: dev0 两组几乎一样（规则要求 ≥63），差的是**构成**——"
          "输单的 walk 更小、basis 更大（= 位移更新鲜、还没被结算线确认）")


# ── §3 持有全程的分解轨迹 ────────────────────────────────────────────────

RTAB = ((120, 151), (90, 120), (60, 90), (40, 60), (20, 40), (0, 20))


def sec3(sig):
    print("\n" + "=" * 100)
    print("§3 持有全程：按 rem 桶（窗内先取均值，再跨窗平均）——输单**哪一项先动**")
    for st in ("t150", "t60"):
        print(f"\n   stage = {st}")
        print("     rem桶    |  赢 walk  赢 basis  赢 bid |  输 walk  输 basis  输 bid | n赢/n输")
        for lo, hi in RTAB:
            acc = {"w": [[], [], []], "l": [[], [], []]}
            for r in sig:
                if r["stage"] != st:
                    continue
                p = r["path"]
                m = p["bm"] < 0
                rem = p["rem"][m]
                s = (rem >= lo) & (rem < hi)
                if not s.sum():
                    continue
                k = "w" if r["won"] else "l"
                for j, key in enumerate(("devf", "basis", "bid")):
                    acc[k][j].append(float(p[key][m][s].mean()))
            if not acc["w"][0] or not acc["l"][0]:
                continue
            f = lambda j, k: np.mean(acc[k][j])                          # noqa: E731
            print(f"     {lo:>3}~{hi:<4} | {f(0,'w'):>9.1f} {f(1,'w'):>8.1f} {f(2,'w'):>7.3f} | "
                  f"{f(0,'l'):>8.1f} {f(1,'l'):>8.1f} {f(2,'l'):>7.3f} | "
                  f"{len(acc['w'][0])}/{len(acc['l'][0])}")
    print("\n   读法（t150）：赢单 walk 单调上行（105→132）、basis 是 +11~+18 的小正数；")
    print("                 输单 **basis 先翻负**（rem 90~120 已 −11，那一刻 dev 还有 +57、")
    print("                 按 dev 腿看完全『安全』，但报价已经从 0.86 掉到 0.75），")
    print("                 随后 **walk 才跟着往下走**（rem 60~90 掉到 +49、rem 0~10 到 −31）。")


# ── §4 领先性：首次穿越的 rem ────────────────────────────────────────────

CONDS = (
    ("basis < −20（缺口先翻）", lambda c, i: c["basis"][i] < -20.0),
    ("basis < −60", lambda c, i: c["basis"][i] < -60.0),
    ("walk < +20", lambda c, i: c["devf"][i] < 20.0),
    ("walk < 0（结算线翻负）", lambda c, i: c["devf"][i] < 0.0),
    ("dev < −20（= 现行腿）", lambda c, i: c["dev"][i] < -20.0),
    ("bid < 0.80", lambda c, i: c["bid"][i] < 0.80),
    ("bid < 0.60", lambda c, i: c["bid"][i] < 0.60),
    ("bid < 0.30", lambda c, i: c["bid"][i] < 0.30),
)


def sec4(sig):
    print("\n" + "=" * 100)
    print("§4 领先性：每窗「首次满足」的 rem（越大 = 越早）与该刻的报价")
    print("   条件                       |  赢 n 中位rem 该刻bid |  输 n 中位rem 该刻bid")
    for tag, f in CONDS:
        line = f"   {tag:<26} |"
        for wn in (True, False):
            rr, bb = [], []
            for r in sig:
                if r["won"] != wn:
                    continue
                c = s36.ctx_of(r)
                for i in range(len(c["rem"])):
                    if f(c, i):
                        rr.append(c["rem"][i])
                        bb.append(c["bid"][i])
                        break
            line += (f" {len(rr):>5} {np.median(rr) if rr else float('nan'):>7.0f} "
                     f"{np.median(bb) if bb else float('nan'):>7.3f} |")
        print(line)
    print("\n   两条读法：")
    print("   ① 输单：basis 翻负在 rem 113（那一刻 bid 还有 **0.73**），而 bid<0.80 在 rem 112")
    print("      ⇒ **分解并不领先报价**，市场拿的是同一批信息，同时动手。")
    print("   ② 赢单：`basis<−20` 也会命中 949 次（46%），而且命中时 bid 中位 **0.98**")
    print("      ⇒ 单看 basis 的水平毫无判别力（结构性水平差，日变；见 09-26 那条记忆）。")


# ── §5 能不能变成钱 ──────────────────────────────────────────────────────

def exact_fill_perm(key, y, fill, sub=None, nperm=4000, seed=37, minn=8):
    """**精确成交价**分层（不做分箱、不受 tie 影响），层内按 key 中位数分组；置换检验。"""
    idx = np.where(sub)[0] if sub is not None else np.arange(len(y))
    kk, yy, ff = key[idx], y[idx], fill[idx]
    hi = (kk >= np.median(kk))
    labels = {}
    for f in sorted(set(ff.round(4))):
        s = ff.round(4) == f
        if s.sum() < 40:
            continue
        ix = np.where(s)[0]
        if hi[ix].sum() < minn or (~hi[ix]).sum() < minn:
            continue
        labels[f] = ix

    def stat(h):
        W = S = 0.0
        for _, ix in labels.items():
            lo = [j for j in ix if not h[j]]
            hh = [j for j in ix if h[j]]
            if len(lo) < minn or len(hh) < minn:
                continue
            w = len(lo) * len(hh) / (len(lo) + len(hh))
            W += w * (yy[hh].mean() - yy[lo].mean())
            S += w
        return (W / S * 100 if S else float("nan")), len(labels)

    obs, nl = stat(hi)
    rnd = random.Random(seed)
    cnt = 0
    for _ in range(nperm):
        h = list(hi)
        for _, ix in labels.items():
            v = [h[j] for j in ix]
            rnd.shuffle(v)
            for j, x in zip(ix, v):
                h[j] = x
        s, _ = stat(h)
        if abs(s) >= abs(obs):
            cnt += 1
    return obs, (cnt + 1) / (nperm + 1), nl


def binned_ctrl(key, arrs, y, minn=15, tag=""):
    """分箱控制版（与精确 fill 版互补）：格内按 key 中位数分两组，打印每格明细。"""
    qs = []
    for a, nb in arrs:
        q = np.quantile(a, np.linspace(0, 1, nb + 1))
        q[0] -= 1e-9
        q[-1] += 1e-9
        qs.append(np.searchsorted(q[1:-1], a))
    gid = list(zip(*[c.tolist() for c in qs]))
    hi = key >= np.median(key)
    cell = collections.defaultdict(lambda: [[0, 0], [0, 0]])
    for j, g in enumerate(gid):
        cell[g][1 if hi[j] else 0][y[j]] += 1
    print(f"   {tag}（格内按 {len(qs)} 维分箱；n≥{minn} 才计入）")
    W = S = 0.0
    tl = nlt = th = nht = 0
    for g, (a, b) in sorted(cell.items()):
        nl, nh = sum(a), sum(b)
        if nl < minn or nh < minn:
            continue
        pl, ph = a[1] / nl, b[1] / nh
        w = nl * nh / (nl + nh)
        W += w * (ph - pl)
        S += w
        tl += a[1]
        nlt += nl
        th += b[1]
        nht += nh
        print(f"     格{g} n低{nl:>5} {pl*100:6.2f}% | n高{nh:>4} {ph*100:6.2f}% | "
              f"{(ph-pl)*100:+6.2f}pp  权重{w:5.1f}")
    print(f"     加权 Δ = {W/S*100:+.2f}pp   池化: 低 {tl/nlt*100:.2f}%(n={nlt})  "
          f"高 {th/nht*100:.2f}%(n={nht})")


def sec5(sig):
    W0 = np.array([r["path"]["devf"][0] for r in sig])
    B0 = np.array([r["path"]["basis"][0] for r in sig])
    DEV0 = np.array([r["dev0"] for r in sig])
    FILL = np.array([r["fill"] for r in sig])
    SD0 = np.array([r["sd"] for r in sig])
    Y = np.array([0 if r["won"] else 1 for r in sig])
    DATE = np.array([str(r["date"]) for r in sig])

    print("\n" + "=" * 100)
    print("§5a 入场闸：按入场 walk 十分位（n=2133）")
    print("     分位区间(美元)      n    输率   均 fill    P&L合计    均笔")
    q = np.quantile(W0, np.linspace(0, 1, 11))
    for i in range(10):
        s = (W0 >= q[i]) & (W0 < q[i + 1] if i < 9 else W0 <= q[i + 1])
        g = [r for j, r in enumerate(sig) if s[j]]
        L = [r for r in g if not r["won"]]
        tot = sum((STAKE / r["fill"] - STAKE) if r["won"] else -STAKE for r in g)
        print(f"     [{q[i]:7.1f},{q[i+1]:7.1f}) {len(g):>5} {len(L)/len(g)*100:6.2f}% "
              f"{np.mean([r['fill'] for r in g]):8.3f} {tot:+9.1f} {tot/len(g):+8.3f}")
    print("     ⇒ 只有**最低三档（walk0 < ~43）**是负的；断点两侧 6.6% → 2.8%")

    print("\n§5b 入场闸的钱：walk0 ≥ X 才下单（日级配对 bootstrap 95%）")
    days = sorted(set(DATE))
    mid = days[len(days) // 2]
    for X in (20.0, 43.0, 60.0):
        for tag, sub in (("全体", np.ones(len(sig), bool)),
                         (f"h1(<{mid})", DATE < mid), (f"h2", DATE >= mid)):
            keep = [r for j, r in enumerate(sig) if sub[j] and r["path"]["devf"][0] >= X]
            allr = [r for j, r in enumerate(sig) if sub[j]]
            by_day = collections.defaultdict(float)
            for r in allr:
                if r["path"]["devf"][0] < X:
                    by_day[r["date"]] -= ((STAKE / r["fill"] - STAKE) if r["won"] else -STAKE)
            lo, hi = s36.boot_day(by_day, 2000)
            pnl = lambda g: sum((STAKE / r["fill"] - STAKE) if r["won"] else -STAKE for r in g)   # noqa: E731
            sk = [r for r in allr if r["path"]["devf"][0] < X]
            print(f"     X={X:5.1f} {tag:<11} 保留 {len(keep):>4}/{len(allr):<4} Δ "
                  f"{pnl(keep) - pnl(allr):+7.2f}U [{lo:+6.2f},{hi:+6.2f}]  "
                  f"跳过组输率 {sum(1 for r in sk if not r['won'])/max(1,len(sk))*100:5.2f}%")

    print("\n§5c 但**控制成交价之后还有没有残差**（精确 fill 分层，不做分箱）")
    print("     量            Δ(输率 高组−低组)   置换 p    价位层数")
    for nm, k in (("walk0", W0), ("basis0", B0), ("dev0（对照）", DEV0)):
        o, p, nl = exact_fill_perm(k, Y, FILL)
        print(f"     {nm:<14} {o:+8.2f}pp        {p:.4f}    {nl}")
    print("     ⇒ dev 全量已被报价吃进（p 0.79）；walk/basis 的**构成**还剩 ≈2pp 残差。")
    print("\n     互补版（分箱控制，格内按 walk0 中位数分两组；n≥15 的格才计入）：")
    RAT = DEV0 / np.where(SD0 > 0, SD0, np.nan)
    ST = np.array([{"t150": 0, "t60": 1, "listen": 2}.get(r["stage"], 3) for r in sig])
    for tag, arrs in (("fill 6 档 × dev0/sd 4 档", [(FILL, 6), (RAT, 4)]),
                      ("σ(sd) 6 档", [(SD0, 6)]),
                      ("stage 3 档", [(ST, 3)]),
                      ("fill 6 × stage 3", [(FILL, 6), (ST, 3)]),
                      ("fill 6 × dev0/sd 4 × stage 3", [(FILL, 6), (RAT, 4), (ST, 3)])):
        binned_ctrl(W0, arrs, Y, tag=f"walk0 控 {tag}")

    print("\n§5d 分半稳定性（同一检验，切两半各跑）")
    for nm, k in (("walk0", W0), ("basis0", B0)):
        for tag, sub in ((f"h1 (<{mid})", DATE < mid), ("h2 (>=mid)", DATE >= mid)):
            o, p, nl = exact_fill_perm(k, Y, FILL, sub=sub)
            print(f"     {nm:<8} {tag:<12} Δ={o:+7.2f}pp  p={p:.4f}  ({nl} 层)")

    print("\n§5e 当**出场触发**用：机制家族的提前触发（对照 36 号现行规则）")
    print("     规则                                   触发  占比  出场均 中位  触发组赢率 出场价−胜率 杀/救    ΔU       [CI]")
    s36.replay(sig, lambda c, i: c["bid"][i] < 0.30 and c["dev"][i] < -20.0,
               "（现行）bid<0.30 ∧ dev<−20")
    for X in (0.0, -20.0, -40.0, -60.0):
        s36.replay(sig, lambda c, i, X=X: c["basis"][i] < X, f"basis<{X:+.0f}（无条件）")
    for lo in (0.60, 0.80):
        for X in (-20.0, -40.0):
            s36.replay(sig, lambda c, i, X=X, lo=lo: c["basis"][i] < X and c["bid"][i] < lo,
                       f"basis<{X:+.0f} ∧ bid<{lo:.2f}")
    for Y_ in (0.80, 0.60, 0.30):
        s36.replay(sig, lambda c, i, Y_=Y_: c["dev"][i] < -20.0 and c["devf"][i] > 0.0
                   and c["bid"][i] < Y_, f"dev<−20 ∧ walk>0 ∧ bid<{Y_:.2f}（结算线还在）")


def sec6(sig):
    print("\n" + "=" * 100)
    print("§6 机制侧写：缺口是**领先量**不是**危险量**")
    A = []
    for r in sig:
        p = r["path"]
        m = p["bm"] < 0
        if m.sum() < 5:
            continue
        A.append((p["basis"][m][0], p["devf"][m][0], p["basis"][m][-1],
                  p["devf"][m][-1], p["dev"][m][-1], r["won"]))
    A = np.array(A)
    print(f"   n={len(A)}（入场 → 收盘）")
    print(f"   corr(basis0, Δwalk)  = {np.corrcoef(A[:,0], A[:,3]-A[:,1])[0,1]:+.3f}"
          "   ← **正**：缺口大的窗，结算线随后**朝缺口方向自己走过来**")
    print(f"   corr(basis0, basis_end) = {np.corrcoef(A[:,0], A[:,2])[0,1]:+.3f}"
          "         ← 缺口有惯性、不是立刻消失")
    q = np.quantile(A[:, 0], [0, .25, .5, .75, 1.0])
    print("\n   按入场 basis0 分档：")
    print("     basis0 档                n    输率   walk0均   Δwalk均   walk末均   dev末均")
    for i in range(4):
        s = (A[:, 0] >= q[i]) & (A[:, 0] < q[i + 1] if i < 3 else A[:, 0] <= q[i + 1])
        print(f"     [{q[i]:7.1f},{q[i + 1]:7.1f}) {s.sum():>6} "
              f"{(1-A[s,5].mean())*100:6.2f}% {A[s,1].mean():9.1f} "
              f"{(A[s,3]-A[s,1]).mean():+9.1f} {A[s,3].mean():9.1f} {A[s,4].mean():9.1f}")
    print("   ⇒ 最高缺口那一档**不是**最差的：它的 walk 后来涨了 +62、dev 收在 157（全场最高）。")
    print("     真正与输相关联的是 **walk 小**（已结算位移少），不是 basis 大。")


def main():
    rows = s36.get()
    sig = sec0(rows)
    sec1(sig)
    sec2(sig)
    sec3(sig)
    sec4(sig)
    sec5(sig)
    sec6(sig)
    print("\n" + "=" * 100)
    print("结论：① 分解在**输单里确实有特征**（basis 先翻负、walk 后跟，市场同时动手）；")
    print("      ② 但**当止损腿没用**（提前触发 = 按公平价卖，杀:救 ≈ 1:1）；")
    print("      ③ 当**入场闸**有 ≈2pp 残差（p≈0.02，dev0 对照为零），**但分半不稳**")
    print("         （h1 p=0.036 / h2 p=0.41）⇒ 登记观察，不改引擎。")


if __name__ == "__main__":
    main()
