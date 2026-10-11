#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘 · 全新数据面 第 4 步：条件扫描（1D 阈值 + 2D 交叉），全部走链式反事实

口径：基线 = 现行链（n=1959）。给每个候选条件做成「三段共用的额外掩码」，
链照常逐段前进（条件不满足 = 该 tick 不算数，链在**同段**继续往后找 / 换到后段），
因此每个 Δ 都是真正的反事实（含改道），不是「砍掉某几笔」的算术。

统计：日级配对 bootstrap（Δ 的 95% 区间），分半（按中点日切），
      并对最优格做「分层行置换」安慰剂（见 55 号脚本）。
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


def feature_matrices(panel: C.Panel) -> dict[str, np.ndarray]:
    sd = panel.sd[:, None]
    f = {
        "p": panel.p,
        "dev": panel.dev,
        "walk": panel.walk,
        "basis": panel.basis,
        "sd": np.repeat(sd, panel.p.shape[1], axis=1),
        "rem": panel.rem.astype(float),
        "walk/sd": panel.walk / np.maximum(sd, EPS),
        "dev/sd": panel.dev / np.maximum(sd, EPS),
        "basis/sd": panel.basis / np.maximum(sd, EPS),
        "basis/walk": panel.basis / np.where(panel.walk > 0, panel.walk, np.nan),
        "raw_move": panel.spot - panel.anchor[:, None],     # 现货离锚（不按侧别定号）
        "raw_tw": panel.twap - panel.anchor[:, None],       # 结算线离锚（不按侧别定号）
        "raw_basis": panel.spot - panel.twap,               # 现货−结算线（不按侧别定号）
    }
    for k, v in f.items():
        if k in ("walk/sd", "dev/sd", "basis/sd", "basis/walk"):
            f[k] = np.nan_to_num(v, nan=-1e18, posinf=1e18, neginf=-1e18)
    f["rem"] = np.where(panel.rem > -900, f["rem"], -1e18)
    return f


def ge(m, x):
    return m >= x


def le(m, x):
    return m <= x


DIRS = {"ge": ge, "le": le}


def main() -> int:
    ap = __import__("argparse").ArgumentParser()
    ap.add_argument("--mode", default="1d", choices=["1d", "2d", "both"])
    ap.add_argument("--topk", type=int, default=25)
    args = ap.parse_args()

    ws = C.load()
    panel, skip = C.build_panel(ws)
    rules = C.Rules()
    prep = C.prepare(panel, rules, skip)
    base = C.run(prep)
    B = float(base.pnl.sum()); N = int(base.fired.sum())
    print(f"基线 n={N} 毛 {B:+.3f}U（净 {base.pnl_net.sum():+.3f}U）")
    F = feature_matrices(panel)

    # 用**成交行**的分位数当阈值锚（保证每格都有样本）
    t = C.entry_table(panel, base)

    def entry_series(name: str) -> np.ndarray:
        if name in t:
            return np.asarray(t[name], dtype=float)
        if "/" in name:
            num, den = name.split("/")
            return t[num] / np.maximum(t[den], EPS)
        if name == "raw_move":
            return t["spot"] - t["anchor"]
        if name == "raw_tw":
            return t["twap"] - t["anchor"]
        if name == "raw_basis":
            return t["spot"] - t["twap"]
        raise KeyError(name)

    def deciles(name: str, qs=(10, 20, 30, 40, 50, 60, 70, 80, 90)):
        v = entry_series(name)
        return [float(np.percentile(v, x)) for x in qs]

    results = []

    if args.mode in ("1d", "both"):
        print("\n" + "=" * 108)
        print("【1D 扫描】 feats ≥/≤ 阈值，三段共用；Δ = 毛P&L(新) − 毛P&L(基线) @2U")
        print(f"  {'特征':<10}{'方向':<5}{'阈值':>10}{'n':>7}{'Δn':>6}{'ΔP&L':>9}"
              f"{'95%下界':>10}{'95%上界':>10}{'h1':>9}{'h2':>9}   段分布(新)")
        days = sorted(set(panel.day))
        mid = days[len(days) // 2]
        h1 = np.array([d < mid for d in panel.day]); h2 = ~h1
        for name, M in F.items():
            cuts = sorted(set(deciles(name)))
            for dname, dfn in DIRS.items():
                for X in cuts:
                    ex = dfn(M, X)
                    e = C.run(prep, extra150=ex, extra60=ex, extralisten=ex)
                    d = float(e.pnl.sum()) - B
                    n = int(e.fired.sum())
                    if abs(d) < 1e-9 and n == N:
                        continue
                    dlt, lo, hi = C.boot_delta(panel, e, base, n=2000, seed=54)
                    dh1 = float(e.pnl[h1].sum() - base.pnl[h1].sum())
                    dh2 = float(e.pnl[h2].sum() - base.pnl[h2].sum())
                    st = [int(((e.stage == s) & e.fired).sum()) for s in (1, 2, 3)]
                    results.append((name, dname, X, n, n - N, d, lo, hi, dh1, dh2, tuple(st)))
                    print(f"  {name:<10}{dname:<5}{X:>10.2f}{n:>7}{n-N:>6}{d:>+9.2f}"
                          f"{lo:>+10.2f}{hi:>+10.2f}{dh1:>+9.2f}{dh2:>+9.2f}   {st}")

    if args.mode in ("2d", "both"):
        print("\n" + "=" * 108)
        print("【2D 交叉扫描】 两个特征同时 ≥ 分位（三段共用），只报 ΔP&L 前若干格")
        names = ["p", "dev", "walk", "basis", "sd", "rem", "walk/sd", "dev/sd", "basis/walk"]
        grids = {n_: deciles(n_, (25, 50, 75)) for n_ in names}
        cells = []
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                for xa in grids[a]:
                    for xb in grids[b]:
                        ex = (F[a] >= xa) & (F[b] >= xb)
                        e = C.run(prep, extra150=ex, extra60=ex, extralisten=ex)
                        d = float(e.pnl.sum()) - B
                        n = int(e.fired.sum())
                        cells.append((a, b, xa, xb, n, n - N, d))
        cells.sort(key=lambda c: -c[6])
        print(f"  共 {len(cells)} 格；Δ>0 的 {sum(1 for c in cells if c[6] > 0)} 格，"
              f"Δ<0 的 {sum(1 for c in cells if c[6] < 0)} 格")
        print(f"  {'特征A':<10}{'特征B':<10}{'阈A':>9}{'阈B':>9}{'n':>7}{'Δn':>6}{'ΔP&L':>9}")
        for c in cells[:args.topk]:
            print(f"  {c[0]:<10}{c[1]:<10}{c[2]:>9.2f}{c[3]:>9.2f}{c[4]:>7}{c[5]:>+6}{c[6]:>+9.2f}")
        print("  ... 最差 5 格:")
        for c in cells[-5:]:
            print(f"  {c[0]:<10}{c[1]:<10}{c[2]:>9.2f}{c[3]:>9.2f}{c[4]:>7}{c[5]:>+6}{c[6]:>+9.2f}")
        with open("data/fresh/scan2d.txt", "w") as fh:
            for c in cells:
                fh.write(f"{c[0]}\t{c[1]}\t{c[2]:.4f}\t{c[3]:.4f}\t{c[4]}\t{c[5]}\t{c[6]:.4f}\n")
        print("  ✔ 全表 → data/fresh/scan2d.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
