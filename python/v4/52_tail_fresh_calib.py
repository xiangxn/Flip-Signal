#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全新链 vs 文档 pin 的口径差异定位（诊断用，可弃）。

文档 pin（data/events, 现行链, 2U, 3803 窗）: n=1959 / WR 96.68% / 毛 +12.29U
本实现（3806 窗, 严格连续 σ）        : n=1818 / WR 96.92% / 毛 +20.21U
⇒ 逐变体归因，找出 141 笔差异的来源。
"""
from __future__ import annotations

import importlib.util
import os
import sys

import numpy as np

_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("fresh51", os.path.join(_here, "51_tail_fresh_chain.py"))
C = importlib.util.module_from_spec(_spec)
sys.modules["fresh51"] = C          # dataclass 需要模块已注册
_spec.loader.exec_module(C)

SIGMA_N, SIGMA_MIN = 18, 3


def sd_index_based(ws: list[dict]) -> np.ndarray:
    """前 ≤18 个**存在**的窗（按 start 排序，不要求时间连续）的 |close−open| 均值。"""
    ws = sorted(ws, key=lambda w: w["start"])
    mv = [abs(w["close"] - w["anchor"]) for w in ws]
    out = np.zeros(len(ws))
    for i, w in enumerate(ws):
        lo = max(0, i - SIGMA_N)
        blk = mv[lo:i]
        out[i] = float(np.mean(blk)) if len(blk) >= SIGMA_MIN else 0.0
    return out


def report(tag: str, panel, e, skip):
    n = int(e.fired.sum())
    w = int(e.won.sum())
    print(f"  {tag:38s} 跳{int(skip.sum()):4d}  n={n:5d}  WR={100.0*w/max(n,1):6.2f}%  "
          f"毛{e.pnl.sum():+7.2f}U  净{e.pnl_net.sum():+7.2f}U")


def main() -> int:
    ws = C.load()
    panel, skip = C.build_panel(ws)
    rules = C.Rules()

    print("【变体归因】目标 pin: 跳?  n=1959  WR 96.68%  毛 +12.29U")
    report("A 基线（严格连续 σ）", panel, C.simulate(panel, rules, skip), skip)

    # ---- B: σ 按事件索引（前 18 个存在的窗，不要求连续）
    sd_idx = sd_index_based(ws)
    panel_idx = C.build_panel(ws)[0]
    panel_idx.sd = sd_idx
    skip_idx = (panel_idx.anchor <= 0) | (sd_idx <= 0)
    report("B σ 按事件索引（不要求连续）", panel_idx, C.simulate(panel_idx, rules, skip_idx), skip_idx)

    # 在 B 宇宙上再开结构开关
    e = C.simulate(panel_idx, rules, skip_idx, listen_needs_t60=False)
    report("C = B + 监听不要求 rem≤60 的 tick", panel_idx, e, skip_idx)

    e = C.simulate(panel_idx, rules, skip_idx, listen_includes_t60=True)
    report("D = B + 监听含 T60 判定 tick", panel_idx, e, skip_idx)

    e = C.simulate(panel_idx, rules, skip_idx, t150_strict=False)
    report("E = B + T150 价格腿 ≥0.80", panel_idx, e, skip_idx)

    e = C.simulate(panel_idx, rules, skip_idx, listen_needs_t60=False, t150_strict=False)
    report("F = B + C + E", panel_idx, e, skip_idx)

    # 差多少窗在 σ 上：两口径的 sd 差异分布
    d = np.abs(sd_idx - panel.sd)
    m = (sd_idx > 0) & (panel.sd > 0)
    print(f"\nσ 两口径：同时可用 {int(m.sum())} 窗，|Δ| 中位 {np.median(d[m]):.3f} 美元，"
          f"最大 {d[m].max():.3f}；仅索引可用 {int(((sd_idx>0)&(panel.sd<=0)).sum())} 窗")
    print(f"  连续口径 sd 中位 {np.median(panel.sd[panel.sd>0]):.2f} / 索引口径 "
          f"{np.median(sd_idx[sd_idx>0]):.2f} 美元")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
