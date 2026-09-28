#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘: **T=150 段的价格腿下限**从 `> 0.80` 抬到 `> 0.85`，对现行引擎有没有改善？
（2026-09-29 用户提问）

背景 = 决策 #26: 2026-09-26 起 T=150 段的价格腿是**严格大于 0.80**（其余两段不动, 仍 ≥0.80）。
当时的依据是「0.80 这一格是两个样本里唯一的负 EV 档」（实盘 4 笔 WR 50% / −14.38U;
14 天 n=17 WR 76.47% / −1.50U）。本轮问的是: 再往上抬一档（0.85）呢？

    T=150  首个可判定 tick 且 rem ≤ 150 → 判 ⑤, 价格腿 `hot > 0.85`（本轮改这里）
    T=60   前段没出信号, 首个可判定 tick 且 rem ≤ 60 → 再判 ⑤, 价格腿 `hot ≥ 0.80`（不动）
    监听   前两段都没信号, 此后每秒判 ②, 价格腿 `hot ≥ 0.80`（不动）

⚠️ **结构性事实（第二节证明）**: 抬 T=150 的价格下限只会让信号**丢失**或**推迟**（改道到
T=60/监听段），**不可能新增信号** —— 因为被拦下的窗走的正是基线那条链的后半段。
所以本轮的判据是: 被拦下那批的 EV 是不是负的。

⚠️ 阶梯（第三节）只看**形态**（一整段同号 = 平台; 只冒一格 = 噪声尖峰），
不挑最优格 —— 项目一贯判据（见 29/31/33 的先例）。

用法: python/venv/bin/python python/v4/35_tail_t150_pricefloor.py
"""
import sys
import glob
import json
import random
import datetime
import collections
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from v2.lib import load_events                                    # noqa: E402

STAKE = 2.0
FLOOR = 0.85           # 本轮题面的下限
LADDER = (0.80, 0.82, 0.85, 0.88, 0.90)
STAGES = ("t150", "t60", "listen")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s13 = load("s13", BASE / "13_tail_sweep.py")
s23 = load("s23", BASE / "23_tail_integrated.py")
s33 = load("s33", BASE / "33_tail_dev2sd.py")

pl = s33.pl
line = s33.line
paired_ci = s33.paired_ci
by_day = s33.by_day


def r5_floor(floor, strict=True):
    """⑤ 本体（dev ≥ 63 ∨ (sd ≥ 40 ∧ dev ≥ sd)），价格腿换成指定下限与算子。

    非价格的两条腿**逐字**照抄 s23.r5（DEV_USD / SD_MIN_USD / 1.0 三个常量都从那边取,
    不在这里重新写数字）——下面的自检会断言它逐窗复现 23 的基线。
    """
    op = (lambda v: v > floor) if strict else (lambda v: v >= floor)

    def f(r):
        if not op(r["fill"]):
            return False
        if r["dev"] >= s23.DEV_USD:
            return True
        return (r["sd"] is not None and r["sd"] >= s23.SD_MIN_USD
                and r["sig"] is not None and r["sig"] >= 1.0)
    return f


# 基线 = 现行引擎口径（T150 严格 > 0.80; T60 ⑤ ≥0.80; 监听 ② ≥0.80）
R150_BASE = r5_floor(s23.P_FLOOR, True)
R60 = r5_floor(s23.P_FLOOR, False)
RLISTEN = s23.r2
BASE_RULES = (R150_BASE, R60, RLISTEN)


def rules_with_floor(floor, strict=True):
    return (r5_floor(floor, strict), R60, RLISTEN)


def main():
    ev = load_events("data/btc")
    hr = s13.hist_ranges(ev)
    rng = random.Random(s33.SEED)

    base_sigs, base_map, _ = s33.run(ev, hr, BASE_RULES)
    var_sigs, var_map, _ = s33.run(ev, hr, rules_with_floor(FLOOR))

    # ── 自检: 本脚本的 r5 复刻必须逐窗复现 23 的 baseline ────────────────
    ref_sigs, ref_map, _ = s33.run(ev, hr, s33.BASE_RULES)
    ok = (len(base_sigs) == len(ref_sigs)
          and all(a["win"] in ref_map and abs(pl([a]) - pl([ref_map[a["win"]]])) < 1e-12
                  for a in base_sigs))
    print(f"  自检: r5_floor(0.80, 严格) 复刻 23 的基线 —— "
          f"{'✅ 逐窗一致' if ok else '❌ 不一致, 下面的数字作废'}"
          f"（n={len(base_sigs)}, P&L {pl(base_sigs):+.2f}U）\n")

    print("=" * 108)
    print(f"一、题面：T=150 段价格腿 > 0.80 → > {FLOOR:.2f}（T=60 与监听段一字不动）")
    print("=" * 108)
    line("基线（现行：T150 > 0.80）", base_sigs, rng)
    line(f"题面（T150 > {FLOOR:.2f}）", var_sigs, rng)
    p = paired_ci(base_map, var_map, rng)
    print(f"\n  配对 Δ（题面 − 基线）P&L: 点估计 {pl(var_sigs)-pl(base_sigs):+.2f}U, "
          f"日级 bootstrap 95%CI [{p[0]:+.1f}, {p[1]:+.1f}]U")
    print(f"  信号数: {len(base_sigs)} → {len(var_sigs)}"
          f"（{'−' if len(var_sigs) <= len(base_sigs) else '+'}"
          f"{abs(len(base_sigs)-len(var_sigs))}）")
    print("\n  按段分解（题面这一列的信号落在哪一段）:")
    for s in STAGES:
        line(f"    {s}", [r for r in var_sigs if r["stage"] == s], rng, width=22)
    print("\n  基线按段分解（对照）:")
    for s in STAGES:
        line(f"    {s}", [r for r in base_sigs if r["stage"] == s], rng, width=22)

    # ── 二、结构性事实: 只有「丢失」与「改道」──────────────────────────
    print("\n" + "=" * 108)
    print("二、抬下限只会丢信号或推迟入场（不可能新增）—— 被拦下那批去了哪")
    print("=" * 108)
    dropped = [(base_map[w], var_map.get(w))
               for w in base_map
               if base_map[w]["stage"] == "t150" and base_map[w]["fill"] <= FLOOR]
    no_sig = [b for b, v in dropped if v is None]
    moved = [(b, v) for b, v in dropped if v is not None]
    print(f"  基线 T=150 段信号里价格 ≤ {FLOOR:.2f} 的: {len(dropped)} 笔"
          f"（占该段 {len(dropped)/sum(1 for r in base_sigs if r['stage']=='t150')*100:.1f}%）")
    print(f"    ├─ 变体整窗**不下注**（后续段也没达标）: {len(no_sig)} 笔"
          f"  基线 P&L {pl(no_sig):+.2f}U"
          f"（{'赢' if no_sig and sum(r['settle_won'] for r in no_sig)/len(no_sig) > 0.5 else '输'}率 "
          f"{sum(r['settle_won'] for r in no_sig)/len(no_sig)*100 if no_sig else 0:.1f}%）")
    print(f"    └─ 变体**推迟入场**到后续段: {len(moved)} 笔")
    if moved:
        print(f"       {'去向':<10}{'n':>5}{'基线价':>9}{'变体价':>9}{'基线P&L':>10}"
              f"{'变体P&L':>10}{'ΔP&L':>9}{'变体rem':>9}")
        for s in STAGES:
            g = [(b, v) for b, v in moved if v["stage"] == s]
            if not g:
                continue
            b_, v_ = [x[0] for x in g], [x[1] for x in g]
            print(f"       {s:<10}{len(g):>5}"
                  f"{sum(r['fill'] for r in b_)/len(b_):>9.4f}"
                  f"{sum(r['fill'] for r in v_)/len(v_):>9.4f}"
                  f"{pl(b_):>+10.2f}{pl(v_):>+10.2f}{pl(v_)-pl(b_):>+9.2f}"
                  f"{sorted(r['rem'] for r in v_)[len(v_ )//2]:>9}")
    print(f"\n  ⇒ 被拦下那批（{len(dropped)} 笔）合计: 基线 {pl([b for b,_ in dropped]):+.2f}U → "
          f"变体 {pl([v for _,v in dropped if v]):+.2f}U, "
          f"Δ {pl([v for _,v in dropped if v])-pl([b for b,_ in dropped]):+.2f}U")
    print("     （抬下限的全部收益/损失都在这一行里 —— 未受影响的窗两边完全一样）")
    fl = [b for b, _ in dropped if b["settle_won"]]
    print(f"  被拦下那批整体胜负: 赢 {len(fl)} / 输 {len(dropped)-len(fl)}"
          f"（WR {len(fl)/len(dropped)*100:.1f}%）—— 但去向一分就两头分明:")
    if no_sig:
        print(f"    整窗不下注的 {len(no_sig)} 笔: WR "
              f"{sum(r['settle_won'] for r in no_sig)/len(no_sig)*100:.1f}%"
              f"（基线在这些窗是**净亏**的）")
    if moved:
        print(f"    推迟入场的 {len(moved)} 笔: WR "
              f"{sum(v['settle_won'] for _, v in moved)/len(moved)*100:.1f}%（原样赢下）"
              f", 只是**买贵了**（均价 "
              f"{sum(b['fill'] for b, _ in moved)/len(moved):.4f} → "
              f"{sum(v['fill'] for _, v in moved)/len(moved):.4f}）")

    # ── 三、阶梯（只读形态）────────────────────────────────────────────
    print("\n" + "=" * 108)
    print("三、价格下限阶梯（形态判读: 一整段同号 = 平台; 只冒一格 = 噪声尖峰）")
    print("=" * 108)
    print(f"  {'T150 价格腿':<16}{'n':>7}{'WR':>9}{'均价':>9}{'EV/注':>10}{'P&L':>10}"
          f"{'t150段n':>9}{'配对Δ':>10}{'95%CI':>18}")
    ladder = {}
    for f in LADDER:
        sg, mp, _ = s33.run(ev, hr, rules_with_floor(f))
        ladder[f] = (sg, mp)
        pc = paired_ci(base_map, mp, rng)
        n = len(sg)
        wr = sum(r["settle_won"] for r in sg) / n * 100
        n150 = sum(1 for r in sg if r["stage"] == "t150")
        tag = "  ←现行" if f == 0.80 else ("  ←题面" if f == FLOOR else "")
        print(f"  {'hot > ' + format(f, '.2f'):<16}{n:>7}{wr:>8.2f}%"
              f"{sum(r['fill'] for r in sg)/n:>9.4f}{pl(sg)/n:>+10.4f}{pl(sg):>+10.2f}"
              f"{n150:>9}{pl(sg)-pl(base_sigs):>+10.2f}"
              f"   [{pc[0]:+.1f},{pc[1]:+.1f}]U{tag}")
    sg85, _, _ = s33.run(ev, hr, rules_with_floor(FLOOR, strict=False))
    print(f"  {'hot ≥ ' + format(FLOOR, '.2f'):<16}{len(sg85):>7}"
          f"{sum(r['settle_won'] for r in sg85)/len(sg85)*100:>8.2f}%"
          f"{sum(r['fill'] for r in sg85)/len(sg85):>9.4f}"
          f"{pl(sg85)/len(sg85):>+10.4f}{pl(sg85):>+10.2f}"
          f"{sum(1 for r in sg85 if r['stage']=='t150'):>9}{pl(sg85)-pl(base_sigs):>+10.2f}"
          f"   （算子对照: 非严格 ≥）")

    # ── 四、基线 T150 段按成交价分桶（梯度证据）────────────────────────
    print("\n" + "=" * 108)
    print("四、基线 T=150 段信号按**成交价**分桶（#26 那条证据的延伸: 梯度上哪几格是负的）")
    print("=" * 108)
    b150 = [r for r in base_sigs if r["stage"] == "t150"]
    edges = [(0.80, 0.82), (0.82, 0.85), (0.85, 0.88), (0.88, 0.92),
             (0.92, 0.96), (0.96, 1.01)]
    print(f"  {'价格桶':<16}{'n':>6}{'WR':>9}{'均价':>9}{'EV/注':>10}{'P&L':>10}{'输':>5}")
    for lo, hi in edges:
        g = [r for r in b150 if lo <= r["fill"] < hi]
        if not g:
            print(f"  {f'[{lo:.2f},{hi:.2f})':<16}{0:>6}")
            continue
        n = len(g)
        print(f"  {f'[{lo:.2f},{hi:.2f})':<16}{n:>6}"
              f"{sum(r['settle_won'] for r in g)/n*100:>8.2f}%"
              f"{sum(r['fill'] for r in g)/n:>9.4f}{pl(g)/n:>+10.4f}{pl(g):>+10.2f}"
              f"{sum(1 for r in g if not r['settle_won']):>5}")
    for lo, hi in ((0.80, 0.85), (0.85, 1.01)):
        g = [r for r in b150 if lo <= r["fill"] < hi]
        n = len(g)
        print(f"  {f'合计 [{lo:.2f},{hi:.2f})':<16}{n:>6}"
              f"{sum(r['settle_won'] for r in g)/n*100:>8.2f}%"
              f"{sum(r['fill'] for r in g)/n:>9.4f}{pl(g)/n:>+10.4f}{pl(g):>+10.2f}"
              f"{sum(1 for r in g if not r['settle_won']):>5}")

    # ── 五、被拦下那批逐窗明细 ────────────────────────────────────────
    print("\n" + "=" * 108)
    print(f"五、被 > {FLOOR:.2f} 拦下的 {len(dropped)} 笔逐窗明细")
    print("=" * 108)
    print(f"  {'日期':<12}{'基线价':>8}{'dev(美元)':>10}{'sd(美元)':>9}{'sig':>6}"
          f"{'结果':>6}   变体去了哪")
    for b, v in sorted(dropped, key=lambda x: (x[0]["date"], -x[0]["rem"])):
        tail = (f"{v['stage']} 段 rem={v['rem']} 价 {v['fill']:.4f} "
                f"({'赢' if v['settle_won'] else '输'})") if v else "整窗不下注"
        print(f"  {b['date']:<12}{b['fill']:>8.4f}{b['dev']:>10.1f}{b['sd']:>9.1f}"
              f"{b['sig']:>6.2f}{('赢' if b['settle_won'] else '输'):>6}   {tail}")

    # ── 六、实盘/纸面样本外（data/tail-live 的 t150 段）─────────────────
    print("\n" + "=" * 108)
    print("六、实盘/纸面样本外: data/tail-live 09-24~28 的 **t150 段**行按成交价分桶")
    print("=" * 108)
    live = []
    for f in sorted(glob.glob("data/tail-live/tail_2026-*.jsonl")):
        for ln in open(f):
            try:
                r = json.loads(ln)
            except Exception:
                continue
            # 只看 **t150 段判定达标** 的行; 价格取 `hot_ask`（= 回测里的 fill 同一读数）
            if (r.get("kind") == "snap" and r.get("stage") == "t150"
                    and r.get("ok") and r.get("hot_ask")):
                live.append(r)
    pos = [r for r in live if "pnl" in r]          # 有仓位、已结算的行
    print(f"  t150 段达标信号 {len(live)} 条, 其中有仓位且已结算 {len(pos)} 条"
          f"（10U/笔, 挂单限价即 hot_ask）")
    print(f"  {'价格桶':<16}{'信号n':>7}{'有仓n':>7}{'WR':>9}{'P&L':>10}{'EV/笔':>10}")
    for lo, hi in ((0.80, 0.82), (0.82, 0.85), (0.85, 0.90), (0.90, 0.95),
                   (0.95, 1.01), (0.80, 0.85), (0.85, 1.01)):
        g = [r for r in live if lo <= r["hot_ask"] < hi]
        gp = [r for r in pos if lo <= r["hot_ask"] < hi]
        lab = ("合计 " if (lo, hi) in ((0.80, 0.85), (0.85, 1.01)) else "") + f"[{lo:.2f},{hi:.2f})"
        if not gp:
            print(f"  {lab:<16}{len(g):>7}{len(gp):>7}{'—':>9}{'—':>10}{'—':>10}")
            continue
        wr = sum(1 for r in gp if r.get("won")) / len(gp) * 100
        pnl = sum(r.get("pnl") or 0 for r in gp)
        print(f"  {lab:<16}{len(g):>7}{len(gp):>7}{wr:>8.1f}%{pnl:>+10.2f}{pnl/len(gp):>+10.4f}")
    print("  ⚠️ 实盘是**挂单等成交**样本（stake 10U, 与回测 2U 不同, 只作方向性核对;")
    print("     回测那一列是「快照瞬间即成交」, 两者不是同一个估计量）。")


if __name__ == "__main__":
    main()
