#!/usr/bin/env python3
"""
v3 flip 深度崩溃变体 —— C1 自信深度 × C2 崩溃深度 的加强版体检。

网格扫描发现（10_flip_family）: 崩得越深越赚——
  定稿 C1>0.73 & +10s≤0.66: n=140 WR 50.7% EV +0.107
  深度 C1>0.73 & +10s≤0.56: n= 26 WR 69.2% EV +0.171
  深度 C1>0.73 & +10s≤0.54: n= 16 WR 81.2% EV +0.242
且 C1 自信深度是必要（C1>0.70 的深度崩溃不赚）。

本脚本: 对深度崩溃候选做完整体检（全量/train/test2d/长窗4d/双半/逐日），
以及 C1×C2 连续面扫描找甜区。
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


def load():
    events = load_events(DATA)
    obs = extract_cross(events, "outcome", confirm_s=(2, 5, 10, 15))
    df = pd.DataFrame(obs)
    df["date"] = pd.to_datetime(df["event_start"], unit="s").dt.date.astype(str)
    return df


def sig(df, c1, c2v):
    return df[df["trigger_bid"].gt(c1) & df["flip_fill10s"].notna() &
              (1 - df["flip_fill10s"]).le(c2v)].dropna(subset=["flip_fill10s"])


def oos_check(df, c1, c2v, name):
    d = sig(df, c1, c2v)
    print(f"\n=== 深度崩溃: {name} (C1>{c1:.2f} & +10s≤{c2v:.2f}) ===")
    tr = d[d["date"].le("2026-08-28")]
    te = d[d["date"].isin(("2026-08-29", "2026-08-30"))]
    te4 = d[d["date"].ge("2026-08-27") & d["date"].le("2026-08-30")]
    for nm, dd in (("全量", d), ("train", tr), ("test2d", te), ("长窗4d", te4)):
        if len(dd) == 0:
            print(f"  {nm}: 空"); continue
        won = (1 - dd["won"]).mean()
        fill = dd["flip_fill10s"].mean()
        pl = (won - fill) * STAKE / fill
        print(f"  {nm:<7s} n={len(dd):4d} WR {won*100:5.1f}% "
              f"fill {fill:.3f} EV {won-fill:+.4f} P&L/2U {pl:+.2f}")
    tdates = sorted(d["date"].unique())
    hd = [x for x in tdates if x <= "2026-08-28"]
    m1 = int(len(hd) * 0.6)
    for nm, dd in (("H1", d[d["date"].isin(hd[:m1])]), ("H2", d[d["date"].isin(hd[m1:])])):
        won = (1 - dd["won"]).mean()
        print(f"  {nm}: n={len(dd):3d} WR {won*100:5.1f}% "
              f"EV {won - dd['flip_fill10s'].mean():+.4f}")
    day_rows = []
    for dd_, g in d.groupby("date"):
        won = (1 - g["won"]).mean()
        day_rows.append((dd_, len(g), won, won - g["flip_fill10s"].mean()))
    pos = sum(1 for _, _, _, e in day_rows if e > 0)
    print(f"  逐日 EV 正: {pos}/{len(day_rows)}  "
          + "  ".join(f"{dd[5:]}: {w*100:.0f}%/{e:+.2f}" for dd, n, w, e in day_rows))


def surface(df):
    print("\n" + "=" * 78)
    print("C1 × C2 连续面（全量 14 天, 每格 n≥20 才显示）")
    print("=" * 78)
    print(f"{'C1\\C2':>7s} " + " ".join(f"{v:>7.2f}" for v in (0.52, 0.54, 0.56, 0.58, 0.60, 0.62, 0.66)))
    for c1 in (0.71, 0.72, 0.73, 0.74, 0.75):
        row = f"{c1:>7.2f} "
        for c2v in (0.52, 0.54, 0.56, 0.58, 0.60, 0.62, 0.66):
            d = sig(df, c1, c2v)
            if len(d) < 20:
                row += "     ·  "
            else:
                won = (1 - d["won"]).mean()
                ev = won - d["flip_fill10s"].mean()
                row += f"{ev:>+7.3f} "
        print(row)


def main():
    df = load()
    print(f"数据: {DATA} | 观测 {len(df)}")
    # 定稿对照
    oos_check(df, 0.73, 0.66, "定稿对照")
    # 深度崩溃候选
    for c1, c2v, nm in ((0.73, 0.60, "深崩 A"), (0.73, 0.58, "深崩 B"),
                        (0.73, 0.56, "深崩 C"), (0.73, 0.54, "深崩 D"),
                        (0.74, 0.58, "深崩 E")):
        oos_check(df, c1, c2v, nm)
    surface(df)


if __name__ == "__main__":
    main()
