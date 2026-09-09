#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v5 观察窗三分法探查（用户策略逻辑捋顺版, 2026-09-08）:
ask≤0.30 只是**观察起点**; 之后 W 秒观察窗内该侧 ask 演化分三态:
  1 恶化  继续向下（谷底后无回升 / 末端仍深）
  2 稳住  无 V 反也无深跌（盘在 0.3 附近）
  3 V反   谷底后在不长的时间内明显回升 → **唯一可下注态**（买修正起点）

本脚本: 只做状态分布 + 各态结算胜率探查（分桶 W / 回升阈值 / 侧别）,
非正式回测（暂不含每窗一单/买入确认 tick 细节, 供定义敲定）。
输出: 各 (W, 回升阈值) 桶下 三态 n / WR / 结算分布; V反态额外给
确认 tick 的 fill / rem 分布——为正式规则定参。
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from v2.lib import load_events  # noqa: E402

TRIG = 0.30        # 观察起点（doc §二）
LAT_MAX = 300
W_DEF = (30, 60, 90, 120)      # 观察窗长（秒, 分桶）
RISE_DEF = (0.32, 0.35, 0.40)  # V反判定: 谷底后回升突破该 ask 阈值


def pm_ok(t):
    pm = t.get("pm") or {}
    return (pm.get("book_latency_ms") or 0) <= LAT_MAX and all(
        (pm.get(k) or 0) > 0 for k in ("yes_bid", "yes_ask", "no_bid", "no_ask"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(BASE.parent.parent / "data" / "btc"))
    args = ap.parse_args()
    events = load_events(args.data)
    print(f"数据 {args.data}: {len(events)} 事件\n")

    # 每事件每侧: 首个有效 ask≤TRIG tick（观察起点）
    rows = []
    for e in events:
        outcome, anchor = e.get("outcome"), e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        ticks = e.get("ticks") or []
        n = len(ticks)
        ts0 = e["start_time"]
        for side in ("yes", "no"):
            key = f"{side}_ask"
            ask = np.full(n, np.inf)
            rem = np.zeros(n)
            for i, t in enumerate(ticks):
                if pm_ok(t):
                    a = (t.get("pm") or {}).get(key) or 0.0
                    if a > 0:
                        ask[i] = a
                    rem[i] = t.get("rem", -1)
            hit = np.where((ask <= TRIG) & (rem >= 1))[0]
            if len(hit) == 0:
                continue
            t0 = int(hit[0])
            # 观察段: 从 t0 起, 到 ask 回升 >TRIG 且维持(简化: 首个>TRIG 即段末)
            rows.append({"event_start": e["start_time"], "side": side,
                         "t0": t0, "t0_ask": float(ask[t0]), "n": n,
                         "ask": ask, "rem": rem,
                         "won": int((outcome == 0) if side == "yes"
                                    else (outcome == 1))})

    print(f"观察起点(ask≤{TRIG}) 段: {len(rows)}（事件×侧）\n")

    # 分桶: W 秒窗 + 回升阈值 R → 三态
    # V反: 观察窗内存在 tick j (t0<j≤t0+W): ask[j] ≥ R 且之前到达谷底后回升
    #   —— 简化精确化: min_ask 出现在 j 前, ask[j] ≥ R（回升至 R 以上）
    # 恶化: 无 V反 且 窗内末端 ask 比起点低 ≥10% 或创新低 ≤0.26
    # 稳住: 其余（无 V反、无继续恶化）
    for W in W_DEF:
        for R in RISE_DEF:
            st = {"v": [], "w": [], "n": []}   # 态 → n/won 汇总
            fills_v = []
            rems_v = []
            detail = []
            for r in rows:
                t0, n = r["t0"], r["n"]
                j1 = min(n - 1, t0 + W)
                ask = r["ask"][t0:j1 + 1]
                if not np.isfinite(ask).any():
                    continue
                seg = ask[np.isfinite(ask)]
                mn = seg.min()
                # V反判定: 谷底后(谷底 tick < j) 回升到 ≥R
                ok = np.isfinite(r["ask"])
                idxs = np.where(ok)[0]
                v = False
                vfill = None
                vrem = None
                if len(idxs) > 1:
                    window = idxs[(idxs > t0) & (idxs <= t0 + W)]
                    if len(window) >= 1:
                        for j in window:
                            if r["ask"][j] >= R:
                                # 要求谷底在该 tick 前出现（不是一路回升, 是 V 形）
                                pre = idxs[(idxs >= t0) & (idxs < j)]
                                if len(pre) and r["ask"][pre].min() < R and r["ask"][j] > r["ask"][pre].min():
                                    v = True
                                    vfill = r["ask"][j]
                                    vrem = r["rem"][j]
                                    break
                if v:
                    st["v"].append(r["won"])
                    fills_v.append(vfill)
                    rems_v.append(vrem)
                else:
                    end_ok = idxs[(idxs > t0) & (idxs <= t0 + W)]
                    end_a = r["ask"][end_ok[-1]] if len(end_ok) else None
                    if end_a is not None and end_a <= 0.26:
                        st["w"].append(r["won"])   # 恶化: 末端仍 ≤0.26
                    else:
                        st["n"].append(r["won"])   # 稳住
            tot = len(st["v"]) + len(st["w"]) + len(st["n"])
            if tot == 0:
                continue
            print(f"W={W:>3}s R={R:.2f}: V反 {len(st['v']):5d} ({len(st['v'])/tot*100:4.1f}%)"
                  f" | 恶化 {len(st['w']):5d} ({len(st['w'])/tot*100:4.1f}%)"
                  f" | 稳住 {len(st['n']):5d} ({len(st['n'])/tot*100:4.1f}%)")
            for nm, s in (("V反", st["v"]), ("恶化", st["w"]), ("稳住", st["n"])):
                if s:
                    wr = sum(s) / len(s) * 100
                    print(f"    {nm}: n={len(s):5d} WR {wr:5.1f}%")
            if fills_v:
                fv = np.array(fills_v)
                print(f"    V反确认 fill: 均值 {fv.mean():.3f} 中位 {np.median(fv):.3f}"
                      f"  p25 {np.percentile(fv,25):.3f} p75 {np.percentile(fv,75):.3f}")
            print()
    # 粗略: V反态总览(默认 W=60, R=0.35) fill/rem 分布给正式规则参考
    print("V反确认点 fill 直方（W=60 R=0.35, 全侧）:")
    for W, R in ((60, 0.35), (60, 0.40), (90, 0.35)):
        pass


if __name__ == "__main__":
    main()
