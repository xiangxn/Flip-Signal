#!/usr/bin/env python3
"""
follow 深挖 —— 在 v3（data/btc 14 天, v2 格式）上复验历史结论 + 新视角。

历史结论（eth 分支 2026-08-16, docs/twap_flip_rederivation）:
  1. 无脑 follow 首个穿越: EV ≈ 0（市场有效）
  2. 未来信息上界: only 类 97-99% 赢（EV +0.19）, both 类 20% 赢（EV -0.51）
  3. 实时可知 wait 变体（单侧穿越 + rem≤60）: 胜率 88-93% 但 fill 已 reprice,
     EV 仅 +0.01~+0.02; fill 甜区 (0.75,0.85] EV +0.04~+0.09（n 小）
  4. v2 checkpoint（2026-08-31）: 旧 follow/wait 结论在 2 周数据"失效"

本次验证（全部决策时刻可知）:
  A. wait 变体 rem 阈值扫描（v2 ticks 格式重新实现）
  B. wait × fill 甜区
  C. 深触发 trigger_bid ≥ 0.80/0.85（v3 数据上唯一正 EV 区）: only 率 + 分日
  D. 确认时刻对侧 bid 阈值扫描（对侧远低于 0.7 = 单侧强势代理）
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from v2.lib import load_events

REPO = Path(__file__).resolve().parent.parent.parent
DATA = str(REPO / "data" / "btc")
TRIGGER = 0.7


def tick_pm(t, side, field):
    pm = t.get("pm") or {}
    return pm.get(f"{side}_{field}") or 0.0


def wilson(wr, n):
    z = 1.96
    denom = 1 + z**2 / n
    p = (wr + z**2 / (2 * n)) / denom
    h = z * np.sqrt(wr * (1 - wr) / n + z**2 / (4 * n**2)) / denom
    return p - h, p + h


def summ(rows, fill_key="fill"):
    d = [r for r in rows if r.get(fill_key)]
    n = len(d)
    if n < 10:
        return None
    wr = np.mean([r["won"] for r in d])
    fill = np.mean([r[fill_key] for r in d])
    lo, hi = wilson(wr, n)
    return n, wr, lo, hi, fill, wr - fill


def print_row(tag, s):
    if s is None:
        print(f"  {tag}: n<10 跳过")
        return
    n, wr, lo, hi, fill, ev = s
    print(f"  {tag}: n={n:4d}  WR={wr*100:5.1f}% [{lo*100:4.1f},{hi*100:4.1f}]  "
          f"fill={fill:.3f}  EV={ev*100:+6.2f}/100股")


def wait_variant(events, rem_max, min_rem=15):
    """wait 变体: 全程跟踪两侧穿越, 在第一个 rem ≤ rem_max 且截止当前仅一侧
    穿越过 0.7 的 tick 跟进该侧（决策时刻特征全部可知, 无未来数据）。
    fill = 触发侧 ask@决策 tick = 1 - 对侧 bid（互补口径）。"""
    rows = []
    for ev in events:
        if ev.get("outcome") is None:
            continue
        ticks = ev.get("ticks") or []
        if len(ticks) < 20:
            continue
        crossed = set()
        hit = False
        for t in ticks:
            rem = t.get("rem")
            if rem is None or rem < min_rem:
                continue
            yb, nb = tick_pm(t, "yes", "bid"), tick_pm(t, "no", "bid")
            if yb > TRIGGER:
                crossed.add("yes")
            if nb > TRIGGER:
                crossed.add("no")
            if rem <= rem_max and len(crossed) == 1 and not hit:
                side = next(iter(crossed))
                other = "no" if side == "yes" else "yes"
                ob = tick_pm(t, other, "bid")
                if ob > 0:  # 对侧盘口有效
                    fill = 1.0 - ob
                    won = (ev["outcome"] == 0) if side == "yes" else (ev["outcome"] == 1)
                    rows.append({"side": side, "rem": rem, "fill": fill, "won": won,
                                 "trigger_bid": tick_pm(t, side, "bid"),
                                 "oth_bid": ob, "event_start": ev["start_time"]})
                hit = True  # 每事件一注
            if len(crossed) == 2 and rem <= rem_max:
                break
    return rows


def deep_trigger(events, tb_min):
    """深触发: 首个穿越（0.7 上升沿）时 trigger_bid ≥ tb_min → follow。
    附带 only/both 整窗类别（仅解剖用）。"""
    rows = []
    for ev in events:
        if ev.get("outcome") is None:
            continue
        ticks = ev.get("ticks") or []
        if len(ticks) < 20:
            continue
        state = {"yes": False, "no": False}
        crossed = set()
        first = None
        for i, t in enumerate(ticks):
            rem = t.get("rem")
            if rem is None or not (15 < rem < 260):
                continue
            for side in ("yes", "no"):
                b = tick_pm(t, side, "bid")
                above = b > TRIGGER
                if above and not state[side]:
                    crossed.add(side)
                    if first is None:
                        first = (i, side, b)
                state[side] = above
        if first is None:
            continue
        i, side, bid = first
        if bid < tb_min:
            continue
        other = "no" if side == "yes" else "yes"
        cls = "both" if len(crossed) == 2 else "only"
        won = (ev["outcome"] == 0) if side == "yes" else (ev["outcome"] == 1)
        t0 = ticks[i]
        fill0 = 1.0 - tick_pm(t0, other, "bid")
        j10 = min(i + 10, len(ticks) - 1)
        fill10 = 1.0 - tick_pm(ticks[j10], other, "bid")
        oth_bid10 = tick_pm(ticks[j10], other, "bid")
        rows.append({"side": side, "cls": cls, "won": won, "fill0": fill0,
                     "fill10": fill10, "trigger_bid": bid, "oth_bid10": oth_bid10,
                     "event_start": ev["start_time"]})
    return rows


def oth_bid_gate(events, gate):
    """确认时刻（+10s）对侧 bid ≤ gate → follow（单侧强势代理）。"""
    rows = []
    for ev in events:
        if ev.get("outcome") is None:
            continue
        ticks = ev.get("ticks") or []
        if len(ticks) < 20:
            continue
        state = {"yes": False, "no": False}
        first = None
        for i, t in enumerate(ticks):
            rem = t.get("rem")
            if rem is None or not (15 < rem < 260):
                continue
            for side in ("yes", "no"):
                b = tick_pm(t, side, "bid")
                above = b > TRIGGER
                if above and not state[side]:
                    if first is None:
                        first = (i, side)
                state[side] = above
        if first is None:
            continue
        i, side = first
        other = "no" if side == "yes" else "yes"
        j10 = min(i + 10, len(ticks) - 1)
        ob10 = tick_pm(ticks[j10], other, "bid")
        if ob10 > gate:
            continue
        won = (ev["outcome"] == 0) if side == "yes" else (ev["outcome"] == 1)
        fill10 = 1.0 - ob10
        rows.append({"won": won, "fill10": fill10, "event_start": ev["start_time"]})
    return rows


def main():
    events = load_events(DATA)
    print(f"数据: {DATA} | 事件 {len(events)}")

    # ── A. wait 变体 rem 扫描 ──
    print("\n===== A. wait 变体（仅一侧穿越 + rem≤X 跟进, 实时可知） =====")
    for rm in (30, 60, 90, 120, 180):
        rows = wait_variant(events, rm)
        s = summ(rows)
        print_row(f"rem≤{rm:3d}", s)

    # ── B. wait × fill 甜区 ──
    print("\n===== B. wait(rem≤60) × fill 甜区 =====")
    base = wait_variant(events, 60)
    for lo, hi in ((0.0, 0.75), (0.75, 0.85), (0.85, 1.0)):
        rows = [r for r in base if lo < r["fill"] <= hi]
        print_row(f"fill∈({lo},{hi}]", summ(rows))

    # ── C. 深触发: trigger_bid 高区 ──
    print("\n===== C. 深触发（首个穿越一步拉高） =====")
    for tb in (0.75, 0.80, 0.85, 0.90):
        rows = deep_trigger(events, tb)
        s = summ(rows, "fill10")
        print_row(f"trigger_bid≥{tb:.2f}（+10s fill）", s)
        if len(rows) >= 10:
            only = [r for r in rows if r["cls"] == "only"]
            both = [r for r in rows if r["cls"] == "both"]
            print(f"      only 率 {len(only)/len(rows)*100:.0f}% "
                  f"(only WR {np.mean([r['won'] for r in only])*100:.0f}% n={len(only)} | "
                  f"both WR {np.mean([r['won'] for r in both])*100:.0f}% n={len(both)})")

    # ── C2. 深触发分日稳定性（≥0.85） ──
    rows85 = deep_trigger(events, 0.85)
    if len(rows85) >= 10:
        import collections
        by_day = collections.defaultdict(list)
        for r in rows85:
            from datetime import datetime, timezone
            d = datetime.fromtimestamp(r["event_start"], tz=timezone.utc).date().isoformat()
            by_day[d].append(r)
        print("\n  分日（trigger_bid≥0.85, +10s fill）:")
        for d in sorted(by_day):
            s = summ(by_day[d], "fill10")
            print_row(f"    {d}", s)

    # ── D. 确认时刻（+10s）对侧 bid gate 扫描 ──
    print("\n===== D. +10s 对侧 bid ≤ gate 时 follow（单侧强势代理） =====")
    for g in (0.50, 0.55, 0.60, 0.65, 0.70):
        rows = oth_bid_gate(events, g)
        s = summ(rows, "fill10")
        print_row(f"对侧 bid≤{g:.2f}", s)


if __name__ == "__main__":
    main()
