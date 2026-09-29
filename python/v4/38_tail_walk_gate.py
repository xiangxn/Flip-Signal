#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘：**入场闸 walk ≥ X**（用户 2026-09-29 的提议）——按引擎真实语义算钱。

背景（`docs/tail_dev_decomp_2026-09-29.md`）：把 `dev` 拆成两个分量

    dev = sgn·(spot − anchor) = basis + walk
      ├─ basis = sgn·(spot − twap)    缺口：**还没**写进结算线的部分（领先量）
      └─ walk  = sgn·(twap − anchor)  位移：**已经**写进结算线的部分（状态量）

信号里输单的构成与赢单相反（`walk0` 中位 38.4 vs 63.2，而 `dev0` 几乎相同），
按精确成交价分层后 `walk0` 还剩 −1.88pp 输率梯度（置换 p=0.021）⇒ 用户提议：
**入场时若 walk < X 就不入场**。本脚本回答「那到底要付什么代价、拿回什么」。

⚠️ 37 号 §5b 那个 Δ +22.97U 是**把 643 笔直接删掉**算的——**不是引擎语义**。
三段递进链是「任一段不达标则进入下一段」，而 `walk` 在窗内**随时间长大**（feed 往
spot 收敛）：T=150 被闸的窗多半会在 T=60 以**更高的 walk、更高的价**重入。
本脚本把这条**改道**算进去，并同时给出三种语义：

    A 逐段 AND   每段判定时都要 walk ≥ X（不达标 ⇒ 该段不出信号 ⇒ 链继续）← 最贴近引擎
    B 整窗闸     首个判定点的 walk < X ⇒ **整窗不下单**（后面几段也不再看）
    C 只闸 T150  仅段 1 加 walk 条件，T=60 与监听段一字不动

判决区间 = **日级配对 bootstrap**（14 天重采样 2000 次, 与 23/14 同源）。
判据 = 32 号的 λ\\* = 1 − 该子集平均成交价（平衡点输率），看闸后余量往哪边动。

用法: python/venv/bin/python python/v4/38_tail_walk_gate.py
"""
import sys
import random
import datetime
import collections
import importlib.util
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s23 = load("s23", BASE / "23_tail_integrated.py")
s13 = load("s13", BASE / "13_tail_sweep.py")

STAKE = s23.STAKE
T150, T60 = s23.T150, s23.T60


def walk_of(t, anchor):
    """walk = sgn·(twap − anchor)；twap 缺失返 None（闸**不拦**——见文件头口径说明）。"""
    tw = t.get("twap")
    if not tw:
        return None
    sgn = 1.0 if t["side"] == "yes" else -1.0
    return sgn * (tw - anchor)


def pnl(rows):
    s = 0.0
    for r in rows:
        s += (STAKE / r["fill"] - STAKE) if r["settle_won"] else -STAKE
    return s


def boot_delta(base, new, n=2000, seed=38):
    """日级**配对** Δ 的 bootstrap 区间：按同一批日子重采样 (P&L_new − P&L_base)。"""
    days = sorted(set(list(base) + list(new)))
    d = [new.get(k, 0.0) - base.get(k, 0.0) for k in days]
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        out.append(sum(d[rnd.randrange(len(d))] for _ in d))
    out.sort()
    return out[int(0.025 * n)], out[int(0.975 * n) - 1]


# ── 闸链（三种语义） ──────────────────────────────────────────────────────

def gate_ok(t, anchor, X, sd=None, unit="usd"):
    """unit=usd: walk ≥ X 美元；unit=sig: walk/sd ≥ X 倍 σ（σ 缺失 ⇒ 不拦）。"""
    w = walk_of(t, anchor)
    if w is None:
        return True
    if unit == "sig":
        return bool(sd and sd > 0 and (w / sd) >= X)
    return w >= X


def chain_gated(ticks, anchor, sd, date, outcome, X, mode, unit="usd"):
    """返回 (rows, info)；info 记本窗被闸的段 / 是否改道 / 改道的价位。"""
    if not ticks:
        return [], None
    info = {"gated": [], "rerouted": None}
    head = ticks[0]
    # mode B: 首个判定点的 walk < X ⇒ 整窗不下单
    if mode == "B" and not gate_ok(head, anchor, X, sd, unit):
        info["gated"].append("t150" if head["rem"] > T60 else "t60")
        info["whole_skip"] = True
        return [], info
    rows = []
    if head["rem"] > T60:
        r = s23.row(head, anchor, sd, date, outcome, "t150")
        if s23.r5(r, strict_price=True) and gate_ok(head, anchor, X, sd, unit):
            r["ok"] = True
            return [r], info
        if s23.r5(r, strict_price=True):
            info["gated"].append("t150")
        rows.append(r)
        rest = [x for x in ticks if x["rem"] <= T60]
    else:
        rest = ticks
    if rest:
        t2 = rest[0]
        r2r = s23.row(t2, anchor, sd, date, outcome, "t60")
        g2 = gate_ok(t2, anchor, X, sd, unit) if mode != "C" else True
        if s23.r5(r2r) and g2:
            r2r["ok"] = True
            if info["gated"]:
                info["rerouted"] = ("t60", r2r["fill"], walk_of(t2, anchor))
            return rows + [r2r], info
        if s23.r5(r2r):
            info["gated"].append("t60")
        rows.append(r2r)
        for x in rest[1:]:
            rl = s23.row(x, anchor, sd, date, outcome, "listen")
            gl = gate_ok(x, anchor, X, sd, unit) if mode != "C" else True
            if s23.r2(rl) and gl:
                rl["ok"] = True
                if info["gated"]:
                    info["rerouted"] = ("listen", rl["fill"], walk_of(x, anchor))
                rows.append(rl)
                return rows, info
            if s23.r2(rl):
                info["gated"].append("listen")
    return rows, info


# ── 数据装载 ──────────────────────────────────────────────────────────────

def universe():
    ev = s23.load_events("data/btc")
    hr = s13.hist_ranges(ev)
    wins = []
    for e in ev:
        outcome, anchor = e.get("outcome"), e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        h = hr.get(e["start_time"])
        if h is None:
            continue
        sd = h / anchor * 1e4 * anchor / 1e4
        date = datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")
        ticks = s23.win_ticks(e)
        if not ticks:
            continue
        # 首判定 tick 的 twap 采样龄（查「walk 小是不是陈旧采样造的假象」）
        age0 = None
        for x in e.get("ticks") or []:
            if x.get("rem") == ticks[0]["rem"]:
                age0 = (x.get("twap") or {}).get("age_ms")
                break
        wins.append({"date": date, "outcome": outcome, "anchor": anchor,
                     "sd": sd, "ticks": ticks, "age0": age0})
    return wins


def run_units(wins, X, mode, unit):
    return run(wins, X, mode, unit)


def run(wins, X, mode, unit="usd"):
    sig, infos = [], []
    for i, w in enumerate(wins):
        rows, info = chain_gated(w["ticks"], w["anchor"], w["sd"], w["date"],
                                 w["outcome"], X, mode, unit)
        for r in rows:
            if r.get("ok"):
                r["walk0"] = walk_of(w["ticks"][0], w["anchor"])
                r["wid"] = i
                sig.append(r)
        if info and info.get("gated"):
            info["wid"] = i
            infos.append(info)
    return sig, infos


def by_day(rows):
    d = collections.defaultdict(float)
    for r in rows:
        d[r["date"]] += (STAKE / r["fill"] - STAKE) if r["settle_won"] else -STAKE
    return d


def main():
    wins = universe()
    base, _ = run(wins, -1e18, "A")

    # §0 pin（与 oracle 23 逐位一致）
    print("=" * 104)
    # ⚠️ 这组字面量是**闸前**基线（oracle 23 在 2026-09-29 落地入场闸之前的值）——
    # 本脚本要算的是「相对基线」的 Δ, 故刻意钉在闸前口径上, 不随 oracle 前移。
    print("§0 口径自检（闸前基线 = oracle 23 的历史 pin, 逐位一致）")
    n = len(base)
    wr = sum(r["settle_won"] for r in base) / n * 100
    P = pnl(base)
    lost = sum(1 for r in base if not r["settle_won"])
    print(f"   基线链 n={n}（pin 2133）  WR {wr:.6f}%（pin 96.061885%）  P&L {P:+.6f}U"
          f"（pin +35.092748）  输 {lost}（pin 84）   "
          f"{'✅' if n == 2133 and abs(P - 35.092748) < 1e-4 else '❌'}")
    print(f"   窗数 {len(wins)}（pin 3640）")
    # 落地形态（mode C, X=43）的**精确新 pin**（供 oracle/parity 用）
    gpin, _ = run(wins, 43.0, "C")
    n2 = len(gpin)
    wr2 = sum(r["settle_won"] for r in gpin) / n2 * 100
    print(f"   落地形态 pin（mode C, X=43）：n={n2}  WR {wr2:.6f}%  "
          f"P&L {pnl(gpin):+.6f}U  输 {sum(1 for r in gpin if not r['settle_won'])}")
    BD = by_day(base)

    # §1 walk 在窗内怎么长（这决定「等一段再进」会发生什么）
    print("\n" + "=" * 104)
    print("§1 为什么「逐段闸」≠「直接删」：walk 在窗内随时间长大（全场 3640 窗，可判定 tick）")
    print("     段             tick 数    walk 中位   walk p25   walk p75")
    buckets = collections.defaultdict(list)
    for w in wins:
        n_ = len(w["ticks"])
        for i, t in enumerate(w["ticks"]):
            wv = walk_of(t, w["anchor"])
            if wv is None:
                continue
            if i == 0:
                buckets["首 tick（最早判定点）"].append(wv)
            else:
                buckets["后续 tick"].append(wv)
    for k in ("首 tick（最早判定点）", "后续 tick"):
        v = sorted(buckets[k])
        print(f"     {k:<14} {len(v):>8}  {v[len(v)//2]:9.1f} {v[len(v)//4]:10.1f} "
              f"{v[3*len(v)//4]:10.1f}")
    print("\n     基线出信号时 walk（2133 笔，按段）——「等一段」= walk 天然更大:")
    print("     段            n     walk p10   中位    p90")
    for st in ("t150", "t60", "listen"):
        v = sorted(r["walk0"] for r in base if r["stage"] == st)
        if not v:
            continue
        print(f"     {st:<12} {len(v):>5} {v[len(v)//10]:10.1f} {v[len(v)//2]:6.1f} "
              f"{v[9*len(v)//10]:8.1f}")

    # §2 三种语义 × 阈值扫描
    print("\n" + "=" * 104)
    print("§2 阈值扫描：X 取多少、三种语义各要付什么（基线 "
          f"n={n} P&L {P:+.2f}U）")
    for mode, mname in (("A", "A 逐段 AND（每段都要 walk≥X）"),
                        ("B", "B 整窗闸（首判 walk<X ⇒ 整窗不下单）"),
                        ("C", "C 只闸 T150 段")):
        print(f"\n   【{mname}】")
        print("      X      n    砍掉   输   WR      P&L        Δ       日级配对95%        改道")
        for X in (-1e18, 0.0, 20.0, 30.0, 40.0, 43.0, 50.0, 60.0, 70.0, 80.0):
            sig, infos = run(wins, X, mode)
            if X == -1e18:
                continue
            dd = by_day(sig)
            lo, hi = boot_delta(BD, dd)
            d = pnl(sig) - P
            ng = sum(len(i["gated"]) for i in infos)
            nr = sum(1 for i in infos if i.get("rerouted"))
            lab = f"{X:6.1f}"
            w_r = sum(r["settle_won"] for r in sig) / len(sig) * 100
            print(f"     {lab} {len(sig):>5} {n - len(sig):>6} {sum(1 for r in sig if not r['settle_won']):>5}  "
                  f"{w_r:6.2f}%  {pnl(sig):+8.2f}U {d:+8.2f}U  [{lo:+7.2f},{hi:+7.2f}]  "
                  f"闸{ng} 改道{nr}")

    # §3 改道细看（X=43, mode C —— 推荐的形态）
    print("\n" + "=" * 104)
    print("§3 改道细看（X=43, mode C）：被 T=150 闸下的窗后来怎么进的")
    sig43, infos43 = run(wins, 43.0, "A")          # mode A 的改道覆盖全链
    s43c, i43c = run(wins, 43.0, "C")
    bl = {r["wid"]: r for r in base}
    gc = {r["wid"]: r for r in s43c}
    re_wid = set(i["wid"] for i in i43c if i.get("rerouted"))
    dead = set(bl) - set(gc)
    print(f"   mode C：被闸的 T=150 信号 250 笔 → 其中 {len(re_wid)} 个窗**改道重入**"
          f"（换到 T=60/监听段），{len(dead)} 个窗**整窗死亡**（后面再也没有信号）")
    ok = [gc[k] for k in re_wid if k in gc]
    if ok:
        fls = sorted(r["fill"] for r in ok)
        print(f"   改道那 {len(ok)} 笔：重入 fill 中位 {fls[len(fls)//2]:.3f}"
              f"（p25 {fls[len(fls)//4]:.3f} p75 {fls[3*len(fls)//4]:.3f}）"
              f"  输 {sum(1 for r in ok if not r['settle_won'])}   P&L {pnl(ok):+.2f}U"
              f"（同一批窗在基线里 {pnl([bl[k] for k in re_wid if k in bl]):+.2f}U"
              f" ⇒ 改道本身**倒亏 {pnl(ok) - pnl([bl[k] for k in re_wid if k in bl]):+.2f}U**）")
    dl = [bl[k] for k in dead]
    print(f"   死亡那 {len(dl)} 笔：输 {sum(1 for r in dl if not r['settle_won'])}"
          f"（{sum(1 for r in dl if not r['settle_won'])/len(dl)*100:.1f}%）"
          f"  P&L {pnl(dl):+.2f}U ⇒ **省下 {-pnl(dl):+.2f}U**")
    print(f"   ⇒ 净 Δ = {-pnl(dl):+.2f} + ({pnl(ok) - pnl([bl[k] for k in re_wid if k in bl]):+.2f})"
          f" = {pnl(s43c) - pnl(base):+.2f}U")
    print(f"   84 个输单：基线 84 → 闸后 {sum(1 for r in s43c if not r['settle_won'])}")

    # §4 分半（Δ 的稳定性）
    print("\n" + "=" * 104)
    print("§4 分半稳定性（mode A；把日子切两半，各自重算 Δ）")
    days = sorted(set(w["date"] for w in wins))
    mid = days[len(days) // 2]
    print(f"   切点 {mid}")
    for mode in ("A", "B", "C"):
        for X in (30.0, 43.0, 60.0):
            line = []
            for tag, sel in ((f"h1 (<{mid})", lambda w: w["date"] < mid),
                             ("h2 (>=mid)", lambda w: w["date"] >= mid)):
                sub = [w for w in wins if sel(w)]
                b, _ = run(sub, -1e18, mode)
                g, _ = run(sub, X, mode)
                dd = by_day(g)
                lo, hi = boot_delta(by_day(b), dd)
                line.append(f"{tag} Δ {pnl(g) - pnl(b):+7.2f}U [{lo:+6.2f},{hi:+6.2f}]")
            print(f"   {mode} X={X:5.1f}  " + "   ".join(line))

    # §5 λ*（用 32 号的判据复核闸后余量）
    print("\n" + "=" * 104)
    print("§5 λ* 判据（平衡点输率 = 1 − 该子集平均成交价）——闸把余量往哪边推")
    print("     集合                        n    均 fill   λ*(平衡点)  实测输率     余量")
    for lab, rows in (("基线全体", base),
                      ("X=43 保留组（mode A）", sig43),
                      ("X=43 被闸掉的那批", [r for r in base if r not in sig43])):
        if not rows:
            print(f"     {lab:<26} —")
            continue
        f_ = sum(r["fill"] for r in rows) / len(rows)
        lam = (1 - f_) * 100
        lr = sum(1 for r in rows if not r["settle_won"]) / len(rows) * 100
        print(f"     {lab:<26} {len(rows):>5}  {f_:.4f}   {lam:6.2f}%    {lr:6.2f}%   "
              f"{lam - lr:+6.2f}pp")
    print("     ⚠️ 「被闸掉的那批」按**笔**归属（不是按窗）：mode A 里被闸的窗若后来重入，"
          "它在保留组里")

    # §6 这个 Δ 经不经得起「它不是价格腿的伪装」和「它是不是两天撑起来的」
    print("\n" + "=" * 104)
    print("§6 稳健性三问（都针对 mode C 的 X=43，因为它的 CI 下界在 0 以上）")
    gate_c, infos_c = run(wins, 43.0, "C")
    kept_wid = set(r["wid"] for r in gate_c)
    t150 = [r for r in base if r["stage"] == "t150"]
    killed = [r for r in t150 if r["wid"] not in kept_wid]
    print(f"\n   (1) 闸掉的是谁：T150 段 {len(t150)} 笔里闸掉 {len(killed)} 笔")
    print("       集合                     n    输率    均 fill   P&L      均笔")
    for lab, g in (("T150 保留", [r for r in t150 if r["wid"] in kept_wid]),
                   ("T150 闸掉", killed)):
        if not g:
            continue
        lo_ = sum(1 for r in g if not r["settle_won"])
        print(f"       {lab:<22} {len(g):>5}  {lo_/len(g)*100:5.2f}%  "
              f"{sum(r['fill'] for r in g)/len(g):.4f}  {pnl(g):+7.2f}U  {pnl(g)/len(g):+.4f}")
    if killed:
        fs = sorted(r["fill"] for r in killed)
        print(f"       闸掉那批 fill：p10 {fs[len(fs)//10]:.3f} 中位 {fs[len(fs)//2]:.3f} "
              f"p90 {fs[9*len(fs)//10]:.3f}   ← 与「价格腿便宜角落」是否同一件事")
        k85 = [r for r in killed if r["fill"] >= 0.85]
        print(f"       其中 fill ≥ 0.85 的 {len(k85)} 笔（占闸掉 {len(k85)/len(killed)*100:.0f}%），"
              f"输率 {sum(1 for r in k85 if not r['settle_won'])/max(1,len(k85))*100:.2f}%")
        for lab, g in (("T150 保留", [r for r in t150 if r["wid"] in kept_wid]), ("T150 闸掉", killed)):
            sdv = sorted(r["sd"] for r in g)
            wv = sorted(r["walk0"] for r in g if r["walk0"] is not None)
            dv = sorted(r["dev"] for r in g)
            medw = wv[len(wv) // 2] if wv else float("nan")
            meds = sdv[len(sdv) // 2] if sdv else float("nan")
            print(f"       {lab} 的 sd 中位 {meds:.1f}（{medw/meds:.2f}σ）   "
                  f"dev0 中位 {dv[len(dv)//2]:.1f}   ← dev0 若明显更小 ⇒ 闸可能只是 dev 矮的伪装")

    print("\n   (2) 逐日明细（mode C X=43；只列有变化的日）")
    bd, gd = by_day(base), by_day(gate_c)
    tot = 0.0
    print("       日期         基线P&L    闸后P&L     Δ")
    for k in sorted(set(list(bd) + list(gd))):
        if abs(gd.get(k, 0) - bd.get(k, 0)) < 1e-9:
            continue
        tot += gd.get(k, 0) - bd.get(k, 0)
        print(f"       {k}  {bd.get(k,0):+9.2f}  {gd.get(k,0):+9.2f}  "
              f"{gd.get(k,0)-bd.get(k,0):+8.2f}")
    print(f"       （变化日合计 {tot:+.2f}U；最大单日贡献 "
          f"{max(abs(gd.get(k,0)-bd.get(k,0)) for k in set(list(bd)+list(gd))):.2f}U）")

    print("\n   (3) 分层置换：T150 段内，按**精确 fill** 分层后 walk0 还有没有信息")
    y = np.array([0 if r["settle_won"] else 1 for r in t150])
    w0 = np.array([r["walk0"] if r["walk0"] is not None else 0.0 for r in t150])
    fl = np.array([r["fill"] for r in t150])
    obs, p, nl = strat_perm(w0, y, fl, minn=6)
    print(f"       T150 段（n={len(t150)}）：高/低组输率差 {obs:+.2f}pp  置换 p={p:.4f}  "
          f"（{nl} 个价位层）")
    print("       ⇒ p 小 = 闸的边际不是「T150 段便宜价」的伪装")

    print("\n   (4) **安慰剂闸**：同样砍 k 笔 T150 信号，但**按 fill 分层随机挑**（4000 次）")
    k = len(killed)
    print(f"       砍 k={k} 笔；随机挑的 Δ 分布 vs 实际 +28.93U")
    if k:
        plc = placebo_drop(t150, kept_wid, base, gate_c, k, BD, nperm=4000)
        print(f"       安慰剂 Δ：中位 {plc['med']:+.2f}U  p5 {plc['p5']:+.2f}  "
              f"p95 {plc['p95']:+.2f}   |Δ|≥28.93 的比例 p={plc['p']:.4f}")

    print("\n   (5) σ 是不是背后的真变量：X 定义成**美元**还是**σ 倍**？")
    print("       口径                         闸掉  输率     Δ        日级配对95%")
    for lab, Xv, unit in (("美元 walk0 ≥ 43", 43.0, "usd"),
                          ("美元 walk0 ≥ 62（=sd 中位）", 62.0, "usd")):
        g2, _ = run_units(wins, Xv, "C", unit)
        dd = by_day(g2)
        lo2, hi2 = boot_delta(BD, dd)
        kd = [r for r in t150 if r["wid"] not in set(x["wid"] for x in g2)]
        lr = sum(1 for r in kd if not r["settle_won"]) / max(1, len(kd)) * 100
        print(f"       {lab:<28} {len(kd):>4}  {lr:5.2f}%  {pnl(g2)-P:+7.2f}U  "
              f"[{lo2:+7.2f},{hi2:+7.2f}]")
    for Xs in (0.5, 0.7, 1.0):
        g2, _ = run_units(wins, Xs, "C", "sig")
        dd = by_day(g2)
        lo2, hi2 = boot_delta(BD, dd)
        kd = [r for r in t150 if r["wid"] not in set(x["wid"] for x in g2)]
        lr = sum(1 for r in kd if not r["settle_won"]) / max(1, len(kd)) * 100
        print(f"       {'σ 倍 walk0/sd ≥ ' + str(Xs):<28} {len(kd):>4}  {lr:5.2f}%  "
              f"{pnl(g2)-P:+7.2f}U  [{lo2:+7.2f},{hi2:+7.2f}]")
    print("       若「σ 倍」明显更好 ⇒ 真变量是 σ 不是缺口；若「美元」更好 ⇒ 缺口口径对")

    print("\n   (6) 是数据假象吗（twap 采样陈旧 ⇒ walk ≈ 0 的系统性来源）")
    ag = [w["age0"] for w in wins if w.get("age0") is not None]
    ag.sort()
    print(f"       全场首判定 tick 的 twap age_ms：p10 {ag[len(ag)//10]} 中位 "
          f"{ag[len(ag)//2]} p90 {ag[9*len(ag)//10]}（缺失 {len(wins)-len(ag)}）")
    for lab, g in (("保留", [r for r in t150 if r["wid"] in kept_wid]),
                   ("闸掉", killed)):
        a2 = sorted(wins[r["wid"]]["age0"] for r in g if wins[r["wid"]].get("age0") is not None)
        zn = sum(1 for r in g if r["walk0"] is not None and abs(r["walk0"]) < 5)
        print(f"       {lab}：age 中位 {a2[len(a2)//2] if a2 else -1}ms   "
              f"|walk0|<5 的 {zn} 笔")

    print("\n   (7) 闸掉的窗走的是哪条准入腿（dev 腿 vs σ 腿）——按**窗**归属，Δ 可加")
    print("       腿                    n   闸掉  闸掉输率   这批窗的Δ(闸后−基线)")
    gb = {r["wid"]: r for r in base}
    gg = {r["wid"]: r for r in gate_c}
    for lab, leg in (("dev 腿  dev ≥ 63", lambda r: r["dev"] >= 63.0),
                     ("σ 腿  sd≥40 ∧ dev≥sd", lambda r: r["dev"] < 63.0 and r["sd"] >= 40.0
                      and r["dev"] >= r["sd"]),
                     ("两条都不走（不该出现）", lambda r: not (
                         r["dev"] >= 63.0 or (r["sd"] >= 40.0 and r["dev"] >= r["sd"])))):
        wids = set(r["wid"] for r in t150 if leg(r))
        if not wids:
            print(f"       {lab:<24} 0")
            continue
        kd = [r for r in t150 if r["wid"] in wids and r["wid"] not in kept_wid]
        lr = sum(1 for r in kd if not r["settle_won"]) / max(1, len(kd)) * 100
        d = (pnl([gg[k] for k in wids if k in gg]) - pnl([gb[k] for k in wids if k in gb]))
        print(f"       {lab:<24} {len(wids):>4} {len(kd):>5}  {lr:7.2f}%   {d:+8.2f}U")

    print("\n   (8) **扫参数这件事本身的代价**：X=43 是同一批数据上的 argmax")
    print("       ⇒ 用「阈值扫描的极值」当统计量做置换检验（层内打乱 walk0，重扫整条曲线取最大）")
    sweep_aware(wins, base, t150, nperm=2000)


def sweep_aware(wins, base, t150, nperm=2000, seed=38):
    """把「扫了 9 个 X + 3 个 σ 倍」的搜索代价算进 p 值。

    恒等式：mode C 只动 T=150 段 ⇒ 被闸窗的结果 = 它**后来那笔**（没有则为 0），
    与 X 无关（用「闸掉全部 T150」的那次运行取它）⇒

        Δ(X) = Σ_{walk0 < X} (pnl_后来 − pnl_T150) = Σ_{walk0 < X} c

    于是整条扫描曲线是一次加权阈值和，置换可以便宜地重扫全网格取极值。
    """
    cut_all, _ = run(wins, 1e18, "C")           # T150 全闸 ⇒ 每个窗的「后来那笔」
    after = {r["wid"]: r for r in cut_all}
    w, c, sd, fl, rm, sy = [], [], [], [], [], []
    for r in t150:
        a = after.get(r["wid"])
        p_a = (((STAKE / a["fill"] - STAKE) if a["settle_won"] else -STAKE) if a else 0.0)
        p_b = (STAKE / r["fill"] - STAKE) if r["settle_won"] else -STAKE
        w.append(r["walk0"] if r["walk0"] is not None else 1e18)
        c.append(p_a - p_b)
        sd.append(r["sd"])
        fl.append(round(r["fill"], 4))
        rm.append(r["rem"] // 10)                       # rem0 十秒档（迟到接入 = 位移没时间长大）
        sy.append(1 if r["side"] == "yes" else 0)
    w, c, sd = np.array(w), np.array(c), np.array(sd)
    DOLL = (0.0, 20.0, 30.0, 40.0, 43.0, 50.0, 60.0, 70.0, 80.0)
    SIG = (0.5, 0.7, 1.0)

    def curve(ww):
        out = [c[ww < X].sum() for X in DOLL]
        sg = np.where(sd > 0, ww / np.maximum(sd, 1e-9), 1e18)
        out += [c[sg < t].sum() for t in SIG]
        return out

    mp = pnl(base)
    obs = curve(w)
    print("       重建曲线（应与 §2 mode C 的 P&L 逐位一致）:")
    print("       " + "  ".join(f"X={X:g}:{mp+v:+.2f}U" for X, v in zip(DOLL, obs[:len(DOLL)])))
    print(f"       σ 倍 0.5/0.7/1.0 → " + "  ".join(f"{mp+v:+.2f}U" for v in obs[len(DOLL):]))
    print(f"       观测极值：最好那条 Δ = **{max(obs):+.2f}U**")
    med = lambda v: sorted(v)[len(v) // 2]                    # noqa: E731
    gset = set(r["wid"] for r in t150 if r["walk0"] is not None and r["walk0"] < 43)
    print(f"       闸掉那 53 笔的 rem0 中位 {med([r['rem'] for r in t150 if r['wid'] in gset])}"
          f" / 保留 {med([r['rem'] for r in t150 if r['wid'] not in gset])}"
          f"；yes 占比 "
          f"{sum(1 for r in t150 if r['wid'] in gset and r['side']=='yes')/max(1,len(gset))*100:.0f}%"
          f" / {sum(1 for r in t150 if r['wid'] not in gset and r['side']=='yes')/max(1,len(t150)-len(gset))*100:.0f}%")
    qs = np.quantile(sd, [0.25, 0.5, 0.75])
    sq = np.searchsorted(qs, sd)
    strata = collections.defaultdict(list)
    for i in range(len(w)):
        strata[(fl[i], int(sq[i]), rm[i], sy[i])].append(i)   # 再控 rem0 十秒档 + 侧别
    strata = [v for v in strata.values() if len(v) > 1]
    print(f"       分层 = 精确 fill × sd 四分位 × rem0 十秒档 × 侧别 ⇒ {len(strata)} 层")
    rnd = random.Random(seed)
    cnt = 0
    mx = []
    for _ in range(nperm):
        ww = w.copy()
        for ix in strata:
            v = ww[ix].copy()
            rnd.shuffle(v)
            ww[ix] = v
        m = max(curve(ww))
        mx.append(m)
        if m >= max(obs):
            cnt += 1
    mx.sort()
    print(f"       置换零分布（{nperm} 次；层内打乱 walk0，每次**重扫 12 个阈值取最大**）：")
    print(f"         中位 {mx[nperm//2]:+.2f}U   p95 {mx[int(0.95*nperm)]:+.2f}U   "
          f"最大 {mx[-1]:+.2f}U")
    pv = (cnt + 1) / (nperm + 1)
    print(f"       ⇒ **搜索校正后的 p = {pv:.4f}**（{'通过 0.05' if pv < 0.05 else '不通过'}；"
          f"零分布中位本身就有 {mx[nperm//2]:+.2f}U ⇒ 扫阈值天然会挑出正的）")


def placebo_drop(t150, kept_wid, base, gate_c, k, BD, nperm=4000, seed=38):
    """按 fill 分层随机挑 k 笔 T150 信号删掉，得到 Δ 的安慰剂分布。"""
    strata = collections.defaultdict(list)
    for r in t150:
        strata[round(r["fill"], 4)].append(r)
    rnd = random.Random(seed)
    outs = []
    for _ in range(nperm):
        drop = []
        for f_, g in strata.items():
            n_ = max(1, round(len(g) * k / len(t150)))
            drop += rnd.sample(g, min(n_, len(g)))
        wid_drop = set(r["wid"] for r in drop)
        dd = collections.defaultdict(float)
        for r in base:
            if r["wid"] in wid_drop:
                continue
            dd[r["date"]] += (STAKE / r["fill"] - STAKE) if r["settle_won"] else -STAKE
        outs.append(pnl([r for r in base if r["wid"] not in wid_drop]) - pnl(base))
    outs.sort()
    return {"med": outs[nperm // 2], "p5": outs[int(0.05 * nperm)],
            "p95": outs[int(0.95 * nperm)],
            "p": sum(1 for v in outs if abs(v) >= 28.93) / nperm}


def strat_perm(key, y, fill, minn=6, nperm=4000, seed=38, sub=None):
    """按**精确 fill 值**分层 → 层内按 key 中位数分高低两组 → 置换检验（层内打乱标签）。"""
    import numpy as np
    if sub is not None:
        key, y, fill = key[sub], y[sub], fill[sub]
    strata = collections.defaultdict(list)
    for i, f_ in enumerate(fill):
        strata[round(float(f_), 4)].append(i)
    labels = {}
    for f_, ix in strata.items():
        if len(ix) < minn:
            continue
        kk = np.array([key[i] for i in ix])
        med = float(np.median(kk))
        labels[f_] = [ix[j] for j in range(len(ix)) if kk[j] >= med], \
                     [ix[j] for j in range(len(ix)) if kk[j] < med]

    def stat(lb):
        a = b = na = nb = 0
        for _, (hi_, lo_) in lb.items():
            na += len(hi_)
            nb += len(lo_)
            a += sum(y[i] for i in hi_)
            b += sum(y[i] for i in lo_)
        if not na or not nb:
            return 0.0, 0
        return (a / na - b / nb) * 100, min(na, nb)

    obs, nl = stat(labels)
    rnd = random.Random(seed)
    cnt = 0
    pool = [i for v in labels.values() for ix in v for i in ix]
    for _ in range(nperm):
        lab = {}
        for f_, (hi_, lo_) in labels.items():
            both = hi_ + lo_
            v = [y[i] for i in both]
            rnd.shuffle(v)
            m = len(hi_)
            lab[f_] = (both[:m], both[m:]), v
        # 用打乱后的标签重算
        a = b = na = nb = 0
        for f_, ((hi_, lo_), v) in lab.items():
            na += len(hi_)
            nb += len(lo_)
            a += sum(v[j] for j in range(len(hi_)))
            b += sum(v[j] for j in range(len(hi_), len(v)))
        if na and nb:
            s = (a / na - b / nb) * 100
            if abs(s) >= abs(obs):
                cnt += 1
    return obs, (cnt + 1) / (nperm + 1), nl


if __name__ == "__main__":
    main()
