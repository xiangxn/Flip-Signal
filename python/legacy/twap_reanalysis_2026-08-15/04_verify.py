#!/usr/bin/env python3
"""
Part 4: 对 Part 3 幸存过滤器（flow_5s 强反向流）的深度验证。

验证项:
  1. 阈值稳健性: EV 随阈值的形态（是否存在过拟合尖峰）
  2. 按天稳定性（全样本、每事件一注口径）
  3. 聚类检查: 通过的穿越是否集中在少数事件/小时
  4. YES/NO 两侧是否同效
  5. 成交时点敏感性: fill0/fill1/fill2/fill3
  6. 二项检验: 过滤后翻转率 vs 基线
  7. 关键交叉: flow_5s × 触发价 / × 剩余时间 / × 小时
  8. bootstrap CI

用法:
    python 04_verify.py --data ../../data_0/lab_resolved
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
from scipy.stats import binomtest

from lib import (load_events, collect_crossings, stats, split_by_days,
                 ev_per_bet, day_of)

THR = 0.2569  # Part 3 在 train 上冻结的阈值（95 分位）


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="../../data_0/lab_resolved")
    args = parser.parse_args()

    events = load_events(args.data)
    xs = collect_crossings(events)
    base = stats(xs, "fill2")
    fl = [x for x in xs if x.flow_5s is not None and x.flow_5s >= THR]

    print("=" * 80)
    print("  Part 4: flow_5s 强反向流过滤器验证")
    print("=" * 80)
    print(f"  基线: 翻转率 {base['flip_rate'] * 100:.1f}%  fill {base['mean_fill']:.3f} "
          f"EV {base['ev']:+.4f}  (n={base['n']})")

    # ── 1. 阈值稳健性 ──
    print(f"\n  ── 1. 阈值稳健性（全样本, fill2）──")
    print(f"  {'阈值':>8s} {'n':>6s} {'翻转率':>8s} {'fill':>7s} {'EV/股':>8s}")
    for t in [0.0, 0.05, 0.1, 0.15, 0.2, THR, 0.3, 0.5, 1.0, 3.0, 10.0]:
        sub = [x for x in xs if x.flow_5s is not None and x.flow_5s >= t]
        a = stats(sub, "fill2")
        print(f"  {t:>8.3f} {a['n']:>6d} {a['flip_rate'] * 100:>7.1f}% "
              f"{a['mean_fill']:>7.3f} {a['ev']:>+8.4f}")

    # ── 2. 每事件一注 + 按天 ──
    by_event: dict[int, list] = {}
    for x in xs:
        by_event.setdefault(x.event_start, []).append(x)
    bets = []
    for et, es in sorted(by_event.items()):
        es.sort(key=lambda x: x.cross_idx)
        for x in es:
            if x.flow_5s is not None and x.flow_5s >= THR and x.fill2 is not None:
                bets.append(x)
                break

    print(f"\n  ── 2. 每事件一注 + 按天（thr={THR}）──")
    a = stats(bets, "fill2")
    days = split_by_days(bets)
    print(f"  n={a['n']}  翻转率 {a['flip_rate'] * 100:.1f}%  fill {a['mean_fill']:.3f} "
          f"EV {a['ev']:+.4f}/股  总P&L {sum(ev_per_bet(x, 'fill2') for x in bets):+.2f}")
    print(f"  {'日期':<8s} {'n':>5s} {'翻转率':>8s} {'fill':>7s} {'EV/股':>8s} {'P&L':>8s}")
    for d in sorted(days):
        ad = stats(days[d], "fill2")
        pd_ = sum(ev_per_bet(x, "fill2") for x in days[d])
        print(f"  {d:<8s} {ad['n']:>5d} {ad['flip_rate'] * 100:>7.1f}% "
              f"{ad['mean_fill']:>7.3f} {ad['ev']:>+8.4f} {pd_:>+8.2f}")

    # ── 3. 聚类检查 ──
    print(f"\n  ── 3. 聚类检查（全部通过穿越, 无每事件去重）──")
    ev_counts = {}
    for x in fl:
        ev_counts[x.event_start] = ev_counts.get(x.event_start, 0) + 1
    multi = {k: v for k, v in ev_counts.items() if v >= 2}
    print(f"  通过穿越 {len(fl)} 个, 分布在 {len(ev_counts)} 个事件")
    print(f"  单事件多个通过: {len(multi)} 个事件（最多 {max(ev_counts.values())} 个）")
    hours = {}
    for x in fl:
        hours[x.hour_utc] = hours.get(x.hour_utc, 0) + 1
    print(f"  小时分布: {dict(sorted(hours.items()))}")
    # 相邻事件的时间聚集（30 分钟内）
    ts = sorted(ev_counts.keys())
    close_pairs = sum(1 for i in range(1, len(ts)) if ts[i] - ts[i - 1] <= 1800)
    print(f"  相邻通过事件 ≤30min 的对数: {close_pairs}/{max(len(ts) - 1, 1)}")

    # ── 4. YES/NO 两侧 ──
    print(f"\n  ── 4. YES/NO 分侧（全部通过穿越）──")
    for side in ("yes", "no"):
        sub = [x for x in fl if x.side == side]
        a2 = stats(sub, "fill2")
        print(f"  {side.upper()}: n={a2['n']:>4d} 翻转率 {a2['flip_rate'] * 100:>5.1f}% "
              f"fill {a2['mean_fill']:.3f} EV {a2['ev']:+.4f}")

    # ── 5. 成交时点敏感性 ──
    print(f"\n  ── 5. 成交时点敏感性（全部通过穿越）──")
    for attr, label in [("fill0", "立即(0 tick)"), ("fill1", "+1 tick"),
                        ("fill2", "+2 ticks"), ("fill3", "+3 ticks")]:
        if attr == "fill1" or attr == "fill3":
            # 手动构造 fill1/fill3
            pass
        sub = fl if attr == "fill0" else None
    # fill0 直接可用; fill1/fill3 需重算
    ev0 = sum(ev_per_bet(x, "fill0") for x in fl)
    print(f"  {'立即 fill0':<14s} n={len(fl):>4d} EV {ev0 / len(fl):+.4f}/股")
    ev2 = sum(ev_per_bet(x, "fill2") for x in fl if x.fill2 is not None)
    n2 = sum(1 for x in fl if x.fill2 is not None)
    print(f"  {'+2t fill2':<14s} n={n2:>4d} EV {ev2 / n2:+.4f}/股")
    # fill1 = 1 - trigger_bid[i+1]
    ev1s = []
    for x in fl:
        e = next((e for e in events if e["start_time"] == x.event_start), None)
        if not e:
            continue
        snaps = e["snapshots"]
        j = x.cross_idx + 1
        if j < len(snaps) and snaps[j]["remaining_sec"] > 0:
            f = 1.0 - snaps[j][("yes_price" if x.side == "yes" else "no_price")]
            ev1s.append((1.0 - f) if x.flip else -f)
    if ev1s:
        print(f"  {'+1t fill1':<14s} n={len(ev1s):>4d} EV {sum(ev1s) / len(ev1s):+.4f}/股")

    # ── 6. 二项检验 ──
    print(f"\n  ── 6. 二项检验（翻转率 vs 基线）──")
    nf, kf = len(fl), sum(1 for x in fl if x.flip)
    p0 = base["flip_rate"]
    res = binomtest(kf, nf, p0, alternative="greater")
    print(f"  通过组: {kf}/{nf} = {kf / nf * 100:.1f}%  vs 基线 {p0 * 100:.1f}%")
    print(f"  binomial p(one-sided) = {res.pvalue:.4f}")

    # ── 7. bootstrap CI（EV, 每事件一注口径）──
    print(f"\n  ── 7. Bootstrap EV CI（每事件一注, fill2）──")
    rng = np.random.default_rng(42)
    evs = np.array([ev_per_bet(x, "fill2") for x in bets])
    means = [rng.choice(evs, size=len(evs), replace=True).mean() for _ in range(2000)]
    lo, hi = np.percentile(means, [2.5, 97.5])
    print(f"  EV = {evs.mean():+.4f}/股  95% CI [{lo:+.4f}, {hi:+.4f}]  n={len(evs)}")

    # ── 8. 交叉: flow_5s × 触发价 / × 剩余时间 / × 小时 ──
    print(f"\n  ── 8. 关键交叉（全部穿越, 通过组内再分）──")
    print(f"  {'交叉':<28s} {'n':>5s} {'翻转率':>8s} {'fill':>7s} {'EV/股':>8s}")
    for label, pred in [
        ("触发价 ≤0.72", lambda x: x.trigger_bid <= 0.72),
        ("触发价 0.72-0.78", lambda x: 0.72 < x.trigger_bid <= 0.78),
        ("触发价 >0.78", lambda x: x.trigger_bid > 0.78),
        ("剩余 ≤60s", lambda x: x.remaining_sec <= 60),
        ("剩余 >180s", lambda x: x.remaining_sec > 180),
        ("小时 12-18 UTC", lambda x: 12 <= x.hour_utc < 18),
        ("其他小时", lambda x: not (12 <= x.hour_utc < 18)),
        ("对侧未穿越过", lambda x: not x.other_crossed_before),
    ]:
        sub = [x for x in fl if pred(x)]
        if len(sub) < 8:
            continue
        a2 = stats(sub, "fill2")
        print(f"  {label:<28s} {a2['n']:>5d} {a2['flip_rate'] * 100:>7.1f}% "
              f"{a2['mean_fill']:>7.3f} {a2['ev']:>+8.4f}")

    # ── 9. 反事实: 同阈值下的不利方向流（对称性检查）──
    print(f"\n  ── 9. 对称性: 强同向流（flow_5s ≤ -{THR}）的跟随 EV ──")
    neg = [x for x in xs if x.flow_5s is not None and x.flow_5s <= -THR]
    a2 = stats(neg, "fill2")
    print(f"  强同向流组: n={a2['n']:>4d} 翻转率 {a2['flip_rate'] * 100:>5.1f}% "
          f"fill {a2['mean_fill']:.3f} EV {a2['ev']:+.4f}（若同样低 → 流方向单边有效）")
    mid = [x for x in xs if x.flow_5s is not None and -THR < x.flow_5s < THR]
    a3 = stats(mid, "fill2")
    print(f"  中性流组:   n={a3['n']:>4d} 翻转率 {a3['flip_rate'] * 100:>5.1f}% "
          f"fill {a3['mean_fill']:.3f} EV {a3['ev']:+.4f}")


if __name__ == "__main__":
    main()
