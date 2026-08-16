#!/usr/bin/env python3
"""
Part 2: 单特征筛选（TWAP 真实结算，从零挖掘）。

对每个因果特征: 四分位分桶 → 翻转率 / fill / EV + χ² 独立性检验；
再加按天符号一致性检查（桶 EV 与全样本 EV 同号的天数占比）。
方向性特征另给"符号切分"（正/负桶）。

铁律: 特征只用 ≤ 穿越 tick 数据；结算只用 market_outcome。

用法:
    python 02_feature_screen.py --data ../../data_0/lab_resolved
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
from scipy.stats import chi2_contingency, spearmanr

from lib import (load_events, collect_crossings, stats, split_by_days,
                 ev_per_bet, day_of)


def ci(rate: float, n: int) -> str:
    if n < 5:
        return "—"
    se = (rate * (1 - rate) / n) ** 0.5
    return f"±{1.96 * se * 100:.1f}pp"


def bucket_table(title: str, xs, key: str, edges: list[float],
                 labels: list[str], baseline: float) -> list:
    """通用分桶报告: 翻转率/95%CI/fill/EV + χ² + 按天符号一致性。"""
    print(f"\n  【{title}】 基线翻转率 {baseline * 100:.1f}%  (n={len(xs)})")
    print(f"  {'分桶':<20s} {'n':>5s} {'翻转率':>8s} {'95%CI':>12s} "
          f"{'fill':>7s} {'EV/股':>8s} {'天同号':>6s}")
    rows = []
    for i, label in enumerate(labels):
        lo = edges[i]
        hi = edges[i + 1] if i + 1 < len(edges) else None
        b = [x for x in xs
             if (v := getattr(x, key)) is not None and lo <= v and (hi is None or v < hi)]
        rows.append(b)
        if not b:
            print(f"  {label:<20s} {0:>5d}        —")
            continue
        a = stats(b, "fill2")
        days = split_by_days(b)
        same = sum(1 for d, dbs in days.items() if len(dbs) >= 5
                   and (stats(dbs, "fill2")["ev"] or 0) * a["ev"] >= 0)
        tot_days = sum(1 for d, dbs in days.items() if len(dbs) >= 5)
        print(f"  {label:<20s} {a['n']:>5d} {a['flip_rate'] * 100:>7.1f}% "
              f"{ci(a['flip_rate'], a['n']):>12s} {a['mean_fill']:>7.3f} "
              f"{a['ev']:>+8.4f} {same}/{tot_days:<4d}")

    table = [[sum(1 for x in b if x.flip), sum(1 for x in b if not x.flip)]
             for b in rows if b]
    if len(table) >= 2:
        chi2, p, _, _ = chi2_contingency(table, correction=False)
        mark = "★" if p < 0.05 else ("△" if p < 0.1 else "·")
        print(f"  χ²={chi2:.1f}  p={p:.3f} {mark}")
    return rows


def quantile_edges(xs, key, n_bins=4):
    vals = sorted(getattr(x, key) for x in xs if getattr(x, key) is not None)
    if len(vals) < 50:
        return None
    edges = [float(vals[len(vals) * k // n_bins]) for k in range(n_bins)]
    labels = [f"Q{k + 1} [{edges[k]:.3g},{edges[k + 1] if k + 1 < n_bins else '∞'})"
              for k in range(n_bins)]
    return edges + [None], labels


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="../../data_0/lab_resolved")
    args = parser.parse_args()

    events = load_events(args.data)
    crossings = collect_crossings(events)
    base = stats(crossings, "fill2")["flip_rate"]

    print("=" * 76)
    print("  Part 2: 单特征筛选（四分位 + 方向符号分桶，fill2 成交口径）")
    print("=" * 76)
    print(f"  观测数: {len(crossings)}  基线翻转率 {base * 100:.1f}%  "
          f"基线 EV {stats(crossings, 'fill2')['ev']:+.4f}/股")

    # ── 非方向特征: 四分位 ──
    for key, title in [
        ("trigger_bid", "触发价（fill 的机械决定项）"),
        ("remaining_sec", "穿越时剩余时间"),
        ("spread", "隐含价差"),
        ("pm_vel", "PM 升速（3tick）"),
        ("range_exp", "现货振幅扩张 |spot-open|/hist"),
        ("vol_10s", "波动率 vol_10s"),
        ("vol_30s", "波动率 vol_30s"),
    ]:
        edges, labels = quantile_edges(crossings, key)
        if edges:
            bucket_table(title, crossings, key, edges, labels, base)

    # ── 方向特征: 符号分桶（正=对翻转有利）+ 四分位 ──
    for key, title in [
        ("spot_pos", "现货位置 spot_pos（正=反向）"),
        ("twap60_pos", "60s均值位置 twap60_pos（正=已实现的 TWAP 偏置反向）"),
        ("gap60", "spot-60s缺口 gap60"),
        ("ret_60s", "60s动量 ret_60s"),
        ("ret_30s", "30s动量 ret_30s"),
        ("ret_10s", "10s动量 ret_10s"),
        ("flow_5s", "5s主动流 flow_5s"),
        ("flow_cum30", "30s累计流 flow_cum30"),
        ("flow_cum60", "60s累计流 flow_cum60"),
        ("depth_imb", "盘口失衡 depth_imb"),
    ]:
        edges = [-1e9, 0.0, 1e9]
        bucket_table(f"{title}（符号）", crossings, key, edges,
                     ["负（不利）", "正（有利）"], base)
        edges, labels = quantile_edges(crossings, key)
        if edges:
            bucket_table(f"{title}（四分位）", crossings, key, edges, labels, base)

    # ── 穿越次数 / 振荡 / 延迟 ──
    print(f"\n  ── 离散特征 ──")
    for key, title, buckets in [
        ("cross_count_same", "该侧第几次穿越", [(1, 2, "第1次"), (2, 3, "第2次"), (3, 1e9, "≥3次")]),
        ("total_crosses_before", "窗口内总穿越次数", [(1, 2, "第1次"), (2, 3, "第2次"), (3, 5, "3-4次"), (5, 1e9, "≥5次")]),
        ("hour_utc", "UTC 小时", [(0, 6, "0-6"), (6, 12, "6-12"), (12, 18, "12-18"), (18, 24, "18-24")]),
    ]:
        rows = []
        print(f"\n  【{title}】")
        for lo, hi, label in buckets:
            b = [x for x in crossings if lo <= getattr(x, key) < hi]
            rows.append(b)
            if not b:
                continue
            a = stats(b, "fill2")
            print(f"  {label:<10s} n={a['n']:>5d}  翻转率 {a['flip_rate'] * 100:>5.1f}% "
                  f"{ci(a['flip_rate'], a['n']):>12s} fill {a['mean_fill']:.3f} "
                  f"EV {a['ev']:+.4f}")
        table = [[sum(1 for x in b if x.flip), sum(1 for x in b if not x.flip)]
                 for b in rows if b]
        if len(table) >= 2:
            chi2, p, _, _ = chi2_contingency(table, correction=False)
            print(f"  χ²={chi2:.1f}  p={p:.3f}")

    # ── 机械结构专项: 已实现 TWAP 偏置 × 剩余时间 ──
    print(f"\n  ── 专项: twap60_pos × 剩余时间（机械拖拽窗口）──")
    print(f"  TWAP-60 结算 = avg(spot[T-60,T])。剩余 r<60s 时，(60-r)/60 的")
    print(f"  结算样本已实现（≈spot 60s 均值）。已实现部分若已偏向翻转侧，")
    print(f"  未来现货必须反向走出 r 秒才能救回 —— 机械上翻转率应升高。")
    print(f"  {'组合':<30s} {'n':>5s} {'翻转率':>8s} {'95%CI':>12s} "
          f"{'fill':>7s} {'EV/股':>8s}")
    for p_lo, p_hi, pl in [(-1e9, 0.0, "twap60 偏穿越侧"), (0.0, 1e9, "twap60 偏翻转侧")]:
        for r_lo, r_hi, rl in [(16, 60, "r<60s"), (60, 120, "60-120s"),
                               (120, 180, "120-180s"), (180, 260, ">180s")]:
            b = [x for x in crossings
                 if x.twap60_pos is not None and p_lo <= x.twap60_pos < p_hi
                 and r_lo < x.remaining_sec <= r_hi]
            if len(b) < 10:
                continue
            a = stats(b, "fill2")
            print(f"  {pl:<16s} × {rl:<10s} {a['n']:>5d} "
                  f"{a['flip_rate'] * 100:>7.1f}% {ci(a['flip_rate'], a['n']):>12s} "
                  f"{a['mean_fill']:>7.3f} {a['ev']:>+8.4f}")

    # ── 专项: spot_pos × 剩余时间 ──
    print(f"\n  ── 专项: spot_pos × 剩余时间 ──")
    print(f"  {'组合':<30s} {'n':>5s} {'翻转率':>8s} {'95%CI':>12s} "
          f"{'fill':>7s} {'EV/股':>8s}")
    for p_lo, p_hi, pl in [(-1e9, 0.0, "spot 同向"), (0.0, 0.5, "spot 反向<0.5hist"),
                           (0.5, 1e9, "spot 反向≥0.5hist")]:
        for r_lo, r_hi, rl in [(16, 60, "r<60s"), (60, 120, "60-120s"),
                               (120, 180, "120-180s"), (180, 260, ">180s")]:
            b = [x for x in crossings
                 if x.spot_pos is not None and p_lo <= x.spot_pos < p_hi
                 and r_lo < x.remaining_sec <= r_hi]
            if len(b) < 10:
                continue
            a = stats(b, "fill2")
            print(f"  {pl:<16s} × {rl:<10s} {a['n']:>5d} "
                  f"{a['flip_rate'] * 100:>7.1f}% {ci(a['flip_rate'], a['n']):>12s} "
                  f"{a['mean_fill']:>7.3f} {a['ev']:>+8.4f}")

    print()
    print("  注: 全部为样本内描述。进入 Part 3 的组合必须满足:")
    print("    (a) χ² p<0.05  (b) 桶 EV 符号跨天稳定  (c) 冻结阈值的样本外 EV>0")


if __name__ == "__main__":
    main()
