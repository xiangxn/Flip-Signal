#!/usr/bin/env python3
"""
Part 1: TWAP 真实结算下的翻转基率与 EV 结构（从零出发，无任何旧特征假设）。

回答三个问题:
  1. 多少事件出现过 UP/DOWN > 0.7？其中多少最终翻转（穿越侧输掉）？
  2. 穿越时刻立即买入对侧的基线 EV 是多少（EV = 翻转率 - fill）？
  3. EV 的机械结构: fill 由触发价决定（fill = 1 - 触发侧 bid），
     翻转率在哪类穿越中能覆盖 fill？

用法:
    python 01_baseline.py --data ../data_0/lab_resolved
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (load_events, collect_crossings, first_crossing_per_event,
                 stats, split_by_days, ev_per_bet, CONFIRM_TICKS)

from scipy.stats import chi2_contingency


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="../../data_0/lab_resolved")
    args = parser.parse_args()

    events = load_events(args.data)
    crossings = collect_crossings(events)
    firsts = first_crossing_per_event(crossings)

    print("=" * 72)
    print("  Part 1: 基率与 EV 结构（TWAP 真实结算 market_outcome）")
    print("=" * 72)

    # ── 1. 数据与穿越宇宙 ──
    n_ev = len(events)
    n_hist = sum(1 for e in events if e.get("hist_range") is not None)
    print(f"  事件总数: {n_ev}（全部有 market_outcome 标签）")
    print(f"  历史振幅可用: {n_hist}（前 {0} 个窗口预热）".format(0))
    print(f"  穿越观测总数: {len(crossings)}（两侧全部上升沿，有效窗口）")
    print(f"  有穿越事件: {len(firsts)}/{n_ev} "
          f"({len(firsts) / n_ev * 100:.1f}%)")

    # ── 2. 事件级基率 ──
    s = stats(firsts)
    print(f"\n  ── 事件级基率（每事件最早穿越）──")
    print(f"  最早穿越侧最终翻转: {s['flips']}/{s['n']} = {s['flip_rate'] * 100:.1f}%")
    print(f"  立即买入对侧: fill={s['mean_fill']:.3f}  EV/股={s['ev']:+.4f}")

    # ── 3. 观测级基率 ──
    print(f"\n  ── 观测级基率（全部穿越）──")
    s0 = stats(crossings)
    print(f"  翻转率: {s0['flip_rate'] * 100:.1f}%  fill={s0['mean_fill']:.3f}  "
          f"EV/股={s0['ev']:+.4f}  (n={s0['n']})")
    s2 = stats(crossings, "fill2")
    print(f"  确认 +{CONFIRM_TICKS} ticks 后成交: fill={s2['mean_fill']:.3f}  "
          f"EV/股={s2['ev']:+.4f}  (n={s2['n']})")

    # ── 4. YES/NO 不对称 ──
    print(f"\n  ── YES/NO 侧不对称 ──")
    for side in ("yes", "no"):
        xs = [x for x in crossings if x.side == side]
        f = [x for x in firsts if x.side == side]
        a = stats(xs)
        b = stats(f)
        print(f"  {side.upper()} 全部穿越: n={a['n']:>4d}  翻转率 {a['flip_rate'] * 100:>5.1f}%  "
              f"fill {a['mean_fill']:.3f}  EV {a['ev']:+.4f}")
        print(f"  {side.upper()} 最早穿越: n={b['n']:>4d}  翻转率 {b['flip_rate'] * 100:>5.1f}%  "
              f"fill {b['mean_fill']:.3f}  EV {b['ev']:+.4f}")

    # ── 5. 触发价分桶（机械 EV 结构）──
    print(f"\n  ── 触发价分桶（fill = 1 - 触发价，机械决定）──")
    buckets = [(0.70, 0.75), (0.75, 0.80), (0.80, 0.85), (0.85, 1.01)]
    rows = []
    for lo, hi in buckets:
        b = [x for x in crossings if lo <= x.trigger_bid < hi]
        rows.append((f"{lo:.2f}-{hi:.2f}", b))
    print(f"  {'触发价':<12s} {'n':>5s} {'翻转率':>8s} {'fill':>7s} {'EV/股':>8s}")
    for label, b in rows:
        if not b:
            continue
        a = stats(b)
        print(f"  {label:<12s} {a['n']:>5d} {a['flip_rate'] * 100:>7.1f}% "
              f"{a['mean_fill']:>7.3f} {a['ev']:>+8.4f}")
    table = [[sum(1 for x in b if x.flip), sum(1 for x in b if not x.flip)]
             for _, b in rows if b]
    if len(table) >= 2:
        chi2, p, _, _ = chi2_contingency(table, correction=False)
        print(f"  χ²={chi2:.1f}  p={p:.3f}（触发价 × 翻转独立性）")

    # ── 6. 穿越次数 / 振荡 ──
    print(f"\n  ── 穿越次数与振荡 ──")
    for label, pred in [
        ("第 1 次穿越（该侧）", lambda x: x.cross_count_same == 1),
        ("第 2+ 次穿越（该侧）", lambda x: x.cross_count_same >= 2),
        ("对侧未穿越过（首侧主导）", lambda x: not x.other_crossed_before),
        ("对侧已穿越过（振荡）", lambda x: x.other_crossed_before),
    ]:
        b = [x for x in crossings if pred(x)]
        a = stats(b)
        print(f"  {label:<22s} n={a['n']:>4d}  翻转率 {a['flip_rate'] * 100:>5.1f}%  "
              f"EV {a['ev']:+.4f}")

    # ── 7. 剩余时间分桶 ──
    print(f"\n  ── 穿越时剩余时间分桶 ──")
    tb = [(16, 60), (60, 120), (120, 180), (180, 260)]
    rows = []
    for lo, hi in tb:
        b = [x for x in crossings if lo < x.remaining_sec <= hi]
        rows.append((f"{lo}-{hi}s", b))
    print(f"  {'剩余时间':<12s} {'n':>5s} {'翻转率':>8s} {'fill':>7s} {'EV/股':>8s}")
    for label, b in rows:
        a = stats(b)
        print(f"  {label:<12s} {a['n']:>5d} {a['flip_rate'] * 100:>7.1f}% "
              f"{a['mean_fill']:>7.3f} {a['ev']:>+8.4f}")
    table = [[sum(1 for x in b if x.flip), sum(1 for x in b if not x.flip)]
             for _, b in rows if b]
    if len(table) >= 2:
        chi2, p, _, _ = chi2_contingency(table, correction=False)
        print(f"  χ²={chi2:.1f}  p={p:.3f}")

    # ── 8. 按天稳定性（事件级最早穿越）──
    print(f"\n  ── 按天稳定性（每事件最早穿越，立即成交）──")
    days = split_by_days(firsts)
    print(f"  {'日期':<8s} {'n':>5s} {'翻转率':>8s} {'fill':>7s} {'EV/股':>8s}")
    for d in sorted(days):
        a = stats(days[d])
        print(f"  {d:<8s} {a['n']:>5d} {a['flip_rate'] * 100:>7.1f}% "
              f"{a['mean_fill']:>7.3f} {a['ev']:>+8.4f}")

    # ── 9. 翻转事件中发生了什么: 触发价分布与剩余时间分布 ──
    print(f"\n  ── 翻转 vs 未翻转 的特征对照（t 检验方向提示）──")
    import numpy as np
    for key, name in [("trigger_bid", "触发价"), ("remaining_sec", "剩余时间"),
                      ("spot_pos", "现货位置"), ("range_exp", "振幅扩张"),
                      ("gap60", "spot-60s均线缺口"), ("ret_60s", "60s动量"),
                      ("flow_cum60", "60s累计流"), ("depth_imb", "盘口失衡"),
                      ("pm_vel", "PM升速"), ("spread", "隐含价差")]:
        fl = [getattr(x, key) for x in crossings if x.flip and getattr(x, key) is not None]
        nf = [getattr(x, key) for x in crossings if not x.flip and getattr(x, key) is not None]
        if len(fl) >= 10 and len(nf) >= 10:
            mf_, mn = np.mean(fl), np.mean(nf)
            diff = mf_ - mn
            print(f"  {name:<14s} 翻转组均值 {mf_:>+8.4f}  未翻转组 {mn:>+8.4f}  "
                  f"差 {diff:>+8.4f}")

    print()
    print("  注: 以上全部为样本内描述性统计，任何正 EV 桶都需要 Part 2/3")
    print("      的分桶显著性 + 按天样本外验证后才能成为候选。")


if __name__ == "__main__":
    main()
