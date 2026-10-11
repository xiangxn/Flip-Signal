#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘 · 全新数据面 第 5 步：段内条件扫描 + 规则参数扫描

54 号把条件三段统用，掩盖了段间差异（t150 段 = 691 笔里 32 个输单，
WR 95.37% vs t60 段 97.98%）。本脚本分两步：
  A 段内条件：条件只加在**某一段**（extra 只给那一段），其余段一字不动
  B 规则参数：直接改 Rules（dev_min / walk_min / floor_min_price / price_min /
              sd_min / σ 腿开关），重跑整条链
"""
from __future__ import annotations

import importlib.util
import os
import sys

import numpy as np

_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("fresh51", os.path.join(_here, "51_tail_fresh_chain.py"))
C = importlib.util.module_from_spec(_spec)
sys.modules["fresh51"] = C
_spec.loader.exec_module(C)

EPS = 1e-9
STAGE_CN = {1: "t150", 2: "t60", 3: "listen"}


def mats(panel):
    sd = panel.sd[:, None]
    f = {
        "p": panel.p, "dev": panel.dev, "walk": panel.walk, "basis": panel.basis,
        "sd": np.repeat(sd, panel.p.shape[1], axis=1),
        "rem": np.where(panel.rem > -900, panel.rem.astype(float), -1e18),
        "walk/sd": panel.walk / np.maximum(sd, EPS),
        "dev/sd": panel.dev / np.maximum(sd, EPS),
        "basis/sd": panel.basis / np.maximum(sd, EPS),
        "raw_move": panel.spot - panel.anchor[:, None],
        "raw_tw": panel.twap - panel.anchor[:, None],
    }
    return f


def main() -> int:
    ws = C.load()
    panel, skip = C.build_panel(ws)
    rules = C.Rules()
    prep = C.prepare(panel, rules, skip)
    base = C.run(prep)
    B = float(base.pnl.sum()); N = int(base.fired.sum())
    t = C.entry_table(panel, base)
    F = mats(panel)
    days = sorted(set(panel.day)); mid = days[len(days) // 2]
    h1 = np.array([d < mid for d in panel.day]); h2 = ~h1
    print(f"基线 n={N} 毛 {B:+.3f}U\n")

    def entry_series(name):
        if name in t:
            return np.asarray(t[name], float)
        if "/" in name:
            a, b = name.split("/")
            return t[a] / np.maximum(t[b], EPS)
        if name == "raw_move":
            return t["spot"] - t["anchor"]
        if name == "raw_tw":
            return t["twap"] - t["anchor"]
        raise KeyError(name)

    print("=" * 112)
    print("【A 段内条件】条件只加在某一段（≥/≤ 分位）；Δn/ΔP&L 都是整链口径")
    print(f"  {'段':<7}{'特征':<10}{'方向':<5}{'阈值':>9}{'n':>7}{'Δn':>6}{'ΔP&L':>9}"
          f"{'95%下界':>10}{'95%上界':>10}{'h1':>9}{'h2':>9}")
    rows = []
    for stage in (1, 2, 3):
        for name, M in F.items():
            v = entry_series(name)
            cuts = sorted(set(float(np.percentile(v, x)) for x in (10, 25, 40, 50, 60, 75, 90)))
            for dname, dfn in (("ge", lambda m, x: m >= x), ("le", lambda m, x: m <= x)):
                for X in cuts:
                    ex = dfn(M, X)
                    kw = {"extra150": ex} if stage == 1 else (
                        {"extra60": ex} if stage == 2 else {"extralisten": ex})
                    e = C.run(prep, **kw)
                    d = float(e.pnl.sum()) - B
                    n = int(e.fired.sum())
                    if abs(d) < 1e-9 and n == N:
                        continue
                    _, lo, hi = C.boot_delta(panel, e, base, n=1500, seed=55)
                    dh1 = float(e.pnl[h1].sum() - base.pnl[h1].sum())
                    dh2 = float(e.pnl[h2].sum() - base.pnl[h2].sum())
                    rows.append((stage, name, dname, X, n, n - N, d, lo, hi, dh1, dh2))
                    print(f"  {STAGE_CN[stage]:<7}{name:<10}{dname:<5}{X:>9.2f}{n:>7}{n-N:>+6}"
                          f"{d:>+9.2f}{lo:>+10.2f}{hi:>+10.2f}{dh1:>+9.2f}{dh2:>+9.2f}")
    rows.sort(key=lambda r: -r[6])
    print("\n  ▼ 段内条件 ΔP&L 前 12：")
    for r in rows[:12]:
        print(f"    {STAGE_CN[r[0]]:<7}{r[1]:<10}{r[2]:<5}{r[3]:>9.2f}{r[4]:>7}{r[5]:>+6}"
              f"{r[6]:>+9.2f}{r[7]:>+10.2f}{r[8]:>+10.2f}{r[9]:>+9.2f}{r[10]:>+9.2f}")

    print("\n" + "=" * 112)
    print("【B 规则参数扫描】直接改 Rules 重跑整链（不是加掩码）")
    print(f"  {'改动':<34}{'n':>7}{'Δn':>6}{'毛P&L':>10}{'ΔP&L':>9}{'95%下界':>10}{'95%上界':>10}"
          f"{'h1':>9}{'h2':>9}  段分布")
    def show(tag, r2):
        p2 = C.prepare(panel, r2, skip)
        e = C.run(p2)
        d = float(e.pnl.sum()) - B
        _, lo, hi = C.boot_delta(panel, e, base, n=3000, seed=55)
        st = [int(((e.stage == s) & e.fired).sum()) for s in (1, 2, 3)]
        dh1 = float(e.pnl[h1].sum() - base.pnl[h1].sum())
        dh2 = float(e.pnl[h2].sum() - base.pnl[h2].sum())
        print(f"  {tag:<34}{int(e.fired.sum()):>7}{int(e.fired.sum())-N:>+6}"
              f"{e.pnl.sum():>+10.2f}{d:>+9.2f}{lo:>+10.2f}{hi:>+10.2f}{dh1:>+9.2f}{dh2:>+9.2f}  {st}")

    for x in (0, 43, 60, 80, 100, 130):
        show(f"walk_min={x}（T150 闸）", C.Rules(walk_min=float(x)))
    for x in (0, 0.83, 0.90, 0.95, 0.97, 0.99):
        show(f"floor_min_price={x}", C.Rules(floor_min=float(x)))
    for x in (0.80, 0.85, 0.90, 0.95):
        show(f"price_min={x}", C.Rules(price_min=float(x)))
    for x in (63, 70, 75, 80, 90, 100, 120):
        show(f"dev_min={x}", C.Rules(dev_min=float(x)))
    for x in (0, 30, 40, 50, 60, 80):
        show(f"sd_min={x}（⑤ σ 腿）", C.Rules(sd_min=float(x)))
    show("σ 腿关闭（dev 单腿）", C.Rules(use_sigma_leg=False))
    show("全部放宽: dev63→63, walk0, floor0", C.Rules(walk_min=0, floor_min=0))
    show("dev75 + walk0 + floor0", C.Rules(dev_min=75, walk_min=0, floor_min=0))
    show("dev75 + floor0（留闸）", C.Rules(dev_min=75, floor_min=0))
    show("dev75 + walk0（留地板）", C.Rules(dev_min=75, walk_min=0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
