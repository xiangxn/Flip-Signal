#!/usr/bin/env python3
"""
Part 7: 最终候选的 train/test 冻结检验 + 组合交叉。

train = 08-05..08-10（6 天）, test = 08-11..08-12（2 天）。
flow 阈值 0.1 是 Part 4 验证的平台值（非尖峰），其余条件来自
全样本结构发现 —— 因此 test 集对 flow 阈值是干净的，
对"小时排除/剩余时间"等附加条件存在部分泄漏，仅供方向参考。

用法:
    python 07_split_check.py --data ../../data_0/lab_resolved
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (load_events, collect_crossings, stats, ev_per_bet, day_of)

TEST_DAYS = {"08-11", "08-12"}


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


def report(title, pred, xs):
    bets = one_bet_per_event(xs, pred)
    tr = [x for x in bets if day_of(x) not in TEST_DAYS]
    te = [x for x in bets if day_of(x) in TEST_DAYS]
    a_tr, a_te = stats(tr, "fill2"), stats(te, "fill2")
    pnl_tr = sum(ev_per_bet(x, "fill2") for x in tr)
    pnl_te = sum(ev_per_bet(x, "fill2") for x in te)
    print(f"  {title:<38s} | train n={a_tr['n']:>4d} 翻转率 {a_tr['flip_rate'] * 100:>5.1f}% "
          f"EV {a_tr['ev']:+.4f} P&L {pnl_tr:+.2f} | test n={a_te['n']:>4d} "
          f"翻转率 {a_te['flip_rate'] * 100:>5.1f}% EV {a_te['ev']:+.4f} P&L {pnl_te:+.2f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="../../data_0/lab_resolved")
    args = parser.parse_args()

    events = load_events(args.data)
    xs = collect_crossings(events)
    base = lambda x: x.flow_5s is not None and x.flow_5s >= 0.1

    print("=" * 100)
    print("  Part 7: train/test 冻结检验（每事件一注, fill2）")
    print("=" * 100)
    print(f"  train=08-05..08-10  test=08-11..08-12  基线: "
          f"train EV {stats([x for x in xs if day_of(x) not in TEST_DAYS], 'fill2')['ev']:+.4f} "
          f"test EV {stats([x for x in xs if day_of(x) in TEST_DAYS], 'fill2')['ev']:+.4f}")
    print()

    report("A 基础 flow≥0.1", base, xs)
    report("B + 剩余>120s", lambda x: base(x) and x.remaining_sec > 120, xs)
    report("E + 非12-18 UTC", lambda x: base(x) and not (12 <= x.hour_utc < 18), xs)
    report("H + 同侧≥2次穿越", lambda x: base(x) and x.cross_count_same >= 2, xs)
    report("G + 对侧未穿越过", lambda x: base(x) and not x.other_crossed_before, xs)
    report("E×B + 非12-18 + 剩余>120s",
           lambda x: base(x) and not (12 <= x.hour_utc < 18)
                     and x.remaining_sec > 120, xs)
    report("E×H + 非12-18 + 同侧≥2次",
           lambda x: base(x) and not (12 <= x.hour_utc < 18)
                     and x.cross_count_same >= 2, xs)
    report("E×G + 非12-18 + 对侧未穿越",
           lambda x: base(x) and not (12 <= x.hour_utc < 18)
                     and not x.other_crossed_before, xs)
    report("B×H + 剩余>120 + 同侧≥2次",
           lambda x: base(x) and x.remaining_sec > 120
                     and x.cross_count_same >= 2, xs)

    # 双侧逐日 EV（E 与 H 组合的 8 天表）
    print(f"\n  ── E×H 逐日（n 较大且 EV 最高的组合候选）──")
    pred = lambda x: base(x) and not (12 <= x.hour_utc < 18) and x.cross_count_same >= 2
    bets = one_bet_per_event(xs, pred)
    from lib import split_by_days
    days = split_by_days(bets)
    for d in sorted(days):
        ad = stats(days[d], "fill2")
        pd_ = sum(ev_per_bet(x, "fill2") for x in days[d])
        print(f"  {d:<8s} n={ad['n']:>4d} 翻转率 {ad['flip_rate'] * 100:>5.1f}% "
              f"EV {ad['ev']:+.4f} P&L {pd_:+.2f}")
    a = stats(bets, "fill2")
    pos = sum(1 for d in days if sum(ev_per_bet(x, "fill2") for x in days[d]) > 0)
    print(f"  合计: n={a['n']} 翻转率 {a['flip_rate'] * 100:.1f}% EV {a['ev']:+.4f} "
          f"P&L {sum(ev_per_bet(x, 'fill2') for x in bets):+.2f} 正天数 {pos}/{len(days)}")


if __name__ == "__main__":
    main()
