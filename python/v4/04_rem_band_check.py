#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v4 时间腿（rem）分桶检查 —— 回答「240s-160s 入场是否更优」（2026-09-13）。

背景: 用户提出把时间腿从 `rem > 180` 改为 `rem ∈ (160, 240)`。01_backtest_r1.py
已加 `--rem-min/--rem-max`（默认维持历史口径）; 按要求回测后两条口径均变差:
  组合带  n=645 EV +0.538/注 P&L +347U （基线 n=625 EV +0.633 +396U）
  R1 带   n=273 EV +0.689/注 P&L +188U （基线 n=245 EV +1.078 +264U）
本脚本把「其余三腿全过（触发×急跌×浅洞）」的信号按 rem 分桶, 定位差异来源。

结论（08-18~08-31, 14 天）:
  低端 [160,180) 是零/负 EV 段: 组合带 n=196 WR 17.9% EV +0.012（分半 h1
    +0.13/h2 −0.19）; R1 带 n=83 EV −0.337（分半同负）→ 下界取 160 是拖累。
  高端 [240,300) 弱正: 组合带 n=181 EV ≈+0.28（h1 +0.01/h2 +0.60）→ 上界取
    240 削掉的是正 EV 段, 不是垃圾。
  ⇒「240s-160s」两端都削错方向。若目标是抬 EV/注, 该削的是低端:
    180 < rem < 240 → 组合带 n=444 EV +0.777/注 P&L +345U（EV +23%,
    总 P&L −51U, 信号 −29%）; R1 带 n=185 EV +1.169 +216U; R1 双层
    n=142 EV +1.383 日正 13/14。
稳健性警示: 20s 桶的 h1/h2 EV 大面积翻转（如 [200,220) h1 +1.33/h2 +0.35）,
  单桶估计基本是噪声; 仅「低端差」在两条口径 × 分半上方向一致, 相对可信。
  与 09-10 带阈值稳健性同结论——分桶 argmax 是噪声尖峰, 不宜据此直接改引擎,
  留 09-15 OOS（现网纸面记录）裁判。
用法: python 04_rem_band_check.py [--data <事件目录>]
"""
import argparse
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
BINS = [160, 180, 200, 220, 240, 280, 300]   # 低端细（用户关注带）, 高端粗
BAND_LO, BAND_HI = 180, 240                  # 诊断得出的候选带（见 docstring）
BASE_REM_MIN = 180                           # 历史基线时间腿（rem > 180, 勿随 bt.REM_MIN 变）


def load_bt():
    """加载 01_backtest_r1.py（复用 extract / rules / STAKE, 不重跑其 main）。"""
    spec = importlib.util.spec_from_file_location("bt", BASE / "01_backtest_r1.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pl_of(bt, g):
    """与 01 一致的 2U/注 P&L 数组。"""
    return np.where(g["settle_won"] == 1, bt.STAKE / g["fill"] - bt.STAKE, -bt.STAKE)


def bucket_table(bt, df, mask, name):
    """分桶 EV 表: n / WR / EV / P&L（子集 rem > 160, 覆盖用户关心的两端）。"""
    s = df[mask].copy()
    s = s[s["rem"] > BINS[0]]
    s["bucket"] = pd.cut(s["rem"], bins=BINS, right=False)
    print(f"\n=== {name}（rem>{BINS[0]} 子集 n={len(s)}）===")
    print(f"{'rem 区间':>12} {'n':>4} {'WR%':>6} {'EV/注':>8} {'P&L':>8}")
    for b, g in s.groupby("bucket", observed=True):
        p = pl_of(bt, g)
        print(f"{str(b):>12} {len(g):>4} {g.settle_won.mean()*100:>6.1f} "
              f"{p.mean():>+8.3f} {p.sum():>+8.1f}")
    p = pl_of(bt, s)
    print(f"{'合计':>12} {len(s):>4} {s.settle_won.mean()*100:>6.1f} "
          f"{p.mean():>+8.3f} {p.sum():>+8.1f}")
    return s


def half_table(bt, s, name):
    """分桶 × 半样本（h1/h2）: 单桶 EV 是否跨半样本稳定。"""
    print(f"\n=== {name} 分桶 × 半样本 ===")
    print(f"{'rem 区间':>12} | {'h1 n':>5} {'h1 EV':>7} | {'h2 n':>5} {'h2 EV':>7}")
    for b, g in s.groupby("bucket", observed=True):
        cells = []
        for h in ("h1", "h2"):
            gh = g[g["h"] == h]
            cells.append(f"{len(gh):>5} {pl_of(bt, gh).mean():>+7.3f}" if len(gh)
                         else f"{0:>5} {'n/a':>7}")
        print(f"{str(b):>12} | {cells[0]} | {cells[1]}")


def band_vs_base(bt, df, mask, name):
    """候选带 [180,240) 相对基线（rem>180）: 逐日 P&L 与剔除段对比。"""
    s = df[mask]
    base = s[s["rem"] > BASE_REM_MIN]
    band = base[(base["rem"] > BAND_LO) & (base["rem"] < BAND_HI)]
    cut = base[~base.index.isin(band.index)]
    print(f"\n=== {name} 基线与 [{BAND_LO},{BAND_HI}) 逐日 P&L ===")
    print(f"{'日期':>12} {'基线 n':>7} {'基线P&L':>8} {'带内 n':>7} {'带内P&L':>8} "
          f"{'剔除段P&L':>10}")
    for d in sorted(s["date"].unique()):
        b, g = base[base["date"] == d], band[band["date"] == d]
        print(f"{d:>12} {len(b):>7} {pl_of(bt, b).sum():>+8.1f} {len(g):>7} "
              f"{pl_of(bt, g).sum():>+8.1f} "
              f"{pl_of(bt, b[~b.index.isin(g.index)]).sum():>+10.1f}")
    print(f"{'合计':>12} {len(base):>7} {pl_of(bt, base).sum():>+8.1f} {len(band):>7} "
          f"{pl_of(bt, band).sum():>+8.1f} {pl_of(bt, cut).sum():>+10.1f}")


def main():
    ap = argparse.ArgumentParser(description="v4 时间腿 rem 分桶检查")
    ap.add_argument("--data", default=str(BASE.parent.parent / "data" / "btc"))
    args = ap.parse_args()

    bt = load_bt()
    df = bt.extract(args.data)
    bt.REM_MIN, bt.REM_MAX = 0, None    # 放开时间腿, 只留触发×急跌×浅洞
    pure, dual, pureC, dualC = bt.rules(df)

    for name, mask in (("组合带 yes(-0.6,0)+no(-1,0)", pureC), ("R1 双侧 (-0.5,0)", pure)):
        half_table(bt, bucket_table(bt, df, mask, name), name)
    band_vs_base(bt, df, pureC, "组合带")


if __name__ == "__main__":
    main()
