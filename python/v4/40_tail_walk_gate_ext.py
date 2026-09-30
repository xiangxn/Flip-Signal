#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘：把入场闸 `walk ≥ X` 从 T=150 段**扩展到 T=60 段 / 监听段**值不值？

用户 2026-10-01 的问题：「入场闸加到 T60 以及以后，对收益是否有影响，试试 43、33」。

现行落地形态（决策 #29）只闸段 1：`stage == t150 ∧ walk < 43` ⇒ 该段不出信号、链继续。
38 号脚本已顺带算过 **mode A（三段逐段 AND）X=43**：+28.31U [−1.72, +61.59]（vs 闸前基线），
与只闸 T150 的 +28.93U 净收益相当，但**多砍 258 笔**。本脚本把这件事拆细：

  · 闸**逐段**参数化（t150 / t60 / listen 各自独立 X），不再只有 A/B/C 三整形态
  · 补 X=33（更低阈值）与整条阈值扫描（20~100），看 T60/监听段有没有自己的最优
  · 归因：多砍的笔在 T60 段还是监听段？砍掉的是输单还是零效果单？改道去哪、改道税多少？
  · 判据与 38/23 同源：**日级配对 bootstrap 区间**；参照系 = **现行 config C43**
    （T150 闸 X=43，n=2080 / +64.026354U）——问题问的是「在现行形态上再加闸」。

口径与 23（oracle）/ 38 逐位一致：宇宙 = `win_ticks` 可判定 tick（延迟 ≤300ms ∧ 四档齐
∧ spot 在场 ∧ 0 < rem ≤ 150）；σ 按事件索引现算；T=150 价格腿严格大于。
闸的语义 = 「该段判定满足价格/dev/σ 腿但 walk < X ⇒ 本段不出信号、链继续（落到下一段）」。

用法: python/venv/bin/python python/v4/40_tail_walk_gate_ext.py
"""

import sys
import random
import datetime
import collections
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s23 = load("s23", BASE / "23_tail_integrated.py")     # oracle：row / r5 / r2 / win_ticks
s38 = load("s38", BASE / "38_tail_walk_gate.py")      # 38 的宇宙/统计口径（复用即同源）

STAKE = s23.STAKE
T60 = s23.T60
STAGES = ("t150", "t60", "listen")


# ── 主配置 = 现行落地形态（决策 #29） ─────────────────────────────────────

LIVE = {"t150": 43.0, "t60": None, "listen": None}

BASE_ROWS = 6686          # 行数 pin（现行 oracle 23）
BASE_SIG = 2080           # 信号 pin
BASE_PNL = 64.026354      # P&L pin
PREGATE_SIG, PREGATE_PNL = 2133, 35.092748   # 闸前基线 pin（2026-09-29 之前）


# ── 闸链（逐段独立 X；None = 该段不加闸） ─────────────────────────────────

def pass_gate(r, X):
    """该行过不过闸。X is None ⇒ 不加闸；walk 无值 ⇒ 放行（fail-open, 与 23/Go 同口径）。"""
    if X is None:
        return True
    return s23.walk_ok(r, X)


def chain_g(ticks, anchor, sd, date, outcome, gates):
    """三段递进链 + 逐段闸。返回 (rows, info)。

    info["gated"] = 本窗被闸的段（仅当价格/dev/σ 腿**本来达标**时才算——闸是唯一拦路者）;
    info["reroute"] = 被闸后改道重入的 (段, fill, walk)；没有任何信号 ⇒ 整窗死亡（由调用方判）。
    """
    if not ticks:
        return [], None
    info = {"gated": [], "reroute": None}
    rows = []
    head = ticks[0]
    if head["rem"] > T60:
        r = s23.row(head, anchor, sd, date, outcome, "t150")
        ok5 = s23.r5(r, strict_price=True)          # 段 1 价格腿严格大于（#26）
        if ok5 and pass_gate(r, gates.get("t150")):
            r["ok"] = True
            return [r], info
        if ok5:
            info["gated"].append("t150")
        rows.append(r)
        rest = [x for x in ticks if x["rem"] <= T60]
    else:
        rest = ticks                                 # 迟到接入：不伪造 t150 行
    if rest:
        t2 = rest[0]
        r2r = s23.row(t2, anchor, sd, date, outcome, "t60")
        if s23.r5(r2r) and pass_gate(r2r, gates.get("t60")):
            r2r["ok"] = True
            if info["gated"]:
                info["reroute"] = ("t60", r2r["fill"], r2r["walk"])
            return rows + [r2r], info
        if s23.r5(r2r):
            info["gated"].append("t60")
        rows.append(r2r)
        for x in rest[1:]:
            rl = s23.row(x, anchor, sd, date, outcome, "listen")
            if s23.r2(rl) and pass_gate(rl, gates.get("listen")):
                rl["ok"] = True
                if info["gated"]:
                    info["reroute"] = ("listen", rl["fill"], rl["walk"])
                rows.append(rl)
                return rows, info
            if s23.r2(rl):
                info["gated"].append("listen")
    return rows, info


def run(wins, gates):
    """跑一个 config，返回 (每条信号行, 计数 Counter)。信号行带 wid（窗下标）与 walk。"""
    sigs, stat = [], collections.Counter()
    for i, w in enumerate(wins):
        rows, info = chain_g(w["ticks"], w["anchor"], w["sd"],
                             w["date"], w["outcome"], gates)
        for r in rows:
            if r.get("ok"):
                r["wid"] = i
                sigs.append(r)
        if info:
            for g in info["gated"]:
                stat["gated_" + g] += 1
            stat["windows_touched"] += 1
    return sigs, stat


def desc(rows):
    n = len(rows)
    if not n:
        return "n=0"
    wr = sum(r["settle_won"] for r in rows) / n * 100
    return (f"n={n:<5} WR {wr:6.2f}%  输 {sum(1 for r in rows if not r['settle_won']):<3} "
            f"P&L {s38.pnl(rows):+8.2f}U")


def fmt_gates(gates):
    lab = []
    for s in STAGES:
        x = gates.get(s)
        lab.append(f"{s}:{'—' if x is None else f'{x:g}'}")
    return " ".join(lab)


def main():
    wins = s38.universe()
    print(f"宇宙：{len(wins)} 窗（pin 3640 有可判定 tick ∧ 有 σ 的窗）")

    # ── §0 口径自检 ──────────────────────────────────────────────────────
    print("\n" + "=" * 108)
    print("§0 口径自检（两把 pin 都要逐位对上，否则后面数字不可信）")
    pre, _ = run(wins, {"t150": None, "t60": None, "listen": None})
    live, live_stat = run(wins, LIVE)
    n, P = len(live), s38.pnl(live)
    wr = sum(r["settle_won"] for r in live) / n * 100
    ok1 = len(pre) == PREGATE_SIG and abs(s38.pnl(pre) - PREGATE_PNL) < 1e-4
    ok2 = n == BASE_SIG and abs(P - BASE_PNL) < 1e-4
    print(f"  闸前基线（无闸）      {desc(pre)}   pin {PREGATE_SIG} / +{PREGATE_PNL:.6f}U "
          f"{'✅' if ok1 else '❌'}")
    print(f"  现行 config（只闸 T150 X=43）{desc(live)}   pin {BASE_SIG} / +{BASE_PNL:.6f}U "
          f"{'✅' if ok2 else '❌'}")
    print(f"  现行 config 的行数（parity pin {BASE_ROWS}）："
          f"{sum(len(chain_g(w['ticks'], w['anchor'], w['sd'], w['date'], w['outcome'], LIVE)[0]) for w in wins)}")

    BD_live = s38.by_day(live)
    BD_pre = s38.by_day(pre)

    # ── §1 各段信号的 walk 分布（现行 config 下）────────────────────────
    print("\n" + "=" * 108)
    print("§1 现行 config 下各段信号的 walk（美元）= 闸如果要往下延，会碰到谁")
    print("     段          n     walk p10    中位    p90   |  <43 的笔数  <33 的笔数")
    for st in STAGES:
        v = sorted(r["walk"] for r in live if r["stage"] == st and r["walk"] is not None)
        if not v:
            continue
        c43 = sum(1 for x in v if x < 43.0)
        c33 = sum(1 for x in v if x < 33.0)
        print(f"     {st:<8} {len(v):>5} {v[len(v)//10]:10.1f} {v[len(v)//2]:7.1f} "
              f"{v[9*len(v)//10]:7.1f}   | {c43:>8} ({c43/len(v)*100:4.1f}%) {c33:>7} "
              f"({c33/len(v)*100:4.1f}%)")
    print("     ⚠️ 「<43 的笔数」是**现行 config 里真的入了场的笔**（改道进来的也在内）——")
    print("        闸往下延正是要拦这批；它与 §3 被闸计数不同（后者含改道链的相互影响）。")

    # walk 在窗内随时间长大吗（决定「延后一段再进」会发生什么）
    print("\n     walk 在窗内随时间长大（全场 3640 窗的可判定 tick，按 rem 桶）:")
    buckets = collections.defaultdict(list)
    for w in wins:
        for t in w["ticks"]:
            wv = s38.walk_of(t, w["anchor"])
            if wv is None:
                continue
            buckets[("rem>60" if t["rem"] > 60 else
                     "30<rem≤60" if t["rem"] > 30 else "rem≤30")].append(wv)
    for k in ("rem>60", "30<rem≤60", "rem≤30"):
        v = sorted(buckets[k])
        if v:
            print(f"       {k:<12} {len(v):>7}  tick   p10 {v[len(v)//10]:8.1f}  中位 "
                  f"{v[len(v)//2]:7.1f}  p90 {v[9*len(v)//10]:8.1f}")

    # ── §2 变体总表（阈值 43 / 33 为主）──────────────────────────────────
    print("\n" + "=" * 108)
    print("§2 逐段加闸总表（参照系 = 现行 config；Δ 为其与现行 config 的差，日级配对 bootstrap）")
    variants = [
        ("现行 config（只闸 T150 X=43）", LIVE, "参照"),
        ("（参考）只闸 T150，X 改 33", {"t150": 33.0, "t60": None, "listen": None}, "换"),
        ("＋闸 T60 X=43", {"t150": 43.0, "t60": 43.0, "listen": None}, "加"),
        ("＋闸 T60 X=33", {"t150": 43.0, "t60": 33.0, "listen": None}, "加"),
        ("＋闸 监听 X=43", {"t150": 43.0, "t60": None, "listen": 43.0}, "加"),
        ("＋闸 监听 X=33", {"t150": 43.0, "t60": None, "listen": 33.0}, "加"),
        ("＋闸 T60+监听 X=43", {"t150": 43.0, "t60": 43.0, "listen": 43.0}, "加"),
        ("＋闸 T60+监听 X=33", {"t150": 43.0, "t60": 33.0, "listen": 33.0}, "加"),
        ("＋闸 T60+监听 X=33/43", {"t150": 43.0, "t60": 33.0, "listen": 43.0}, "加"),
        ("＋闸 T60+监听 X=43/33", {"t150": 43.0, "t60": 43.0, "listen": 33.0}, "加"),
        ("撤 T150 闸、只闸 T60 X=43", {"t150": None, "t60": 43.0, "listen": None}, "换"),
        ("撤 T150 闸、只闸 T60+监听 X=43", {"t150": None, "t60": 43.0, "listen": 43.0}, "换"),
        ("只闸 监听 X=43（T150/T60 都不闸）", {"t150": None, "t60": None, "listen": 43.0}, "换"),
    ]
    print(f"   {'形态':<34}{'n':>5}{'WR':>8}{'P&L':>10}{'Δ vs 现行':>11}{'日级配对95%':>20}"
          f"{'被闸(t150/t60/li)':>18}")
    for lab, g, kind in variants:
        sig, st = run(wins, g)
        d = s38.pnl(sig) - P
        if kind == "参照":
            print(f"   {lab:<34}{len(sig):>5}"
                  f"{sum(r['settle_won'] for r in sig)/len(sig)*100:>7.2f}%"
                  f"{s38.pnl(sig):>+10.2f}{'—':>11}{'（参照）':>20}"
                  f"{st['gated_t150']:>8}/{st['gated_t60']}/{st['gated_listen']:<8}")
            continue
        lo, hi = s38.boot_delta(BD_live, s38.by_day(sig), seed=40)
        star = " ⭐" if lo > 0 else ("  " if d > 0 else " ⚠️")
        print(f"   {lab:<34}{len(sig):>5}"
              f"{sum(r['settle_won'] for r in sig)/len(sig)*100:>7.2f}%"
              f"{s38.pnl(sig):>+10.2f}{d:>+11.2f}   [{lo:+7.2f},{hi:+7.2f}]"
              f"{st['gated_t150']:>8}/{st['gated_t60']}/{st['gated_listen']:<8}{star}")
    print("   被闸计数 = 「价格/dev/σ 腿本来达标、闸是唯一拦路者」的行数（按段）。")
    print("   ⭐ = 日级配对 95% 区间下界 > 0（相对现行 config 的提升才叫影响）。")

    # ── §3 归因：T60/监听段被闸的是谁、砍掉后去哪 ────────────────────────
    print("\n" + "=" * 108)
    print("§3 归因（相对现行 config，逐段单独加闸）")
    live_by_wid = {r["wid"]: r for r in live}
    for lab, g in (("T60 X=43", {"t150": 43.0, "t60": 43.0, "listen": None}),
                   ("T60 X=33", {"t150": 43.0, "t60": 33.0, "listen": None}),
                   ("监听 X=43", {"t150": 43.0, "t60": None, "listen": 43.0}),
                   ("监听 X=33", {"t150": 43.0, "t60": None, "listen": 33.0})):
        sig, st = run(wins, g)
        new_by_wid = {r["wid"]: r for r in sig}
        dead = set(live_by_wid) - set(new_by_wid)          # 整窗死亡
        moved = set(new_by_wid) & set(live_by_wid)         # 改道重入（同窗换了行）
        moved = {k for k in moved
                 if new_by_wid[k]["stage"] != live_by_wid[k]["stage"]}
        dl = [live_by_wid[k] for k in dead]
        mv_new = [new_by_wid[k] for k in moved]
        mv_old = [live_by_wid[k] for k in moved]
        print(f"\n   【{lab}】被闸 {st['gated_t60']}（t60）+ {st['gated_listen']}（listen）笔")
        if dl:
            lose = sum(1 for r in dl if not r["settle_won"])
            print(f"     整窗死亡 {len(dl):>3} 笔：输 {lose}（{lose/len(dl)*100:.1f}%）"
                  f"  P&L {s38.pnl(dl):+.2f}U ⇒ **省下 {-s38.pnl(dl):+.2f}U**")
        if mv_new:
            fls = sorted(r["fill"] for r in mv_new)
            print(f"     改道重入 {len(mv_new):>3} 笔：新 fill 中位 {fls[len(fls)//2]:.3f}"
                  f"  新段 {collections.Counter(r['stage'] for r in mv_new).most_common()}"
                  f"  输 {sum(1 for r in mv_new if not r['settle_won'])}")
            print(f"       同一批窗：现行 {s38.pnl(mv_old):+.2f}U → 改道后 "
                  f"{s38.pnl(mv_new):+.2f}U ⇒ **改道税 {s38.pnl(mv_old) - s38.pnl(mv_new):+.2f}U**")
        dd = s38.pnl(sig) - P
        print(f"     ⇒ 净 Δ = {dd:+.2f}U")

    # ── §4 阈值扫描（每段独立，叠加在现行 config 上）─────────────────────
    print("\n" + "=" * 108)
    print("§4 阈值扫描：把闸移到某一段、X 从 20 扫到 100（Δ vs 现行 config，日级配对 95%）")
    XS = (20.0, 25.0, 30.0, 33.0, 35.0, 40.0, 43.0, 50.0, 60.0, 70.0, 80.0, 100.0)
    for lab, mk in (("只加 T60 段闸", lambda X: {"t150": 43.0, "t60": X, "listen": None}),
                    ("只加监听段闸", lambda X: {"t150": 43.0, "t60": None, "listen": X}),
                    ("加 T60+监听段闸", lambda X: {"t150": 43.0, "t60": X, "listen": X})):
        print(f"\n   【{lab}】")
        print("      X    n     被闸   输   WR      P&L       Δ        日级配对95%")
        for X in XS:
            sig, st = run(wins, mk(X))
            ng = st["gated_t60"] + st["gated_listen"]
            d = s38.pnl(sig) - P
            lo, hi = s38.boot_delta(BD_live, s38.by_day(sig), seed=40)
            mark = " ⭐" if lo > 0 else ""
            print(f"     {X:5.1f}{len(sig):>6}{ng:>7}{sum(1 for r in sig if not r['settle_won']):>5}"
                  f"  {sum(r['settle_won'] for r in sig)/len(sig)*100:6.2f}% {s38.pnl(sig):+8.2f}U"
                  f" {d:+8.2f}U  [{lo:+7.2f},{hi:+7.2f}]{mark}")

    # ── §5 分半稳定性（只取 §4 里最好且方向为正的形态）──────────────────
    print("\n" + "=" * 108)
    print("§5 分半稳定性（把 14 天切两半，各自对现行 config 重算 Δ）")
    days = sorted(set(w["date"] for w in wins))
    mid = days[len(days) // 2]
    print(f"   切点 {mid}")
    for lab, g in (("＋闸 T60 X=43", {"t150": 43.0, "t60": 43.0, "listen": None}),
                   ("＋闸 T60 X=33", {"t150": 43.0, "t60": 33.0, "listen": None}),
                   ("＋闸 监听 X=43", {"t150": 43.0, "t60": None, "listen": 43.0}),
                   ("＋闸 监听 X=33", {"t150": 43.0, "t60": None, "listen": 33.0}),
                   ("＋闸 T60+监听 X=43", {"t150": 43.0, "t60": 43.0, "listen": 43.0}),
                   ("＋闸 T60+监听 X=33", {"t150": 43.0, "t60": 33.0, "listen": 33.0})):
        line = []
        for tag, sel in ((f"h1(<{mid})", lambda w: w["date"] < mid),
                         (f"h2(≥{mid})", lambda w: w["date"] >= mid)):
            sub = [w for w in wins if sel(w)]
            ref, _ = run(sub, LIVE)
            sig, _ = run(sub, g)
            dd = s38.by_day(sig)
            lo, hi = s38.boot_delta(s38.by_day(ref), dd, seed=40)
            line.append(f"{tag} Δ {s38.pnl(sig) - s38.pnl(ref):+7.2f}U [{lo:+6.2f},{hi:+6.2f}]")
        print(f"   {lab:<22} " + "   ".join(line))

    # ── §6 walk 在 T60/监听段还有没有判别力（闸值不值的机制检验）─────────
    print("\n" + "=" * 108)
    print("§6 机制检验：T60 段信号里，walk 高低与输率有关吗（现行 config 口径, 按精确 fill 分层）")
    import numpy as np
    for st_ in ("t60", "listen"):
        R = [r for r in live if r["stage"] == st_ and r["walk"] is not None]
        if len(R) < 30:
            print(f"   {st_} 段样本薄（n={len(R)}），跳过")
            continue
        y = np.array([0 if r["settle_won"] else 1 for r in R])
        wv = np.array([r["walk"] for r in R])
        fl = np.array([r["fill"] for r in R])
        obs, p, nl = s38.strat_perm(wv, y, fl, minn=6)
        # 把 X 取在本段中位数上，看「低 walk 半」与「高 walk 半」的输率
        med = float(np.median(wv))
        lo_g = [r for r in R if r["walk"] < med]
        hi_g = [r for r in R if r["walk"] >= med]
        lrlo = sum(1 for r in lo_g if not r["settle_won"]) / len(lo_g) * 100
        lrhi = sum(1 for r in hi_g if not r["settle_won"]) / len(hi_g) * 100
        print(f"   {st_} 段（n={len(R)}，walk 中位 {med:.1f}）：低半输率 {lrlo:.2f}% vs "
              f"高半 {lrhi:.2f}%（差 {lrlo-lrhi:+.2f}pp）；按精确 fill 分层后置换 "
              f"p={p:.4f}（{nl} 层）")
        for X in (33.0, 43.0):
            k = [r for r in R if r["walk"] < X]
            if k:
                print(f"       walk < {X:g} 的 {len(k):>3} 笔（{len(k)/len(R)*100:4.1f}%）："
                      f"输率 {sum(1 for r in k if not r['settle_won'])/len(k)*100:5.2f}%"
                      f"  均 fill {sum(r['fill'] for r in k)/len(k):.4f}"
                      f"  P&L {s38.pnl(k):+7.2f}U")
            else:
                print(f"       walk < {X:g} 的 0 笔")
    print("   ⇒ 若这两段的 walk 层次置换 p 都大、低/高半输率差 ≈0 ⇒ 闸在这里没有判别力，")
    print("      多砍的笔只是把成交推到更高价（改道税），不会提高胜率。")

    # ── §7 监听闸细账（它与 T60 闸不同：改道是 listen→listen，§3 看不见）──
    print("\n" + "=" * 108)
    print("§7 监听段闸细账：整窗死亡 vs 同段改道（现行 config → 加监听闸）")
    for X in (20.0, 33.0, 43.0):
        sig, st = run(wins, {"t150": 43.0, "t60": None, "listen": X})
        new_by_wid = {r["wid"]: r for r in sig}
        dead = [live_by_wid[k] for k in set(live_by_wid) - set(new_by_wid)]
        same = [k for k in set(live_by_wid) & set(new_by_wid)
                if live_by_wid[k]["stage"] == "listen" and new_by_wid[k] is not None]
        disp = [(live_by_wid[k], new_by_wid[k]) for k in same
                if live_by_wid[k]["rem"] != new_by_wid[k]["rem"]]
        print(f"\n   【监听 X={X:g}】Δ {s38.pnl(sig) - P:+.2f}U   n {len(live)} → {len(sig)}")
        if dead:
            lose = sum(1 for r in dead if not r["settle_won"])
            print(f"     整窗死亡 {len(dead):>3}：输 {lose}（{lose/len(dead)*100:.1f}%）"
                  f"  P&L {s38.pnl(dead):+.2f}U ⇒ 省下 {-s38.pnl(dead):+.2f}U")
        if disp:
            fd = [b["fill"] - a["fill"] for a, b in disp]
            fd.sort()
            dd_ = s38.pnl([b for _, b in disp]) - s38.pnl([a for a, _ in disp])
            late = sum(1 for a, b in disp if b["rem"] < a["rem"])
            print(f"     同段改道 {len(disp):>3}（其中 {late} 笔入场更晚）："
                  f"新 fill 中位 {sorted(b['fill'] for _, b in disp)[len(disp)//2]:.4f}"
                  f"（fill 变化中位 {fd[len(fd)//2]:+.4f}）"
                  f"  这批窗 P&L {s38.pnl([a for a,_ in disp]):+.2f} → "
                  f"{s38.pnl([b for _,b in disp]):+.2f}U（Δ {dd_:+.2f}U）")
        bd, gd = s38.by_day(live), s38.by_day(sig)
        per = sorted(((k, gd.get(k, 0) - bd.get(k, 0)) for k in set(list(bd) + list(gd))),
                     key=lambda kv: -abs(kv[1]))
        tot = sum(v for _, v in per)
        print(f"     逐日 Δ 合计 {tot:+.2f}U；|Δ| 最大的 5 天："
              + "  ".join(f"{k[5:]} {v:+.2f}" for k, v in per[:5]))
        print(f"     正 Δ 天 {sum(1 for _, v in per if v > 1e-9)}/{len(per)}；"
              f"最大单日贡献 {per[0][1]:+.2f}U（占 {abs(per[0][1])/max(abs(tot),1e-9)*100:.0f}%）")

    # ── §9 监听闸是「价格腿伪装」吗（fill 地板对照）─────────────────────
    print("\n" + "=" * 108)
    print("§9 监听段的 walk 与 fill 是一回事吗（若闸的效果 = 砍廉价角落，闸就没带新信息）")
    import math
    L = [r for r in live if r["stage"] == "listen" and r["walk"] is not None]

    def _rank(v):
        o = sorted(range(len(v)), key=lambda i: v[i])
        r_ = [0] * len(v)
        for pos, i in enumerate(o):
            r_[i] = pos
        return r_

    rw, rf = _rank([r["walk"] for r in L]), _rank([r["fill"] for r in L])
    n_ = len(rw)
    rho = ((sum(rw[i] * rf[i] for i in range(n_)) - n_ * (n_ - 1) ** 2 / 4)
           / math.sqrt((sum(x * x for x in rw) - n_ * (n_ - 1) ** 2 / 4)
                       * (sum(x * x for x in rf) - n_ * (n_ - 1) ** 2 / 4)))
    print(f"   listen 段 n={n_}：walk ↔ fill 的 Spearman ρ = {rho:+.3f}"
          f"（正 ⇒ 低 walk 与便宜价同向）")
    print("      fill 桶           n    输率     P&L      walk 中位")
    for lo_, hi_ in ((0.80, 0.82), (0.83, 0.90), (0.91, 0.97), (0.98, 1.00)):
        g = [r for r in L if lo_ <= r["fill"] <= hi_ + 1e-9]
        if not g:
            continue
        print(f"      [{lo_:.2f},{hi_:.2f}] {len(g):>7}  "
              f"{sum(1 for r in g if not r['settle_won'])/len(g)*100:5.2f}%  "
              f"{s38.pnl(g):+8.2f}U  {sorted(r['walk'] for r in g)[len(g)//2]:8.1f}")
    cheap = [r for r in L if r["fill"] <= 0.82 + 1e-9]
    rest = [r for r in L if r["fill"] > 0.82 + 1e-9]
    print(f"      廉价角落 ≤0.82：{len(cheap)} 笔（占 {len(cheap)/len(L)*100:.1f}%），其中 walk<43 占 "
          f"{sum(1 for r in cheap if r['walk'] < 43)/max(1, len(cheap))*100:.0f}%"
          f"  vs  >0.82 里 walk<43 占 {sum(1 for r in rest if r['walk'] < 43)/max(1, len(rest))*100:.0f}%")

    def chain_fillfloor(ticks, anchor, sd, date, outcome, floor):
        """对照口径：listen 段跳过 `fill ≤ floor` 的 tick（链继续），其余与现行 config 完全相同
        ——用来回答「闸的效果能不能被一条更直接的价格腿复现」。"""
        if not ticks:
            return [], None
        rows, head = [], ticks[0]
        if head["rem"] > T60:
            r = s23.row(head, anchor, sd, date, outcome, "t150")
            if s23.r5(r, strict_price=True) and s23.walk_ok(r, 43.0):
                r["ok"] = True
                return [r], None
            rows.append(r)
            rest_ = [x for x in ticks if x["rem"] <= T60]
        else:
            rest_ = ticks
        if rest_:
            t2 = rest_[0]
            r2r = s23.row(t2, anchor, sd, date, outcome, "t60")
            if s23.r5(r2r):
                r2r["ok"] = True
                return rows + [r2r], None
            rows.append(r2r)
            for x in rest_[1:]:
                rl = s23.row(x, anchor, sd, date, outcome, "listen")
                if s23.r2(rl) and rl["fill"] > floor:
                    rl["ok"] = True
                    rows.append(rl)
                    return rows, None
        return rows, None

    print("\n   对照：把闸换成一根更直接的**价格地板**（listen 段跳过 fill ≤ X 的 tick）")
    print("      X        n     Δ vs 现行    日级配对95%    砍掉 listen 行")
    for floor in (0.80, 0.81, 0.82, 0.83, 0.85, 0.90):
        sig = []
        for i, w in enumerate(wins):
            rr, _ = chain_fillfloor(w["ticks"], w["anchor"], w["sd"],
                                    w["date"], w["outcome"], floor)
            for r in rr:
                if r.get("ok"):
                    r["wid"] = i
                    sig.append(r)
        d = s38.pnl(sig) - P
        lo2, hi2 = s38.boot_delta(BD_live, s38.by_day(sig), seed=40)
        nch = sum(1 for r in L if r["fill"] <= floor + 1e-9)
        print(f"      >{floor:.2f} {len(sig):>5}  {d:+8.2f}U  [{lo2:+7.2f},{hi2:+7.2f}]  {nch:>6}")

    # ── §8 λ* 判据（与 38 §5 同口径）────────────────────────────────────
    print("\n" + "=" * 108)
    print("§8 λ* 判据（平衡点输率 = 1 − 均 fill；余量 = λ* − 实测输率）")
    print("     集合                              n    均 fill   λ*      实测输率   余量")
    for lab, g in (("现行 config 全体", LIVE),
                   ("＋闸 T60+监听 X=43 全体", {"t150": 43.0, "t60": 43.0, "listen": 43.0}),
                   ("＋闸 T60+监听 X=33 全体", {"t150": 43.0, "t60": 33.0, "listen": 33.0})):
        sig, _ = run(wins, g)
        f_ = sum(r["fill"] for r in sig) / len(sig)
        lam = (1 - f_) * 100
        lr = sum(1 for r in sig if not r["settle_won"]) / len(sig) * 100
        print(f"     {lab:<32} {len(sig):>5}  {f_:.4f}  {lam:5.2f}%  {lr:6.2f}%  {lam-lr:+6.2f}pp")


if __name__ == "__main__":
    main()
