#!/usr/bin/env python3
"""
v3 flip 信号扩量 —— 「自信崩溃家族」二维参数空间扫描。

背景: 定稿 C1/C2（trigger_bid>0.73 & +10s post_bid≤0.66）14 天仅 140 注
（~10 注/天, EV +0.107/股）。本脚本探索更大量的同族变体:
  * C1 深度阈值 × 确认时点（+5/+10/+15s）× 崩溃阈值 的三维网格
  * 崩溃形状特征（post_dip 崩多深 / cross_speed_s 拉多快）分桶
目标: 找 n 更大（≥250）且全量/样本外 EV>0 的变体, 或确认定稿已是最优。

评估协议（与 06_backtest 一致）:
  * flip 买对侧, fill = 对侧 ask@确认时刻 = 1 - 触发侧 bid@确认时刻（flip_fill{ds}s）
  * P&L: 2U/笔, shares=2/fill, 赢→shares-2, 输→-2
  * 每事件一注（首个穿越）, 无 fallback
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from v2.lib import load_events, extract_cross

REPO = Path(__file__).resolve().parent.parent.parent
DATA = str(REPO / "data" / "btc")
CONFIRM_S = (2, 5, 10, 15)
STAKE = 2.0


def wilson(wr, n):
    z = 1.96
    denom = 1 + z**2 / n
    p = (wr + z**2 / (2 * n)) / denom
    h = z * np.sqrt(wr * (1 - wr) / n + z**2 / (4 * n**2)) / denom
    return p - h, p + h


def load():
    events = load_events(DATA)
    obs = extract_cross(events, "outcome", confirm_s=CONFIRM_S)
    rows = []
    for o in obs:
        r = dict(o)
        # 崩溃形状: post_dip = 确认窗口内触发侧 bid 最低点（各时点 min）
        r["post_dip"] = min(1 - r[f"flip_fill{ds}s"] for ds in CONFIRM_S
                            if r.get(f"flip_fill{ds}s"))
        rows.append(r)
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["event_start"], unit="s").dt.date.astype(str)
    return df


def ev_of(df, ds, c1, c2v):
    """C1 深度 & 确认时点 & 崩溃阈值 → (n, WR, fill, EV)（全量 14 天）。"""
    d = df[df["trigger_bid"].gt(c1) &
           df[f"flip_fill{ds}s"].notna() &
           (1 - df[f"flip_fill{ds}s"]).le(c2v)].dropna(subset=[f"flip_fill{ds}s"])
    if len(d) < 20:
        return None
    fill = d[f"flip_fill{ds}s"].mean()
    won = d["won"]  # follow_won; flip 买对侧 → flip_won = 1 - won
    flip_won = (1 - won).mean()
    ev = flip_won - fill
    return len(d), flip_won, fill, ev, d


def grid(df):
    print("=" * 78)
    print(f"网格扫描: C1 深度 × 确认时点 × 崩溃阈值（全量 14 天, "
          f"基准 = C1>0.73 & +10s ≤0.66: n=140 WR 50.7% EV +0.107）")
    print("=" * 78)
    best = []
    for c1 in (0.70, 0.72, 0.73, 0.75, 0.78, 0.80):
        for ds in (5, 10, 15):
            for c2v in (0.60, 0.62, 0.64, 0.66, 0.68, 0.70):
                r = ev_of(df, ds, c1, c2v)
                if r is None:
                    continue
                n, wr, fill, ev, d = r
                if n >= 150 and ev > 0:
                    best.append((ev, n, c1, ds, c2v, wr, fill))
                if c2v in (0.66,) or (n >= 150 and ev > 0):
                    print(f"  C1>{c1:.2f} +{ds:2d}s ≤{c2v:.2f}: "
                          f"n={n:4d} WR {wr*100:5.1f}% fill {fill:.3f} EV {ev:+.4f}")
    best.sort(reverse=True)
    print("\n候选（n≥150 且 EV>0, 按 EV 排序 top 10）:")
    for ev, n, c1, ds, c2v, wr, fill in best[:10]:
        print(f"  C1>{c1:.2f} +{ds:2d}s ≤{c2v:.2f}: n={n:4d} WR {wr*100:5.1f}% "
              f"fill {fill:.3f} EV {ev:+.4f}")
    return best


def oos_check(df, c1, ds, c2v):
    """样本外体检: train≤08-28 选 / test2d / 长窗 / 双半 / 逐日。"""
    d = df[df["trigger_bid"].gt(c1) &
           df[f"flip_fill{ds}s"].notna() &
           (1 - df[f"flip_fill{ds}s"]).le(c2v)].dropna(subset=[f"flip_fill{ds}s"])
    tr = d[d["date"].le("2026-08-28")]
    te = d[d["date"].isin(("2026-08-29", "2026-08-30"))]
    te4 = d[d["date"].ge("2026-08-27") & d["date"].le("2026-08-30")]
    print(f"\n=== 体检: C1>{c1:.2f} +{ds}s ≤{c2v:.2f} ===")
    for nm, dd in (("全量", d), ("train", tr), ("test2d", te), ("长窗4d", te4)):
        if len(dd) == 0:
            print(f"  {nm}: 空"); continue
        won = (1 - dd["won"]).mean()
        fill = dd[f"flip_fill{ds}s"].mean()
        print(f"  {nm:<7s} n={len(dd):4d} WR {won*100:5.1f}% "
              f"fill {fill:.3f} EV {won-fill:+.4f} P&L {(won-fill)*STAKE/fill:+.2f}")
    tdates = sorted(d["date"].unique())
    hd = [x for x in tdates if x <= "2026-08-28"]
    m1 = int(len(hd) * 0.6)
    for nm, dd in (("H1", d[d["date"].isin(hd[:m1])]), ("H2", d[d["date"].isin(hd[m1:])])):
        won = (1 - dd["won"]).mean()
        print(f"  {nm}: n={len(dd):3d} WR {won*100:5.1f}% "
              f"EV {won - dd[f'flip_fill{ds}s'].mean():+.4f}")
    day_rows = []
    for dd_, g in d.groupby("date"):
        won = (1 - g["won"]).mean()
        day_rows.append((dd_, len(g), won, won - g[f"flip_fill{ds}s"].mean()))
    pos = sum(1 for _, _, _, e in day_rows if e > 0)
    print(f"  逐日 EV 正: {pos}/{len(day_rows)}  "
          + "  ".join(f"{dd[5:]}: {w*100:.0f}%/{e:+.2f}" for dd, n, w, e in day_rows))


def shape(df, c1, c2v, ds=10):
    """崩溃形状分桶（在 C1/C2 信号内）: post_dip 崩多深 / cross_speed_s 拉多快。"""
    d = df[df["trigger_bid"].gt(c1) & (1 - df[f"flip_fill{ds}s"]).le(c2v)]
    base_won = (1 - d["won"]).mean()
    print(f"\n=== 崩溃形状解剖（C1>{c1:.2f} & +{ds}s≤{c2v:.2f}, 基线 WR {base_won*100:.1f}%, n={len(d)}) ===")
    for feat in ("post_dip", "cross_speed_s"):
        qs = [d[feat].quantile(q) for q in (0.2, 0.4, 0.6, 0.8)]
        bounds = [-np.inf] + qs + [np.inf]
        print(f"  ◆ {feat}")
        for k in range(5):
            lo, hi = bounds[k], bounds[k + 1]
            if k == 0:
                m = d[feat].between(lo, hi, inclusive="neither")
            elif k < 4:
                m = d[feat].gt(lo) & d[feat].le(hi)
            else:
                m = d[feat].gt(lo)
            dd = d[m]
            if len(dd) < 20:
                continue
            won = (1 - dd["won"]).mean()
            fill = dd[f"flip_fill{ds}s"].mean()
            print(f"    Q{k+1} ({lo:.2f},{hi:.2f}] n={len(dd):4d} WR {won*100:5.1f}% "
                  f"fill {fill:.3f} EV {won-fill:+.4f}")


def main():
    df = load()
    print(f"数据: {DATA} | 观测 {len(df)}")
    best = grid(df)
    if not best:
        print("\n无 n≥150 且 EV>0 的网格变体——定稿 C1/C2 即最优区")
        return
    # 对 top 3 候选做体检
    for _, n, c1, ds, c2v, wr, fill in best[:3]:
        oos_check(df, c1, ds, c2v)
    # 定稿基准也体检（对照）
    oos_check(df, 0.73, 10, 0.66)
    # 崩溃形状（定稿参数）
    shape(df, 0.73, 0.66)


if __name__ == "__main__":
    main()
