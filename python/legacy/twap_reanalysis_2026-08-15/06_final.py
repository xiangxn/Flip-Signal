#!/usr/bin/env python3
"""
Part 6: 最终候选对比与策略规格。

对 Part 5 的候选做最后一轮结构检查:
  * 穿越次序结构: 首穿越 vs 重复穿越 vs 对侧未穿越
  * 小时排除的稳健性
  * 最终推荐策略的完整规格 + 每日 P&L + 利润因子 + fill 敏感性

用法:
    python 06_final.py --data ../../data_0/lab_resolved
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (load_events, collect_crossings, stats, split_by_days,
                 ev_per_bet, day_of)

FLOW_THR = 0.1


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
    wins = [ev_per_bet(x, "fill2") for x in bets if ev_per_bet(x, "fill2") > 0]
    losses = [ev_per_bet(x, "fill2") for x in bets if ev_per_bet(x, "fill2") < 0]
    pf = sum(wins) / abs(sum(losses)) if losses else float("inf")
    pos = sum(1 for d in days if sum(ev_per_bet(x, "fill2") for x in days[d]) > 0)
    print(f"\n  【{title}】 n={a['n']}  翻转率 {a['flip_rate'] * 100:.1f}%  "
          f"fill {a['mean_fill']:.3f}  EV {a['ev']:+.4f}/股  "
          f"总P&L {sum(ev_per_bet(x, 'fill2') for x in bets):+.2f}  PF {pf:.2f}  "
          f"正天数 {pos}/{len(days)}")
    print(f"  {'日期':<8s} {'n':>5s} {'翻转率':>8s} {'fill':>7s} {'EV/股':>8s} {'P&L':>8s}")
    for d in sorted(days):
        ad = stats(days[d], "fill2")
        pd_ = sum(ev_per_bet(x, "fill2") for x in days[d])
        print(f"  {d:<8s} {ad['n']:>5d} {ad['flip_rate'] * 100:>7.1f}% "
              f"{ad['mean_fill']:>7.3f} {ad['ev']:>+8.4f} {pd_:>+8.2f}")
    return bets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="../../data_0/lab_resolved")
    args = parser.parse_args()

    events = load_events(args.data)
    xs = collect_crossings(events)
    base = lambda x: x.flow_5s is not None and x.flow_5s >= FLOW_THR

    print("=" * 80)
    print("  Part 6: 最终候选对比")
    print("=" * 80)

    # ── 穿越次序结构（基础过滤器内, 全部穿越）──
    fl = [x for x in xs if base(x)]
    print(f"\n  ── 穿越次序结构（基础过滤器内, 全部穿越 n={len(fl)}）──")
    for label, pred in [
        ("事件内首穿越(两侧均首次)", lambda x: x.total_crosses_before == 1),
        ("同侧第2+次(对侧未穿越)", lambda x: x.cross_count_same >= 2
                                          and not x.other_crossed_before),
        ("对侧已穿越过(振荡)", lambda x: x.other_crossed_before),
    ]:
        sub = [x for x in fl if pred(x)]
        a = stats(sub, "fill2")
        print(f"  {label:<26s} n={a['n']:>4d} 翻转率 {a['flip_rate'] * 100:>5.1f}% "
              f"fill {a['mean_fill']:.3f} EV {a['ev']:+.4f}")

    # ── 候选逐一对比 ──
    candidates = [
        ("A 基础 flow≥0.1", base),
        ("B + 剩余>120s", lambda x: base(x) and x.remaining_sec > 120),
        ("C + 触发价≤0.78", lambda x: base(x) and x.trigger_bid <= 0.78),
        ("D + 剩余>120s + 触发≤0.78",
         lambda x: base(x) and x.remaining_sec > 120 and x.trigger_bid <= 0.78),
        ("E + 非12-18 UTC", lambda x: base(x) and not (12 <= x.hour_utc < 18)),
        ("F + 非12-18 UTC + 剩余>120s",
         lambda x: base(x) and not (12 <= x.hour_utc < 18)
                   and x.remaining_sec > 120),
        ("G + 对侧未穿越过", lambda x: base(x) and not x.other_crossed_before),
        ("H + 非首穿越(同侧≥2次)", lambda x: base(x) and x.cross_count_same >= 2),
    ]
    for label, pred in candidates:
        day_table(label, one_bet_per_event(xs, pred))

    # ── 最终推荐: 组合最优者 ──
    final = lambda x: (base(x) and x.remaining_sec > 120 and x.trigger_bid <= 0.78
                       and not (12 <= x.hour_utc < 18))
    bets = day_table("FINAL: flow≥0.1 + rem>120 + trigger≤0.78 + 非12-18UTC",
                     one_bet_per_event(xs, final))

    # ── 最终策略规格 ──
    print(f"\n  ── 最终策略规格（fill2 口径）──")
    a = stats(bets, "fill2")
    a0 = stats(bets, "fill0")
    t_span = (max(x.event_start for x in xs) - min(x.event_start for x in xs)) / 86400 + 1
    print(f"  信号频率: {a['n'] / t_span:.1f} 注/天")
    print(f"  翻转率: {a['flip_rate'] * 100:.1f}%  fill2={a['mean_fill']:.3f} "
          f"EV={a['ev']:+.4f}/股  总P&L={sum(ev_per_bet(x, 'fill2') for x in bets):+.2f} 股")
    print(f"  立即成交(fill0): fill={a0['mean_fill']:.3f} EV={a0['ev']:+.4f}/股")
    # 满仓假设: 每注等额 1 单位
    bankroll_growth = sum(ev_per_bet(x, "fill2") for x in bets)
    print(f"  8 天累计(等额1单位/注): {bankroll_growth:+.2f} 单位")

    # ── 稳健性: 相邻阈值扰动 ──
    print(f"\n  ── 阈值扰动稳健性（flow 阈值 0.05/0.1/0.2, 其他条件不变）──")
    for ft in [0.05, 0.1, 0.2]:
        pred = lambda x, t=ft: (x.flow_5s is not None and x.flow_5s >= t
                                and x.remaining_sec > 120 and x.trigger_bid <= 0.78
                                and not (12 <= x.hour_utc < 18))
        b = one_bet_per_event(xs, pred)
        a2 = stats(b, "fill2")
        print(f"  flow≥{ft:.2f}: n={a2['n']:>4d} 翻转率 {a2['flip_rate'] * 100:>5.1f}% "
              f"EV {a2['ev']:+.4f}/股  P&L {sum(ev_per_bet(x, 'fill2') for x in b):+.2f}")

    # ── 稳健性: remaining 阈值扰动 ──
    print(f"\n  ── 剩余时间阈值扰动（100/120/150s, 其他条件不变）──")
    for rt in [100, 120, 150]:
        pred = lambda x, t=rt: (x.flow_5s is not None and x.flow_5s >= 0.1
                                and x.remaining_sec > t and x.trigger_bid <= 0.78
                                and not (12 <= x.hour_utc < 18))
        b = one_bet_per_event(xs, pred)
        a2 = stats(b, "fill2")
        print(f"  rem>{rt:>3d}s: n={a2['n']:>4d} 翻转率 {a2['flip_rate'] * 100:>5.1f}% "
              f"EV {a2['ev']:+.4f}/股  P&L {sum(ev_per_bet(x, 'fill2') for x in b):+.2f}")


if __name__ == "__main__":
    main()
