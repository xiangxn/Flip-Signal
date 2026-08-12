#!/usr/bin/env python3
"""
回测参数配置 — 复合评分版翻转信号回测。

所有可调参数集中在此文件，方便调参。
参数说明对应 docs/flip_backtest_plan.md §2 和 §4。

支持的市场: BTC, ETH

各市场参数独立预设:
  - DEFAULT_CONFIG     BTC 回测参数 (2026-08-12 sweep: flips>1, 胜率 50.4%, P&L +62.79, 盈亏比 2.3)
  - ETH_CONFIG         ETH 回测参数 (初始值与 BTC 相同，待独立调参)

用法:
  python backtest_flip_scoring.py --profile btc --data ../data/btc/
  python backtest_flip_scoring.py --profile eth --data ../data/eth/
"""

from dataclasses import dataclass


@dataclass
class FlipBacktestConfig:
    # ── Layer 0: 前置条件 (§2.1) ──
    trigger_threshold: float = 0.7       # PM 一侧价格超过此值触发
    allow_retry_crossings: bool = True   # 多穿越重试: True=每个上升沿都尝试评分（首个通过者获胜），False=仅首次穿越（旧行为）
    min_pre_snaps: int = 5               # 穿越前至少需要的 snapshot 数
    max_remaining_sec: int = 260         # §2.1 窗口有效期: 仅 remaining_sec < 此值的穿越才有效 (与 >0.7 同级前置条件)

    # ── T+0 实时特征 ──

    # §2.2 路径效率 — 振荡判定核心
    path_eff_oscillating: float = 0.7      # 收紧到 0.7 (原 0.8 太宽, 胜率不足)

    # §2.3 噪声比
    noise_ratio_oscillating: float = 1.5   # 不变

    # §2.4 翻转次数
    flips_oscillating: int = 1             # 2026-08-12 sweep: 1 比 2 多 6 笔优质振荡信号，胜率不变 P&L 更高

    # §2.5 来回振荡综合判定: 三个条件需同时满足
    #   path_eff <= path_eff_oscillating AND
    #   noise_ratio > noise_ratio_oscillating AND
    #   flips > flips_oscillating (当前 >1 即 ≥2 次翻转)

    # §2.6 振幅扩张 (tick-independent: |price-open|/hist_avg_range)
    hist_window_N: int = 18              # 前 N 根 K 线 (~1.5小时, 最优)
    range_exp_threshold: float = 0.5     # 振幅 < 此值 → BTC没动, PM过度自信 → +2
    range_exp_max: float = 1.5           # F0: 收紧到 1.5 (原 2.0 太宽, 真突破仍然通过了)

    # §2.7 对面价格确认 (T+5s 特征) — Formula A: 三档评分
    confirm_delay_ticks: int = 5         # 确认等待 tick 数 → 25s (原 1=5s, 5s 数据下对面价格来不及反应)
    od_hard_filter: float = -999.0       # other_delta 硬过滤下限, -999 = 禁用 (Formula A: 由评分权重处理)
    other_delta_vstrong: float = 0.05    # 对面涨幅 > 此值 → +3 分 (原 +4)
    other_delta_strong: float = 0.02     # 对面涨幅 > 此值 → +2 分 (原 0.03, +4)
    other_delta_weak: float = 0.01       # 对面涨幅 > 此值 → +1 分 (原 +2)

    # §2.8 BTC 位置 (tick-independent: vs Open, 以 hist_avg_range 为单位)
    # Formula A: 放宽 — YES侧 btc_pos < -0.1, NO侧 btc_pos > 0.1
    btc_pos_max: float = 0.1             # NO>0.7: btc_pos > 此值 → BTC与PM背离 → +1
    btc_pos_min: float = -0.1            # YES>0.7: btc_pos < 此值 → BTC与PM背离 → +1

    # §2.9 入场价格
    entry_cheap_strong: float = 0.20     # entry < 此值 → +1 分 (Formula A: 重新激活)
    entry_cheap_weak: float = 0.25       # entry < 此值 → +1 分

    # ── 评分权重 (§2.10) — Formula A ──
    w_other_d5_vstrong: int = 3          # 对面大涨 (od > 0.05)
    w_other_d5_strong: int = 2           # 对面中涨 (od > 0.02)
    w_other_d5_weak: int = 1             # 对面小涨 (od > 0.01)
    w_oscillating: int = 1               # 来回振荡 (降低到 +1: 振荡信号胜率 31% 远低于趋势 45%)
    w_cheap_entry_strong: int = 1        # 极低价入场 (Formula A: 重新激活)
    w_cheap_entry_weak: int = 1          # 低价入场
    w_range_expansion: int = 2           # 振幅扩张
    w_btc_extreme: int = 1               # BTC 极端位置

    # ── 入场阈值 (§2.10) ──
    score_entry: int = 5                 # ≥ 此值 → 开仓 (1 share)
    score_add: int = 99                  # ≥ 此值 → 加仓 (disabled: 5s下评分不够精细)


# 默认配置实例
DEFAULT_CONFIG = FlipBacktestConfig()  # BTC 回测参数 (2026-08-12 sweep 优化: WR 50.9%, P&L +62)
ETH_CONFIG = FlipBacktestConfig()       # ETH 回测参数 (初始值与 BTC 相同，待独立调参)
