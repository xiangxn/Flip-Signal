#!/usr/bin/env python3
"""
Part 5: 幸存过滤器（强反向流）的精细化组合。

Part 4 发现:
  * flow_5s ≥ 0.05~0.5 是稳健平台（EV 单调升至 +0.09，非尖峰）
  * 通过组内交叉: 剩余 >180s（+0.150）、触发价 ≤0.78（+0.09）、
    小时 12-18 UTC（-0.05, 反常）、对侧未穿越过（+0.099）

本脚本:
  1. 以 flow_5s ≥ 0.1（平台下限, 更多样本）为基础, 逐层叠加精化条件
  2. 每层组合输出 8 天逐日 EV 表（要求 ≥6/8 天正 EV 才视为稳健）
  3. 小时效应的细粒度检查（是否真实, 还是小样本假象）
  4. 最终策略规格 + 每事件一注模拟 + fill0/fill2 对比

用法:
    python 05_refine.py --data ../../data_0/lab_resolved
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (load_events, collect_crossings, stats, split_by_days,
                 ev_per_bet, day_of)

FLOW_THR = 0.1  # 平台下限阈值（Part 4: 0.05~0.5 全部 EV>0.07）


def one_bet_per_event(xs, pred):
    by_event: dict[int, list] = {}
    for x in xs:
        by_event.setdefault(x.event_start, []).append(x)
    bets = []
    for et in sorted(by_event):
        for x in sorted(by_event[et], key=lambda x: x.cross_idx):
            if pred(x) and x.fill2 is not None:
                bets.append(x)
                break
    return bets


def day_table(title, bets):
    a = stats(bets, "fill2")
    days = split_by_days(bets)
    print(f"\n  【{title}】 n={a['n']}  翻转率 {a['flip_rate'] * 100:.1f}%  "
          f"fill {a['mean_fill']:.3f}  EV {a['ev']:+.4f}/股  "
          f"总P&L {sum(ev_per_bet(x, 'fill2') for x in bets):+.2f}")
    print(f"  {'日期':<8s} {'n':>5s} {'翻转率':>8s} {'fill':>7s} {'EV/股':>8s} {'P&L':>8s}")
    pos_days = 0
    for d in sorted(days):
        ad = stats(days[d], "fill2")
        pd_ = sum(ev_per_bet(x, "fill2") for x in days[d])
        if pd_ > 0:
            pos_days += 1
        print(f"  {d:<8s} {ad['n']:>5d} {ad['flip_rate'] * 100:>7.1f}% "
              f"{ad['mean_fill']:>7.3f} {ad['ev']:>+8.4f} {pd_:>+8.2f}")
    print(f"  正 P&L 天数: {pos_days}/{len(days)}")
    return bets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="../../data_0/lab_resolved")
    args = parser.parse_args()

    events = load_events(args.data)
    xs = collect_crossings(events)

    base = lambda x: x.flow_5s is not None and x.flow_5s >= FLOW_THR

    print("=" * 80)
    print("  Part 5: 强反向流过滤器精细化")
    print("=" * 80)

    # ── 0. 基础过滤器（新阈值 0.1, 平台内）──
    bets0 = one_bet_per_event(xs, base)
    day_table(f"基础: flow_5s ≥ {FLOW_THR}", bets0)

    # ── 1. 叠加剩余时间 ──
    for rem_lo, label in [(120, "剩余>120s"), (150, "剩余>150s"),
                          (180, "剩余>180s"), (210, "剩余>210s")]:
        pred = lambda x, lo=rem_lo: base(x) and x.remaining_sec > lo
        bets = one_bet_per_event(xs, pred)
        day_table(f"基础 + {label}", bets)

    # ── 2. 叠加触发价 ──
    for tri_hi, label in [(0.78, "触发价≤0.78"), (0.75, "触发价≤0.75")]:
        pred = lambda x, hi=tri_hi: base(x) and x.trigger_bid <= hi
        bets = one_bet_per_event(xs, pred)
        day_table(f"基础 + {label}", bets)

    # ── 3. 三条件: 基础 + 剩余>180 + 触发价≤0.78 ──
    pred3 = lambda x: base(x) and x.remaining_sec > 180 and x.trigger_bid <= 0.78
    day_table("基础 + 剩余>180s + 触发价≤0.78", one_bet_per_event(xs, pred3))

    # ── 4. 小时效应细查（全部通过穿越, 逐日看 12-18 vs 其他）──
    print(f"\n  ── 小时效应细查（基础过滤器内, 全部穿越）──")
    fl = [x for x in xs if base(x)]
    for label, pred in [("12-18 UTC", lambda x: 12 <= x.hour_utc < 18),
                        ("其他小时", lambda x: not (12 <= x.hour_utc < 18)),
                        ("0-8 UTC", lambda x: 0 <= x.hour_utc < 8),
                        ("8-12 UTC", lambda x: 8 <= x.hour_utc < 12),
                        ("18-24 UTC", lambda x: 18 <= x.hour_utc < 24)]:
        sub = [x for x in fl if pred(x)]
        a = stats(sub, "fill2")
        days = split_by_days(sub)
        day_evs = []
        for d in sorted(days):
            ad = stats(days[d], "fill2")
            if ad["n"] >= 3:
                day_evs.append(round(ad["ev"], 3))
        print(f"  {label:<12s} n={a['n']:>4d} 翻转率 {a['flip_rate'] * 100:>5.1f}% "
              f"EV {a['ev']:+.4f}  逐日EV: {day_evs}")

    # ── 5. 首穿越交互 ──
    pred5 = lambda x: base(x) and x.total_crosses_before == 1
    day_table("基础 + 事件内首穿越", one_bet_per_event(xs, pred5))

    # ── 6. YES/NO 分侧（基础）──
    for side in ("yes", "no"):
        pred = lambda x, s=side: base(x) and x.side == s
        day_table(f"基础 + {side.upper()} 侧", one_bet_per_event(xs, pred))

    # ── 7. 最终候选: 全样本 EV 与频率 ──
    print(f"\n  ── 候选策略频率与 EV 汇总（fill2, 每事件一注）──")
    t0, t1 = min(x.event_start for x in xs), max(x.event_start for x in xs)
    days_span = (t1 - t0) / 86400 + 1
    for label, pred in [
        ("基础 flow≥0.1", base),
        ("+剩余>180s", lambda x: base(x) and x.remaining_sec > 180),
        ("+触发价≤0.78", lambda x: base(x) and x.trigger_bid <= 0.78),
        ("+两者", pred3),
    ]:
        bets = one_bet_per_event(xs, pred)
        a = stats(bets, "fill2")
        ev0 = stats([x for x in bets], "fill0")
        print(f"  {label:<18s} n={a['n']:>4d} ({a['n'] / days_span:>4.1f}/天) "
              f"翻转率 {a['flip_rate'] * 100:>5.1f}% fill2 EV {a['ev']:+.4f} "
              f"fill0 EV {ev0['ev']:+.4f}")


if __name__ == "__main__":
    main()
