#!/usr/bin/env python3
"""
v3 follow 2U/注回测 —— 实际收益（USDC）。

口径与 flip 定稿回测（06_backtest）1:1 对称:
  * 观测: 每事件首个 0.7 上升沿（extract_cross）
  * follow = 买触发侧（顺势）; fill = 1 - 对侧 bid@决策时刻（互补口径,
    对侧 bid 即真实最优 ask, 数据互补恒等 99.98%）
  * 决策: 穿越 +10s 确认（flip 定稿同节奏）; 另给 +0s 穿越即入（用户"0.7 入场"直觉）
  * 2U/注: shares = 2/fill; 赢 → shares-2, 输 → -2（每股兑 1U, 官方结算）
  * 标签: won = 触发侧赢

变体:
  A. 全量 follow（+10s 确认）           A'. 真实 ask 口径对照
  B. 穿越即入（+0s, "0.7 入场"直觉）    B2. +10s fill≤0.73（0.7 左右入场子集）
  C. wait 尾盘（rem≤30/60 仅一侧穿越, 07b 唯一正 EV 族）
对照: 定稿 flip（C1>0.73 & post_end≤0.66, flip_fill10s）
输出: 总 P&L / 每注 / ROI / 按日 / 累计
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from v2.lib import load_events, extract_cross

REPO = Path(__file__).resolve().parent.parent.parent
DATA = str(REPO / "data" / "btc")
STAKE = 2.0
TRIGGER = 0.7


def enrich_obs(events, obs):
    """补各决策时刻的 fill 口径: real = 触发侧真实 ask, comp = 1 - 对侧 bid。"""
    by_start = {e["start_time"]: e for e in events}
    rows = []
    for o in obs:
        ev = by_start[o["event_start"]]
        ticks = ev.get("ticks") or []
        side, other, i = o["side"], o["other"], o["i"]
        r = dict(o)

        def snap(j):
            pm = ticks[min(j, len(ticks) - 1)].get("pm") or {}
            return {
                "bid": pm.get(f"{side}_bid") or 0.0,
                "ask": pm.get(f"{side}_ask") or 0.0,
                "oth_bid": pm.get(f"{other}_bid") or 0.0,
            }

        for ds in (0, 10):
            s = snap(i + ds)
            r[f"fill_real{ds}s"] = s["ask"] or None          # 真实吃单价
            r[f"fill_comp{ds}s"] = 1.0 - s["oth_bid"] if s["oth_bid"] else None
            r[f"side_bid{ds}s"] = s["bid"] or None           # 触发侧 bid（flip 用）
        rows.append(r)
    return rows


def wait_variant(events, rem_max, min_rem=15):
    """wait 变体: 全程跟踪两侧穿越, 在第一个 rem≤rem_max 且截止当前仅一侧
    穿越过的 tick 跟进该侧（决策时刻特征全部可知）。fill = 1 - 对侧 bid。"""
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
            pm = t.get("pm") or {}
            yb, nb = pm.get("yes_bid") or 0.0, pm.get("no_bid") or 0.0
            if yb > TRIGGER:
                crossed.add("yes")
            if nb > TRIGGER:
                crossed.add("no")
            if rem <= rem_max and len(crossed) == 1 and not hit:
                side = next(iter(crossed))
                other = "no" if side == "yes" else "yes"
                ob = pm.get(f"{other}_bid") or 0.0
                if ob > 0:
                    fill = 1.0 - ob
                    won = (ev["outcome"] == 0) if side == "yes" else (ev["outcome"] == 1)
                    rows.append({"won": won, "fill": fill,
                                 "event_start": ev["start_time"]})
                hit = True  # 每事件一注
            if len(crossed) == 2 and rem <= rem_max:
                break
    return rows


def pl_series(won, fill):
    """2U/注 P&L: shares = 2/fill, 赢 → shares-2, 输 → -2。"""
    won = np.asarray(won, dtype=float)
    fill = np.asarray(fill, dtype=float)
    shares = STAKE / fill
    return np.where(won > 0, shares - STAKE, -STAKE)


def backtest(df, fill_key, name):
    d = df[df[fill_key].gt(0)].dropna(subset=[fill_key]).copy()
    n = len(d)
    if n == 0:
        print(f"\n[{name}] 无样本")
        return
    wr = d["won"].mean()
    fill = d[fill_key].mean()
    pl = pl_series(d["won"], d[fill_key])
    total, per, roi = pl.sum(), pl.mean(), pl.sum() / (STAKE * n)
    print(f"\n===== {name} =====")
    print(f"  n={n:4d}  WR={wr*100:5.1f}%  fill={fill:.3f}  EV/股={wr-fill:+.4f}")
    print(f"  2U/注: 总 P&L {total:+8.2f} USDC | 每注 {per:+.4f} USDC | "
          f"ROI {roi*100:+.2f}%")
    day_rows, cum = [], 0.0
    for dd, g in d.groupby("date"):
        gpl = pl_series(g["won"], g[fill_key])
        cum += gpl.sum()
        day_rows.append((dd, len(g), gpl.sum(), cum))
    pos = sum(1 for _, _, p, _ in day_rows if p > 0)
    print(f"  逐日 P&L 正天数: {pos}/{len(day_rows)}")
    print("  " + "  ".join(f"{dd[5:]}: {p:+6.1f}({cumv:+7.1f})"
                           for dd, n2, p, cumv in day_rows))


def main():
    events = load_events(DATA)
    obs = extract_cross(events, "outcome")
    df = pd.DataFrame(enrich_obs(events, obs))
    df["date"] = pd.to_datetime(df["event_start"], unit="s").dt.date.astype(str)
    print(f"数据: {DATA} | 事件 {len(events)} | follow 观测 {len(df)}")

    # A / A'. 全量 follow +10s 确认（flip 定稿同节奏）
    backtest(df, "fill_comp10s", "A.  全量 follow（+10s 确认, 互补口径）")
    backtest(df, "fill_real10s", "A'. 全量 follow（+10s 真实 ask 对照）")

    # B. 穿越即入（用户"0.7 入场"直觉: 触发时价格即 ~0.7-0.75）
    backtest(df, "fill_comp0s", "B.  穿越即入 follow（+0s）")

    # B2. 0.7 左右入场子集: 确认时 fill≤0.73
    cheap = df[df["fill_comp10s"].le(0.73)]
    backtest(cheap, "fill_comp10s", "B2. fill≤0.73 子集（0.7 左右入场）")

    # C. wait 尾盘（07b 唯一正 EV 族）
    for rm in (30, 60):
        wrows = wait_variant(events, rm)
        wdf = pd.DataFrame(wrows)
        wdf["date"] = pd.to_datetime(wdf["event_start"], unit="s").dt.date.astype(str)
        backtest(wdf, "fill", f"C.  wait 尾盘（rem≤{rm} 仅一侧穿越）")

    # 对照: 定稿 flip（flip 买对侧, won=1-won, fill=flip_fill10s=1-触发侧bid@10s）
    fd = df[df["trigger_bid"].gt(0.73) &
            df["flip_fill10s"].notna() &
            (1 - df["flip_fill10s"]).le(0.66)].copy()
    fd["won"] = 1 - fd["won"]
    backtest(fd, "flip_fill10s", "对照: 定稿 flip（C1>0.73 & +10s≤0.66）")


if __name__ == "__main__":
    main()
