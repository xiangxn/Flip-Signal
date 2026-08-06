#!/usr/bin/env python3
"""
回测参数配置 — 复合评分版翻转信号回测。

所有可调参数集中在此文件，方便调参。
参数说明对应 docs/flip_backtest_plan.md §2 和 §4。
"""

from dataclasses import dataclass, field


@dataclass
class FlipBacktestConfig:
    # ── Layer 0: 前置条件 (§2.1) ──
    trigger_threshold: float = 0.7       # PM 一侧价格超过此值触发
    first_crossing_only: bool = True     # 仅首次穿越触发
    min_pre_snaps: int = 5               # 穿越前至少需要的 snapshot 数

    # ── T+0 实时特征 ──

    # §2.2 路径效率 — 振荡判定核心
    path_eff_oscillating: float = 0.5      # path_eff ≤ 此值 → 判定为来回振荡 (5s数据校准)

    # §2.3 噪声比
    noise_ratio_oscillating: float = 5.0   # noise_ratio > 此值 → 加强振荡判定

    # §2.4 翻转次数
    flips_oscillating: int = 2             # flips > 此值 → 加强振荡判定 (5s数据校准)

    # §2.5 来回振荡综合判定: 三个条件需同时满足
    #   path_eff <= path_eff_oscillating AND
    #   noise_ratio > noise_ratio_oscillating AND
    #   flips > flips_oscillating

    # §2.6 振幅扩张 (tick-independent: |price-open|/hist_avg_range)
    hist_window_N: int = 18              # 前 N 根 K 线 (~1.5小时, 最优)
    range_exp_threshold: float = 0.5     # 振幅 < 此值 → BTC没动, PM过度自信 → +2
    range_exp_max: float = 2.0           # F0: 振幅 ≥ 此值 → 真突破, 一票否决

    # §2.7 对面价格确认 (T+5s 特征)
    confirm_delay_ticks: int = 1         # 确认等待 tick 数 (5s 数据下 1 tick = 5s)
    other_delta_strong: float = 0.03     # 对面涨幅 > 此值 → +4 分
    other_delta_weak: float = 0.01       # 对面涨幅 > 此值 → +2 分

    # §2.8 BTC 位置 (tick-independent: vs Open, 以 hist_avg_range 为单位)
    btc_pos_max: float = 0.5             # YES>0.7: 0 < btc_pos < 此值 → +1
    btc_pos_min: float = -0.5            # NO>0.7:  此值 < btc_pos < 0 → +1

    # §2.9 入场价格
    entry_cheap_strong: float = 0.20     # entry < 此值 → +2 分
    entry_cheap_weak: float = 0.25       # entry < 此值 → +1 分

    # ── 评分权重 (§2.10) ──
    w_other_d5_strong: int = 4           # 对面大涨
    w_other_d5_weak: int = 2             # 对面小涨
    w_oscillating: int = 1               # 来回振荡
    w_cheap_entry_strong: int = 0        # OPT#5: 极低价入场加分移除 (entry<0.20 全亏)
    w_cheap_entry_weak: int = 1          # 低价入场
    w_range_expansion: int = 2           # 振幅扩张
    w_btc_extreme: int = 1               # BTC 极端位置

    # ── 入场阈值 (§2.10) ──
    score_entry: int = 5                 # ≥ 此值 → 开仓 (1 share)
    score_add: int = 99                  # ≥ 此值 → 加仓 (disabled: 5s下评分不够精细)


# 默认配置实例
DEFAULT_CONFIG = FlipBacktestConfig()
