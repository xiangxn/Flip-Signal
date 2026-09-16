#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v4 纸面数据规则回放 —— 在引擎落盘的 touches_*.jsonl 上重放各规则的入场与结算
（2026-09-13）。

用途: 回答「同一段行情里, R1 双层 与 引擎现行组合带 各自打多少」——09-15 OOS
复验的纸面侧口径对照。引擎现行 gate = 组合版**单层**（pureC, dist_s 侧别带）,
本脚本用记录里的 m_45/dist_s/dist_t/rem 重判, 四条规则同台:

  R1 纯现货  dist_s ∈ (−0.5,0)                    （01 对照第一条）
  R1 双层    + dist_t ∈ (−0.5,0)                  （01 对照第二条, 本次重点）
  组合 纯现货 dist_s 侧别带 yes(−0.6,0)/no(−1,0)  （引擎现行 gate）
  组合 双层  + dist_t 侧别带
  引擎实际   ok 列（= 引擎真实执行 + 结算, 作基线参照）

可评估性前提（重要）: 引擎只对 ok 行注册结算轮询, won/pnl 仅 ok 行回填。因此只有
「⊆ 引擎 gate」的规则能无损回放。R1 双侧 (−0.5,0) 比组合带每侧都窄 ⇒ R1 两条 ⊆
组合单层 ⊆ 组合双层, 前提成立; 脚本对「选中但无结算」的行计数并告警（若非 0,
说明该规则越出引擎 gate, 结论需打折）。

用法: python 05_paper_rule_replay.py [--data <dir>] [--stake 2]
"""
import argparse
import json
from pathlib import Path
from math import sqrt

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent

STAKE = 2.0
CRASH_MIN = 0.40
REM_MIN = 180
BAND = (-0.5, 0.0)      # R1 对照组（双侧共用, 同 01）
BAND_YC = (-0.6, 0.0)   # 组合版 yes 带（引擎现行, 同 01 文档口径）
BAND_NO = (-1.0, 0.0)   # 组合版 no 带


def load_records(data_dir):
    """读 touches_*.jsonl → DataFrame（按 ts 排序, 保持时间序）。"""
    rows = []
    for f in sorted(Path(data_dir).glob("touches_*.jsonl")):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    print(f"⚠️  {f.name}: 行解析失败跳过: {line[:80]}")
                    continue
                if r.get("event_type") == "touch":
                    rows.append(r)
    df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    df["date"] = df["date"].astype(str)
    for c in ("m_45", "dist_s", "dist_t", "rem", "fill"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["won"] = pd.to_numeric(df["won"], errors="coerce")
    df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce")
    # 半样本: 按时序把日期对半切
    dpos = pd.factorize(df["date"])[0]
    half = len(np.unique(df["date"])) // 2
    df["h"] = np.where(dpos < half, "h1", "h2")
    return df


def shallow(v, band):
    return (v > band[0]) & (v < band[1])   # NaN → False（与 01 同: 缺失不入选）


def masks(df):
    """四条规则掩码（镜像 01 rules, 时间腿 rem > 180）+ 引擎实际 gate（ok 列）。"""
    crash = df["m_45"] >= CRASH_MIN
    rem_ok = df["rem"] > REM_MIN
    yes_s = df["side"] == "yes"
    pure = crash & rem_ok & shallow(df["dist_s"], BAND)
    dual = pure & shallow(df["dist_t"], BAND)
    pureC = crash & rem_ok & ((yes_s & shallow(df["dist_s"], BAND_YC)) |
                              (~yes_s & shallow(df["dist_s"], BAND_NO)))
    dualC = pureC & ((yes_s & shallow(df["dist_t"], BAND_YC)) |
                     (~yes_s & shallow(df["dist_t"], BAND_NO)))
    ok = df["ok"].fillna(False).astype(bool)
    return [("R1 纯现货", pure), ("R1 双层", dual),
            ("组合 纯现货", pureC), ("组合 双层", dualC), ("引擎实际执行(ok)", ok)]


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    w = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - w) / d, (c + w) / d)


def report(name, s, ndays, n_stale):
    """n_stale = 基线以外多出的未结算行数: >0 才说明该规则越出了引擎 gate。"""
    if len(s) == 0:
        print(f"{name}: n=0\n")
        return
    settled = s[s["won"].notna()]
    k = int(settled["won"].sum())
    lo, hi = wilson(k, len(settled))
    f = settled["fill"].mean()
    pl = np.where(settled["won"] == 1, STAKE / settled["fill"] - STAKE, -STAKE)
    nday_pos = int(sum(
        np.where(g["won"] == 1, STAKE / g["fill"] - STAKE, -STAKE).sum() > 0
        for _, g in settled.groupby("date")))
    flag = f"  ⚠️ 多出 {n_stale} 行未结算（越出引擎 gate, 结论打折）" if n_stale else ""
    print(f"{name}{flag}")
    print(f"  信号量: n={len(settled)}（{len(settled)/ndays:.1f}/日）  "
          f"side {settled.side.value_counts().to_dict()}")
    print(f"  胜率:   {k/len(settled)*100:.1f}% [{lo*100:.1f},{hi*100:.1f}]  "
          f"fill {f:.3f}  (盈亏平衡 {f*100:.1f}%)")
    print(f"  收益:   EV {k/len(settled)-f:+.4f}/股 ≈ {pl.mean():+.3f}U/注  "
          f"P&L {pl.sum():+.0f}U/{ndays}天 (≈ {pl.sum()/ndays:+.1f}U/日)  "
          f"日正 {nday_pos}/{settled['date'].nunique()}")
    for h, nm in (("h1", "前半"), ("h2", "后半")):
        g = settled[settled["h"] == h]
        if len(g):
            plg = np.where(g["won"] == 1, STAKE / g["fill"] - STAKE, -STAKE)
            print(f"    {nm} {h}: n={len(g):3d}  WR {g.won.mean()*100:5.1f}%  "
                  f"EV {plg.mean():+.3f}U/注  P&L {plg.sum():+6.1f}U")
    print("  逐日:")
    for d, g in sorted(settled.groupby("date")):
        plg = np.where(g["won"] == 1, STAKE / g["fill"] - STAKE, -STAKE)
        print(f"    {d}: n={len(g):3d}  WR {g.won.mean()*100:5.1f}%  "
              f"P&L {plg.sum():+6.1f}U")
    print()


def main():
    global STAKE
    ap = argparse.ArgumentParser(description="v4 纸面规则回放（R1 双层 vs 组合带）")
    ap.add_argument("--data", default=str(BASE.parent.parent / "data" / "v4"))
    ap.add_argument("--stake", type=float, default=STAKE)
    args = ap.parse_args()
    STAKE = args.stake

    df = load_records(args.data)
    if len(df) == 0:
        print(f"{args.data}: 无记录"); return
    ndays = df["date"].nunique()
    print(f"纸面记录 {args.data}: {len(df)} 观测 / {ndays} 天 "
          f"({df['date'].min()} ~ {df['date'].max()})  时间腿 rem > {REM_MIN}\n")

    rules = masks(df)
    # 基线未结算数 = 引擎自身都还没结算的信号（最新窗口, 属正常）;
    # 某规则多出未结算行才等于「该规则选中了引擎没执行的 tick」= 越出 gate。
    base_stale = int(df[df["ok"].fillna(False).astype(bool)]["won"].isna().sum())
    for name, m in rules:
        s = df[m]
        n_stale = max(0, int(s["won"].isna().sum()) - base_stale)
        report(name, s, ndays, n_stale)

    # 逐日并排: R1 双层 vs 引擎实际执行（同段行情直接对比）
    d_dual = df[rules[1][1]]
    d_live = df[rules[4][1]]
    print("=== 逐日并排: R1 双层 vs 引擎实际执行(ok) ===")
    print(f"{'日期':>12} {'R1双 n':>7} {'R1双 P&L':>9} {'引擎 n':>7} {'引擎 P&L':>9}")
    for d in sorted(df["date"].unique()):
        cells = []
        for s in (d_dual, d_live):
            g = s[(s["date"] == d) & s["won"].notna()]
            cells.append((len(g),
                          np.where(g["won"] == 1, STAKE / g["fill"] - STAKE, -STAKE).sum()
                          if len(g) else 0.0))
        print(f"{d:>12} {cells[0][0]:>7} {cells[0][1]:>+9.1f} "
              f"{cells[1][0]:>7} {cells[1][1]:>+9.1f}")
    print()


if __name__ == "__main__":
    main()
