#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v5 TWAP-pred 增强探索（doc §十.3 + §五 辅助过滤）—— 纯 CSV 统计, 不重扫数据。

输入: python/v5/data/trades_twap_pred.csv（01 全网格逐笔明细, 每行含入场 tick
的 d_pred/v/成交流 10s·120s/盘口 depth 特征列）; 规则/口径见
docs/twap_pred_plan_2026-09-07.md 与 01 脚本头。

三节:
  A 口径自检      M=0 各 look 的 n/WR/EV 与 01 网格表 M=0 行逐一核对
  B 预测校准审计  corr(dir·D_pred, dir·actual_d) 幅度校准 + |D_pred| 四分位
                 WR 单调性 + rem/|v| 桶/侧别 WR 表（M=0 行入场条件使符号一致率
                 ≡ WR 无独立信息, 不列）—— 检视线性假设质量
                 （01 结论为 v1 全网格近似盈亏平衡, 本节回答"预测本身准不准"）
  C 辅助特征条件  M=0 基线上加条件看 WR/EV 增量（只作 v2 P̂ 升级线索, 不进头条）:
      OF 同向      10s 净成交流 (buy−sell) 符号 = 预测方向 dir
      反向大单     dir 方向成交流相对 120s 均值切片 spike ×θ
                   （简化口径: 数据为秒级聚合 vol, 无逐笔单; "反向"指相对
                     当前 TWAP 位置(spot 已偏)方向的放量, doc §五语义近似）
      盘口失衡     bid5/(bid5+ask5) 偏 0.5 方向 = dir（depth 全 0 剔除）
      组合         OF + 反向大单 + 盘口失衡 同时成立

用法:
  python 02_features_aux.py [--csv ...] [--stake 2] [--theta 3] [--look 15]
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
STAKE_DEF = 2.0
THETA_DEF = 3.0        # 反向大单 spike 倍数（10s 流量 / 120s 均值切片）
LOOK_DEF = 15          # 校准审计的自然默认 look


def pl(s, stake):
    return np.where(s["settle_won"] == 1, stake / s["fill"] - stake, -stake)


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    w = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - w) / d, (c + w) / d)


def stat_line(name, s, stake):
    if len(s) == 0:
        print(f"  {name}: n=0")
        return
    k = int(s["settle_won"].sum())
    lo, hi = wilson(k, len(s))
    p = pl(s, stake)
    print(f"  {name}: n={len(s):5d}  WR {k/len(s)*100:5.1f}% "
          f"[{lo*100:.1f},{hi*100:.1f}]  fill {s['fill'].mean():.3f}  "
          f"EV {p.mean():+.3f}U/注  P&L {p.sum():+8.1f}U")


def main():
    ap = argparse.ArgumentParser(description="v5 TWAP-pred 增强探索")
    ap.add_argument("--csv", default=str(BASE / "data" / "trades_twap_pred.csv"))
    ap.add_argument("--stake", type=float, default=STAKE_DEF)
    ap.add_argument("--theta", type=float, default=THETA_DEF)
    ap.add_argument("--look", type=int, default=LOOK_DEF)
    args = ap.parse_args()

    df = pd.read_csv(args.csv)
    print(f"明细 {args.csv}: {len(df)} 行 | stake={args.stake}U | θ={args.theta:.0f}\n")
    # 方向: dir=+1 买 yes(预测 close>open) / −1 买 no
    df["dir"] = np.where(df["side"] == "yes", 1.0, -1.0)
    d0 = df[df["margin"] == 0].copy()      # 无安全边际基线
    # 特征
    d0["net10"] = d0["buy10"] - d0["sell10"]
    denom = d0["buy120"].replace(0.0, np.nan)   # 反向大单 spike: 10s / 均值切片
    d0["buy_spike"] = 12.0 * d0["buy10"] / denom
    d0["sell_spike"] = 12.0 * d0["sell10"] / d0["sell120"].replace(0.0, np.nan)
    dsum = d0["depth_bid5"] + d0["depth_ask5"]
    d0["imb"] = d0["depth_bid5"] / dsum.replace(0.0, np.nan)  # 全 0 → NaN 剔除

    # ---------- A: 口径自检 ----------
    print("A 口径自检（M=0 基线, 应逐行等于 01 网格表 M=0 行）")
    for L, s in d0.groupby("look"):
        stat_line(f"look={L}", s, args.stake)
    print()

    # ---------- B: 预测校准审计（L=15, M=0） ----------
    s = d0[d0["look"] == args.look]
    dirs = s["dir"].to_numpy()
    dad = (s["actual_d"] * dirs).to_numpy()   # 极性化真实穿越: >0 = 该侧赢
    deff = (s["d_pred"] * dirs).to_numpy()    # 极性化预测穿越
    # 入场方向条件（D_pred>+M 买 yes / D_pred<−M 买 no）保证 dir·D_pred>0
    # 恒成立 ⇒ M=0 行「符号一致率」≡ WR, 无独立校准信息; 校准量改取:
    # ① corr(极性化幅度): 线性假设下 |D_pred| 应随真实穿越幅度线性放大
    # ② |deff| 四分位 WR 单调性: 预测幅度若含信息, 幅度越大该侧胜率越高
    corr = np.corrcoef(deff, dad)[0, 1]
    print(f"B 预测校准审计（look={args.look}, M=0, n={len(s)}）")
    print(f"  corr(dir·D_pred, dir·actual_d) = {corr:+.3f}"
          f"（~0 = 预测幅度不随真实穿越校准; actual_d 符号与 settle_won"
          f" 逐行一致 ⇒ 下方 WR 即方向命中率）")
    ab = np.abs(deff)
    try:
        qb = pd.qcut(ab, 4, duplicates="drop")
        print("  |D_pred| 四分位 WR（预测幅度若含信息应单调上升）:")
        for b, g in s.groupby(qb, observed=True):
            k = int(g["settle_won"].sum())
            print(f"    |D| {b.left:7.3f}~{b.right:7.3f}  n={len(g):5d}"
                  f"  WR {k/len(g)*100:5.1f}%")
    except ValueError:
        pass
    s2 = s.copy()
    s2["rem_b"] = np.where(s2["rem"] >= 180, "rem≥180",
                           np.where(s2["rem"] >= 60, "rem60..179", "rem<60"))
    av = s2["v"].abs()
    s2["v_b"] = np.where(av == 0, "|v|=0", np.where(av <= 0.05, "(0,0.05]",
                         np.where(av <= 0.2, "(0.05,0.2]", ">0.2")))
    for rb in ("rem≥180", "rem60..179", "rem<60"):
        g = s2[s2["rem_b"] == rb]
        if not len(g):
            continue
        kk = int(g["settle_won"].sum())
        lo, hi = wilson(kk, len(g))
        cc = np.corrcoef(g["d_pred"] * g["dir"], g["actual_d"] * g["dir"])[0, 1]
        print(f"  [{rb}] n={len(g):5d}  WR {kk/len(g)*100:5.1f}% "
              f"[{lo*100:.1f},{hi*100:.1f}]  corr {cc:+.3f}")
    for vb in ("|v|=0", "(0,0.05]", "(0.05,0.2]", ">0.2"):
        g = s2[s2["v_b"] == vb]
        if not len(g):
            continue
        kk = int(g["settle_won"].sum())
        lo, hi = wilson(kk, len(g))
        print(f"  |v| {vb:>9} n={len(g):5d}  WR {kk/len(g)*100:5.1f}% "
              f"[{lo*100:.1f},{hi*100:.1f}]")
    print()

    # ---------- C: 辅助特征条件评估（M=0, 逐 look） ----------
    print(f"C 辅助特征条件评估（M=0 基线; 反向大单 θ={args.theta:.0f};"
          f" 探索性, 仅作 v2 线索）")
    for L, base in d0.groupby("look"):
        m_of = (np.sign(base["net10"]) == base["dir"]) & (base["net10"] != 0)
        spike = np.where(base["dir"] > 0, base["buy_spike"], base["sell_spike"])
        m_big = (spike > args.theta) & (np.isfinite(spike))
        m_imb = (np.sign(base["imb"] - 0.5) == base["dir"]) & \
            np.isfinite(base["imb"])
        stat_line(f"look={L} 基线 M=0", base, args.stake)
        stat_line("     +OF 同向(10s净量=dir)", base[m_of], args.stake)
        stat_line(f"    +反向大单(spike>θ)", base[m_big], args.stake)
        stat_line("    +盘口失衡(bid5比=dir)", base[m_imb], args.stake)
        stat_line("    +OF&大单&盘口 组合", base[m_of & m_big & m_imb], args.stake)
    print()
    print("（口径注: 反向大单为秒级 buy/sell_vol 聚合的近似——10s 流量超 120s "
          "均值切片 θ 倍; 非逐笔大单单笔检测。depth 为入场 tick 当刻快照。）")


if __name__ == "__main__":
    main()
