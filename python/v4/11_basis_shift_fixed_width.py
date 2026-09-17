#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
1:1 Basis 平移 + 宽度不变 —— 零拟合参数版本的对照（2026-09-17）。

用户提案（a.md §3 追加）：
  「(-0.6,0)/(-1,0) 本来就是拟合出来的；Basis 会变（负/0/正），但它理论上不该影响
    「砸到 0.2 后翻转」的概率。所以要按 Basis 动态移动阈值区间，但保持带宽不变(~0.5)。」

与 10_dist_s_what_it_measures 的关系：09 的 k 扫描每换一个 k 都**重新拟合了 lo**，
因此没有测过「形状原封不动、只做坐标平移」这一版。本脚本补的就是这个：

  R_A  现行      dist_s ∈ (yes −0.6, 0) / (no −1.0, 0)      ← 拟合自回测期
  R_B  用户提案  dist_t ∈ (yes −0.6, 0) / (no −1.0, 0)      ← 同样数字，1:1 平移、宽度不变
  R_C  半平移    z_0.5  ∈ (yes −0.6, 0) / (no −1.0, 0)      ← k=0.5，宽度不变
  R_R1  R1 旧带   dist_s ∈ (yes −0.5, 0) / (no −0.5, 0)      ← 双侧同宽参照

R_B 是**零拟合参数**的：−0.6/−1.0 是回测期在 dist_s 上定下的数字，原样搬到 dist_t，
搬到纸面期时也不重标定 → 可以当前向预注册规则看。

三块：
  【1】回测期同台（含 h1/h2 分半）
  【2】纸面期前向应用（回测期数字不动；标签由 windows_* 推导，只读相对排序）
  【3】纸面期被挡信号的命运——用户所指的「没信号但翻转了」到底是哪些

用法: python 11_basis_shift_fixed_width.py [--stake 2] [--bt data/btc] [--paper data/v4]
"""
import argparse
import glob
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, BASE / fname)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


r1 = _load("r1", "01_backtest_r1.py")
r8 = _load("r8", "08_dist_t_band_refit.py")

CRASH_MIN = r1.CRASH_MIN
REM_MIN = 180
r1.REM_MAX = None            # 头条口径无上限（01 模块默认 240 是陷阱）
r1.REM_MIN = REM_MIN

#: (名称, 坐标, 带) —— 坐标 None = 不设浅洞腿
RULES = (
    ("R_A 现行 dist_s（拟合）", "dist_s", {"yes": -0.6, "no": -1.0}),
    ("R_B 提案 dist_t（1:1平移）", "dist_t", {"yes": -0.6, "no": -1.0}),
    ("R_C 半平移 k=0.5", "zk0.5", {"yes": -0.6, "no": -1.0}),
    ("R_R1 旧带 dist_s(-0.5)", "dist_s", {"yes": -0.5, "no": -0.5}),
    ("R_0 不设浅洞腿", None, None),
)


def pl_of(s, stake):
    if len(s) == 0:
        return np.zeros(0)
    return np.where(s["settle_won"] == 1, stake / s["fill"] - stake, -stake)


def summ(s, stake):
    if len(s) == 0:
        return (0, 0.0, 0.0, 0.0)
    pl = pl_of(s, stake)
    return (len(s), s["settle_won"].mean(), pl.mean(), pl.sum())


def pick(df, coord, bands, extra=None):
    """按 (坐标, 侧别带) 选样本。coord=None → 只做前两腿。"""
    m = (df["m_45"] >= CRASH_MIN) & (df["rem"] > REM_MIN) & df["dist_s"].notna()
    if coord is not None:
        side_ok = np.zeros(len(df), bool)
        for side, lo in bands.items():
            v = df[coord]
            side_ok |= (df["side"] == side).to_numpy() & v.notna().to_numpy() \
                & (v > lo).to_numpy() & (v < 0).to_numpy()
        m &= side_ok
    m = np.asarray(m).copy()
    if extra is not None:
        m &= extra
    return df[m]


def add_coords(df):
    df = df.copy()
    df["zk0.5"] = df["dist_s"] - 0.5 * (df["dist_s"] - df["dist_t"])
    return df


def main():
    ap = argparse.ArgumentParser(description="1:1 Basis 平移 + 宽度不变")
    ap.add_argument("--bt", default=str(BASE.parent.parent / "data" / "btc"))
    ap.add_argument("--paper", default=str(BASE.parent.parent / "data" / "v4"))
    ap.add_argument("--stake", type=float, default=2.0)
    args = ap.parse_args()
    stake = args.stake

    bt = add_coords(r1.extract(args.bt))
    ndays = bt["date"].nunique()
    bt = bt[bt["dist_s"].notna() & bt["dist_t"].notna()]

    # ================= 【1】回测期 =================
    print("=" * 78)
    print("【1】回测期同台（14 天, 2U/注）—— 形状/数字全部来自 R_A 的原始标定, 只换坐标")
    print("=" * 78)
    print(f"  {'规则':<28}{'n':>5}{'WR':>8}{'EV':>10}{'P&L':>10}{'U/日':>8}")
    for name, coord, bands in RULES:
        s = pick(bt, coord, bands)
        n, wr, ev, p = summ(s, stake)
        print(f"  {name:<28}{n:>5}{wr*100:>7.1f}%{ev:>+10.3f}{p:>+10.1f}{p/ndays:>+8.1f}")

    print(f"\n  分半（h1 / h2）:")
    for name, coord, bands in RULES:
        s = pick(bt, coord, bands)
        out = f"  {name:<28}"
        for h in ("h1", "h2"):
            n, wr, ev, p = summ(s[s["h"] == h], stake)
            out += f"  {h}: n={n:4d} EV {ev:+.3f} P&L {p:+7.1f}U"
        print(out)

    # ================= 【2】纸面期前向 =================
    print("\n" + "=" * 78)
    print("【2】纸面期前向应用（回测期数字原样搬, 不重标定）")
    print("=" * 78)
    r8.STAKE_REF = stake
    pp = r8.load_paper(Path(args.paper))
    if not len(pp):
        print("  无可推导标签的行（缺 windows_*）")
        return
    ok = pp[(pp["ok"] == True) & pp["won"].notna()]  # noqa: E712
    agree = int((ok["settle_won"].astype(bool) == ok["won"]).sum())
    print(f"  标签推导核对: 引擎已结算 {len(ok)} 行, 一致 {agree}"
          f"（{agree/max(1,len(ok))*100:.1f}%）  区间 {pp['date'].min()} ~ {pp['date'].max()}")
    print(f"  ⚠️ 标签是本地推导（windows_* 的 anchor/close）非官方结算 → 只读规则间相对排序")
    if "gate_reason" in pp:
        g = pp["gate_reason"].notna().sum()
        if g:
            pp = pp[pp["gate_reason"].isna()]
            print(f"  已剔除 gate_reason 非空行 {g}（默认口径）")
    pp = add_coords(pp)
    pp["pl"] = np.where(pp["settle_won"] == 1, stake / pp["fill"] - stake, -stake)
    nd = pp["date"].nunique()
    print(f"\n  {'规则':<28}{'n':>5}{'WR':>8}{'EV':>10}{'P&L':>10}{'U/日':>8}")
    for name, coord, bands in RULES:
        s = pick(pp, coord, bands)
        if not len(s):
            print(f"  {name:<28}{0:>5}")
            continue
        n, wr, ev, p = summ(s, stake)
        print(f"  {name:<28}{n:>5}{wr*100:>7.1f}%{ev:>+10.3f}{p:>+10.1f}{p/nd:>+8.1f}")

    # ================= 【3】纸面期被挡信号的命运 =================
    print("\n" + "=" * 78)
    print("【3】纸面期「被浅洞带挡掉」的信号去了哪里（m_45≥%.2f × rem>%d 已过）" % (CRASH_MIN, REM_MIN))
    print("=" * 78)
    base = pick(pp, None, None)
    print(f"  触发三腿齐全（急跌×时间）总体: n={len(base)}  WR {base.settle_won.mean()*100:.1f}%"
          f"  EV {base['pl'].mean():+.3f}  P&L {base['pl'].sum():+.1f}U")
    inA = pick(pp, "dist_s", {"yes": -0.6, "no": -1.0})
    inB = pick(pp, "dist_t", {"yes": -0.6, "no": -1.0})
    setA, setB = set(inA.index), set(inB.index)
    print(f"  R_A 收进 {len(setA)}    R_B 收进 {len(setB)}    "
          f"R_B 新增 {len(setB-setA)}    R_A 独有 {len(setA-setB)}")
    for tag, idx in (("R_B 新增（被 R_A 挡掉但 R_B 收进）", setB - setA),
                     ("R_A 独有（R_B 挡掉）", setA - setB)):
        s = pp.loc[sorted(idx)]
        if len(s):
            n, wr, ev, p = summ(s, stake)
            print(f"    {tag:<34} n={n:4d}  WR {wr*100:5.1f}%  EV {ev:+.3f}  P&L {p:+7.1f}U")
        else:
            print(f"    {tag:<34} n=   0")
    # 被 R_A 挡掉的全部（含两侧超界）
    outA = base.loc[sorted(set(base.index) - setA)]
    if len(outA):
        n, wr, ev, p = summ(outA, stake)
        up = outA[outA["dist_s"] >= 0]
        lo = outA[outA["dist_s"] <= np.where(outA["side"] == "yes", -0.6, -1.0)]
        print(f"\n  R_A 挡掉的总体 n={n}  WR {wr*100:.1f}%  EV {ev:+.3f}  P&L {p:+.1f}U"
              f"   ← 若为负则挡对了")
        print(f"    其中 dist_s ≥ 0（上界挡）: n={len(up):4d}  EV {pl_of(up, stake).mean() if len(up) else 0:+.3f}"
              f"  P&L {pl_of(up, stake).sum():+.1f}U")
        print(f"    其中 dist_s ≤ lo（下界挡）: n={len(lo):4d}  EV {pl_of(lo, stake).mean() if len(lo) else 0:+.3f}"
              f"  P&L {pl_of(lo, stake).sum():+.1f}U")

    print("\n  逐日 basis 水平 × R_A 挡单率:")
    print(f"    {'日期':<12}{'三腿n':>7}{'R_A n':>7}{'挡单率':>9}{'basis$中位':>12}{'dist_s中位':>11}")
    for d, s in base.groupby("date"):
        a = len(setA & set(s.index))
        bz = s["spot"] - s["twap_price"]          # 原始价差（美元），符号未按狗侧翻转
        print(f"    {d:<12}{len(s):>7}{a:>7}{(1-a/max(len(s),1))*100:>8.1f}%"
              f"{bz.median():>+12.1f}{s['dist_s'].median():>+11.2f}")


if __name__ == "__main__":
    main()
