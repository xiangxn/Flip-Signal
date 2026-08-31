#!/usr/bin/env python3
"""
01 基率复核（v2 数据, 计划 §7.1）—— 新 1s 数据重算 only/both 结构与类别上界。

目标: 管线 sanity check —— 确认老结论（2026-08-15 重推导）在新数据可复现:
  * only 类（整窗仅一侧穿越）: 该侧 ~97%+ 赢 → follow 方向, EV 上界 +0.19/股
  * both 类（两侧都穿越）: 首个穿越侧只赢 ~20% → flip 方向, EV 上界 +0.51/股
对比口径: v1 为 5s 快照 + T+2 ticks(10s) 确认; v2 为 1s ticks, 报告 +2s/+10s 双确认。

用法: ./venv/bin/python v2/01_base_rate.py
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import load_events, extract_cross

DATA = "../data/btc"


def _ci(rate: float, n: int) -> str:
    if n < 5:
        return "—"
    se = (rate * (1 - rate) / n) ** 0.5
    return f"±{1.96 * se * 100:.1f}pp"


def summarize(xs, title: str, fill_key: str, won_key: str) -> None:
    """按 (fill, won) 汇总: n/胜率/均价/EV。"""
    if not xs:
        print(f"  {title}: 无数据")
        return
    fills = [x[fill_key] for x in xs if x.get(fill_key) is not None]
    if not fills:
        print(f"  {title}: 无有效 fill")
        return
    n = len(fills)
    wr = sum(1 for x in xs if x[won_key]) / n
    mf = sum(fills) / n
    ev = sum((1.0 - f) if x[won_key] else -f for x, f in
             zip(xs, fills) if x.get(fill_key) is not None) / n
    print(f"  {title:<28s} n={n:>4d} 胜率={wr * 100:>5.1f}% ({_ci(wr, n)}) "
          f"均价={mf:.3f} EV={ev:+.4f}/股")


def main():
    events = load_events(DATA)
    obs = extract_cross(events, "outcome")
    if not obs:
        print("无观测。")
        sys.exit(1)

    both_n = sum(1 for o in obs if o["cls"] == "both")
    n = len(obs)
    cross_ev = len(obs) / len(events)
    sides = Counter(o["side"] for o in obs)
    by_day = Counter(o["event_start"] // 86400 for o in obs)
    both_days = Counter(o["event_start"] // 86400 for o in obs if o["cls"] == "both")

    print(f"数据: {DATA}  |  事件 {len(events)}  |  首个穿越观测 {n} "
          f"(穿越率 {cross_ev * 100:.1f}%)  |  跨度 "
          f"{len(by_day)} 天")
    print(f"穿越侧分布: YES {sides['yes']} / NO {sides['no']}  |  "
          f"both {both_n} ({both_n / n * 100:.1f}%) / only {n - both_n} ({100 - both_n / n * 100:.1f}%)")
    print()

    print("=" * 70)
    print("  类别上界（整窗信息, 不可交易）—— v2 1s 口径 vs 旧结论")
    print("=" * 70)
    for ds in (2, 10):
        print(f"\n  ── 确认 +{ds}s ──")
        print(f"  {'类别':<10s} {'n':>5s} {'follow胜率':>10s} {'followEV':>10s} {'flipEV':>10s}")
        for cls in ("only", "both"):
            xs = [o for o in obs if o["cls"] == cls]
            if not xs:
                continue
            fills = [o[f"fill{ds}s"] for o in xs]
            flips = [o[f"flip_fill{ds}s"] for o in xs]
            wr = sum(1 for o in xs if o["won"]) / len(xs)
            fev = sum((1.0 - f) if o["won"] else -f for o, f in zip(xs, fills)) / len(xs)
            # flip = 买对侧, 价格 flip_fill; 穿越侧赢(≠won)亏 fill, 对侧赢(==!won)赚 1-fill
            rev = sum(-f if o["won"] else (1.0 - f) for o, f in zip(xs, flips)) / len(xs)
            print(f"  {cls:<10s} {len(xs):>5d} {wr * 100:>9.1f}% {fev:>+10.4f} {rev:>+10.4f}")

    # 对比旧结论注释（来自 docs/twap_flip_rederivation_2026-08-15.md）
    print("\n  旧结论参考: only 类 follow WR ~97%+/EV 上界 +0.19/股; "
          "both 类首个穿越侧 WR ~20%, flip EV 上界 +0.51/股")

    print()
    print("=" * 70)
    print("  按天稳定性（both 率 / 穿越率）")
    print("=" * 70)
    print(f"  {'日':<10s} {'事件':>5s} {'穿越':>5s} {'both':>5s} {'both率':>7s}")
    for d in sorted(by_day):
        ev_day = sum(1 for e in events if e["start_time"] // 86400 == d)
        print(f"  {d:<10d} {ev_day:>5d} {by_day[d]:>5d} {both_days[d]:>5d} "
              f"{both_days[d] / by_day[d] * 100:>6.1f}%")

    # both 类时间结构: 首次穿越与对侧反穿的时间间隔（flip 线最关心: 决策有多早）
    print()
    print("=" * 70)
    print("  both 类时序结构: 首次穿越 → 对侧反穿 0.7 的间隔")
    print("=" * 70)
    gaps = []
    for event in events:
        ticks = event.get("ticks") or []
        state = {"yes": False, "no": False}
        first = None
        cross_t = {}
        for t in ticks:
            rem = t.get("rem")
            if rem is None or not (15 < rem < 260):
                continue
            for side in ("yes", "no"):
                bid = (t.get("pm") or {}).get(f"{side}_bid") or 0
                if bid > 0.7 and not state[side]:
                    if first is None:
                        first = side
                    cross_t[side] = t["ts"]
                state[side] = bid > 0.7
        if first and len(cross_t) == 2:
            gaps.append((cross_t["no" if first == "yes" else "yes"] - cross_t[first]) / 1000.0)
    if gaps:
        gaps.sort()
        n = len(gaps)
        print(f"  n={n}  中位 {gaps[n // 2]:.0f}s  均值 {sum(gaps) / n:.0f}s  "
              f"p25 {gaps[n // 4]:.0f}s  p75 {gaps[3 * n // 4]:.0f}s")
        for g in (30, 60, 120, 180, 240):
            print(f"  ≤{g}s 内反穿: {sum(1 for x in gaps if x <= g) / n * 100:.1f}%")


if __name__ == "__main__":
    main()
