#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浅洞带口径核检（2026-09-03，临时分析）——回答「现口径 (−0.5,0) 方向性是否=策略本意」：
现口径浅洞带 = 现货在狗败侧 ≤0.5σ（dist_s∈(−0.5,0)）；用户口述 = 「BTC 距锚有多近」
（未指定方向）。本脚本在急跌+时间两腿已过（只剩浅洞腿定生死）的触发总体里，
按 dist_s 区间切分布 + 分桶胜率/收益，看方向性是否影响结果。

用法: python 03_shallow_band_check.py [--data <事件目录>] [--stake 2]
"""
import argparse
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent

# 01_backtest_r1.py 是权威提取/口径源（模块级只有定义，main 有守卫）
_spec = importlib.util.spec_from_file_location("r1", BASE / "01_backtest_r1.py")
r1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(r1)

STAKE = r1.STAKE
CRASH_MIN = r1.CRASH_MIN
REM_MIN = r1.REM_MIN


def pl_of(s):
    """2U/注收益数组（win → shares−2; lose → −2）。"""
    return np.where(s["settle_won"] == 1, STAKE / s["fill"] - STAKE, -STAKE)


def bucket_report(name, s, total_days):
    if len(s) == 0:
        print(f"{name}: n=0")
        return
    k = int(s.settle_won.sum())
    pl = pl_of(s)
    lo, hi = r1.wilson(k, len(s))
    print(f"{name}: n={len(s):3d}  side={s.side.value_counts().to_dict()}  "
          f"WR {k/len(s)*100:5.1f}% [{lo*100:4.1f},{hi*100:4.1f}]  fill {s.fill.mean():.3f}  "
          f"EV {pl.mean():+.3f}U/注  P&L {pl.sum():+6.0f}U  WR−fill {k/len(s)-s.fill.mean():+.4f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(BASE.parent.parent / "data" / "btc"))
    args = ap.parse_args()

    df = r1.extract(args.data)
    ndays = df["date"].nunique()
    print(f"数据 {args.data}: 事件 → 0.2 首触观测 {len(df)}（{df['date'].nunique()} 天）\n")

    # 触发总体中「只剩浅洞腿定生死」的子集: 急跌过 + 时间过（浅洞不入条件）
    pop = df[(df["m_45"] >= CRASH_MIN) & (df["rem"] > REM_MIN)].copy()
    has_d = pop["dist_s"].notna()
    print(f"急跌×时间已过、只剩浅洞腿的触发: {len(pop)}  "
          f"（其中 dist_s 缺失[no_hist/missing_spot 类] {int((~has_d).sum())}）\n")

    # 1) dist_s 分布
    d = pop.loc[has_d, "dist_s"]
    print("dist_s 分布（σ 单位; 负 = 现货在狗败侧, 正 = 已在狗赢侧）:")
    edges = np.arange(-1.5, 1.501, 0.1)
    hist, _ = np.histogram(d, bins=edges)
    for lo, hi, c in zip(edges[:-1], edges[1:], hist):
        bar = "#" * int(c / max(1, hist.max()) * 40)
        print(f"  [{lo:+4.1f},{hi:+4.1f})  {c:4d}  {bar}")
    print(f"  p5/p25/p50/p75/p95 = {np.percentile(d,[5,25,50,75,95]).round(2).tolist()}")
    print(f"  dist_s>0（狗赢侧, 现口径必拒）占比: {int((d>0).sum())} / {len(d)} = {(d>0).mean()*100:.1f}%\n")

    # 2) 分桶: 只看方向与深度
    for lo, hi, tag in [(-np.inf, -1.0, "≤−1σ（狗败侧深）"),
                        (-1.0, -0.5, "(−1,−0.5)σ（狗败侧中）"),
                        (-0.5, 0.0, "(−0.5,0)σ（狗败侧浅=现口径）"),
                        (0.0, 0.5, "(0,0.5]σ（狗赢侧浅）"),
                        (0.5, 1.0, "(0.5,1]σ（狗赢侧中）"),
                        (1.0, np.inf, ">1σ（狗赢侧深）")]:
        m = (d > lo) & (d <= hi)
        bucket_report(tag, pop.loc[has_d].loc[m], ndays)
    print()
    bucket_report("dist_s 缺失（no_hist/missing_spot）", pop.loc[~has_d], ndays)

    # 3) 候选带定义对比（只比较有 dist_s 的）
    print("--- 候选浅洞带（在急跌×时间已过的触发上） ---")
    hd = pop.loc[has_d]
    for name, m in [
        ("A. 现口径: −0.5 < dist < 0（方向性: 须在狗败侧）",
         (hd["dist_s"] > -0.5) & (hd["dist_s"] < 0)),
        ("B. 对称浅: |dist| < 0.5（无方向, 你的口述最贴近版本）",
         hd["dist_s"].abs() < 0.5),
        ("C. 狗败侧任意深: dist < 0",
         hd["dist_s"] < 0),
        ("D. 狗败侧 ≤1σ: −1 < dist < 0",
         (hd["dist_s"] > -1.0) & (hd["dist_s"] < 0)),
        ("E. 对称 ≤1σ: |dist| < 1",
         hd["dist_s"].abs() < 1.0),
        ("F. 仅狗赢侧浅（现口径补集）: 0 ≤ dist < 0.5",
         (hd["dist_s"] >= 0) & (hd["dist_s"] < 0.5)),
        ("G. 不设浅洞腿（对照）: 全部",
         np.ones(len(hd), bool)),
    ]:
        bucket_report(name, hd[m], ndays)

    # B\A = B 多出的部分（=F 类）已在上; A\B 不存在（A⊂B）。输出 R1 口径总 P&L 对照
    print()
    r1pure = pop[has_d][(pop[has_d]["dist_s"] > -0.5) & (pop[has_d]["dist_s"] < 0)]
    k = int(r1pure.settle_won.sum())
    print(f"R1 现口径完整回测参考: n={len(r1pure)} WR {k/len(r1pure)*100:.1f}% "
          f"P&L {pl_of(r1pure).sum():+.0f}U（脚本 01 摘要口径一致应 n=245）")


if __name__ == "__main__":
    main()
