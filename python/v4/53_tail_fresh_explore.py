#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘 · 全新数据面 第 3 步：基线的描述性结构（成交行特征 / 价格校准 / 输单落点）

只读 50 号缓存 + 51 号链，不碰仓库任何既有分析代码。
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

FEATS = ["p", "dev", "basis", "walk", "sd", "rem"]


def q(a, p):
    return float(np.percentile(a, p))


def main() -> int:
    ws = C.load()
    panel, skip = C.build_panel(ws)
    rules = C.Rules()
    e = C.simulate(panel, rules, skip)
    t = C.entry_table(panel, e)
    ex = C.excess(t)

    print("=" * 92)
    print("【0】基线（现行链）")
    print(f"  窗 {panel.W}（跳过 {int(skip.sum())}）  成交 n={len(t['p'])}  "
          f"WR={100*t['won'].mean():.3f}%  毛 {t['pnl'].sum():+.2f}U  净 {t['pnl_net'].sum():+.2f}U")
    for s, name in ((1, "t150"), (2, "t60"), (3, "listen")):
        m = t["stage"] == s
        print(f"  {name:6s} n={int(m.sum()):4d} WR={100*t['won'][m].mean():6.3f}% "
              f"毛{t['pnl'][m].sum():+7.2f}U 输{int((~t['won'][m]).sum()):3d} "
              f"| 中位 p={np.median(t['p'][m]):.3f} dev={np.median(t['dev'][m]):7.1f} "
              f"basis={np.median(t['basis'][m]):7.1f} walk={np.median(t['walk'][m]):7.1f} "
              f"sd={np.median(t['sd'][m]):6.1f} rem={np.median(t['rem'][m]):5.0f}")

    print()
    print("=" * 92)
    print("【1】价格校准：市场价 p vs 实际胜率（超额 = (WR − 均p)/均p，即每笔收益率）")
    print(f"  {'价带':<14}{'n':>6}{'均p':>8}{'实际WR':>10}{'超额/笔':>10}{'毛P&L':>10}{'段':>22}")
    bands = [(0.80, 0.85), (0.85, 0.90), (0.90, 0.95), (0.95, 0.99), (0.99, 1.01)]
    for lo, hi in bands:
        for stage, sname in ((0, "全部"), (1, "t150"), (2, "t60"), (3, "listen")):
            m = (t["p"] >= lo) & (t["p"] < hi)
            if stage:
                m &= t["stage"] == stage
            if m.sum() == 0:
                continue
            pm = t["p"][m].mean()
            wr = t["won"][m].mean()
            print(f"  [{lo:.2f},{hi:.2f}){'':<3}{int(m.sum()):>6}{pm:>8.3f}{100*wr:>9.2f}%"
                  f"{100*(wr-pm)/pm:>9.2f}%{t['pnl'][m].sum():>+10.2f}{sname:>9}")

    print()
    print("=" * 92)
    print("【2】特征分档（全部成交，五档分位）—— n / 实际WR / 均p / 超额每笔 / 毛P&L")
    for f in FEATS:
        v = t[f]
        qs = [20, 40, 60, 80]
        cuts = [float(np.percentile(v, x)) for x in qs]
        edges = [-1e18] + cuts + [1e18]
        print(f"  ── {f}  (中位 {np.median(v):.2f})")
        for i in range(5):
            m = (v >= edges[i]) & (v < edges[i + 1])
            if m.sum() == 0:
                continue
            pm = t["p"][m].mean(); wr = t["won"][m].mean()
            print(f"     [{edges[i] if i else float('-inf'):>9.2f},{edges[i+1] if i<4 else float('inf'):>9.2f})"
                  f" n={int(m.sum()):4d} WR={100*wr:6.2f}% p̄={pm:.3f} "
                  f"超额={100*(wr-pm)/pm:+6.2f}% P&L={t['pnl'][m].sum():+7.2f}U "
                  f"输{int((~t['won'][m]).sum()):3d}")

    print()
    print("=" * 92)
    print("【3】64 个输单的落点")
    L = ~t["won"]
    print(f"  总量 {int(L.sum())}：", {k: int((L & (t['stage'] == s)).sum()) for k, s in
                                     (("t150", 1), ("t60", 2), ("listen", 3))})
    print(f"    侧别: yes {int((L & t['hot_yes']).sum())} / no {int((L & ~t['hot_yes']).sum())}"
          f"   （全体 yes {int(t['hot_yes'].sum())} / no {int((~t['hot_yes']).sum())}）")
    print(f"    价带: " + "  ".join(
        f"[{lo:.2f},{hi:.2f})={int((L & (t['p'] >= lo) & (t['p'] < hi)).sum())}"
        for lo, hi in bands))
    print(f"    walk<43: {int((L & (t['walk'] < 43)).sum())}   walk<0: {int((L & (t['walk'] < 0)).sum())}"
          f"   basis<0: {int((L & (t['basis'] < 0)).sum())}   dev<63: {int((L & (t['dev'] < 63)).sum())}")
    print(f"    sd<30: {int((L & (t['sd'] < 30)).sum())}  sd<40: {int((L & (t['sd'] < 40)).sum())}")
    print(f"    行含: " + " ".join(
        f"{d}:{int((L & (t['day'] == d)).sum())}" for d in sorted(set(t["day"]))))

    print()
    print("=" * 92)
    print("【4】特征相关性（成交行）与派生量")
    X = np.column_stack([t[f] for f in FEATS])
    names = FEATS
    print("       " + "".join(f"{n:>9}" for n in names))
    for i, n in enumerate(names):
        row = "".join(f"{np.corrcoef(X[:, i], X[:, j])[0,1]:>9.3f}" for j in range(len(names)))
        print(f"  {n:>5}{row}")
    print(f"  walk/sd 中位 {np.median(t['walk']/t['sd']):.2f}   "
          f"basis/walk 中位 {np.median(t['basis']/np.maximum(t['walk'],1e-9)):.2f}   "
          f"dev/sd 中位 {np.median(t['dev']/t['sd']):.2f}")

    print()
    print("=" * 92)
    print("【5】未成交的窗里有多少「本来是候选」的")
    notfired = ~e.fired & ~skip
    V = panel.valid & ~skip[:, None]
    price_ok = V & (panel.p >= 0.80)
    has_cand = price_ok.any(axis=1) & notfired
    print(f"  未成交 {int(notfired.sum())} 窗，其中出现过 p≥0.80 的有效 tick 的 {int(has_cand.sum())} 窗")
    i150 = C._first(V & (panel.rem <= 150))
    print(f"  连 rem≤150 的有效 tick 都没有的窗: {int((i150 < 0).sum() - skip.sum())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
