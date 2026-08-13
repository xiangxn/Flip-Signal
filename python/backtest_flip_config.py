#!/usr/bin/env python3
"""
回测参数配置 — 精简公式版翻转信号回测（Formula B, 2026-08-13 标定）。

所有可调参数集中在此文件，方便调参。
完整方案见 docs/flip_strategy_plan_2026-08-13.md，
分析过程见 docs/flip_optimization_analysis_2026-08-13.md。

Formula B — 三个信号条件，与策略哲学一一对应:
  B1 背离硬要求 divergence_floor=0.0（2026-08-13 频率优化版）:
      穿越时刻 BTC 不得与 PM 同向（div < floor 直接否决；YES侧
      div=-btc_pos，NO侧 div=+btc_pos）。同向穿越 EV≈0（χ²=125），
      直接否决；中性/背离穿越均可入场。min_divergence 可选再要求
      背离强度（默认 0 = 不要求）。历史振幅未就绪的事件整体跳过。
  B2 过度自信 range_expansion < 1.0 → +2:
      BTC 振幅 < 1 个历史平均振幅 = BTC 未真正突破但 PM 已 0.7+。
  B3 确认回归 other_delta 三档 → +3/+2/+1:
      确认期（T+2 ticks）对侧 bid 回升 = 反转正在发生。

  score_entry = 2: 任一核心信号成立即触发（B2 或 od>0.02），无需特征堆叠。

成交口径（实盘对齐）:
  yes_price/no_price 存的是各订单簿 BEST BID。实盘 FAK 买对侧成交在
  ASK = 1 - 触发侧 bid（双token互补）。fill 与 gate 均按 ask 口径。

支持的市场: BTC, ETH（ETH 沿用 BTC 参数，待独立标定）

基准结果（data_0, 1719 事件, ask 口径, 2026-08-13 频率优化版）:
  n=364, 2.54 信号/小时, WR 45.6%, EV +0.234/笔, P&L +85.10, PF 2.89
  （详见 docs/flip_frequency_optimization_2026-08-13.md）

用法:
  python backtest_flip_scoring.py --profile btc --data ../data_0/lab/
"""

from dataclasses import dataclass


@dataclass
class FlipBacktestConfig:
    # ── Layer 0: 前置条件 ──
    trigger_threshold: float = 0.7       # PM 一侧 bid 超过此值触发
    min_pre_snaps: int = 5               # 穿越前至少需要的 snapshot 数
    max_remaining_sec: int = 260         # 窗口有效期上限: 仅 remaining_sec < 此值的穿越才有效
    min_remaining_sec: int = 15          # 窗口有效期下限: 确认(2 ticks×5s)+FAK 执行缓冲(~5-10s)

    # ── B1 背离硬要求（Formula B 核心）──
    # 双向过滤：divergence_floor 否决同向 + min_divergence 可选强度要求。
    # 背离度 = ±btc_pos（YES侧取负）：
    #   div < divergence_floor → 否决（0 = 否决同向穿越，BTC 与 PM 同向 EV≈0）
    #   -999 = 禁用
    divergence_floor: float = 0.0
    #   div < min_divergence → 否决（floor 之上的额外强度门槛）
    # 0 = 不要求额外强度
    min_divergence: float = 0.0

    # ── B2 过度自信（振幅扩张, tick-independent）──
    hist_window_N: int = 18              # 前 N 根 K 线 (~1.5小时) 平均振幅
    range_exp_threshold: float = 1.0     # 振幅 < 此值 → BTC 未真正突破(<1 个历史振幅), PM过度自信 → +2
    range_exp_max: float = 1.5           # 振幅 ≥ 此值 → 真突破, PM是对的 → 否决

    # ── B3 确认回归（T+delay 特征）──
    confirm_delay_ticks: int = 2         # 确认等待 tick 数 → 10s
    od_hard_filter: float = -999.0       # other_delta 硬过滤下限, -999 = 禁用
    other_delta_vstrong: float = 0.05    # 对侧 bid 涨 > 此值 → +3
    other_delta_strong: float = 0.02     # 对侧 bid 涨 > 此值 → +2
    other_delta_weak: float = 0.01       # 对侧 bid 涨 > 此值 → +1

    # ── 入场价上限 gate（ASK 口径，实盘对齐 trading.max_price）──
    # 确认时刻对侧 ASK (=1-触发侧bid) > 此值 → 信号无效
    # 实盘 Trader 用最优卖价校验 FAK，回测必须同口径
    max_entry_price: float = 0.45

    # ── 评分权重（Formula B）──
    w_other_d5_vstrong: int = 3          # 对侧确认大涨
    w_other_d5_strong: int = 2           # 对侧确认中涨
    w_other_d5_weak: int = 1             # 对侧确认小涨
    w_range_expansion: int = 2           # BTC没动但PM 0.7+ → 过度自信

    # ── 入场阈值 ──
    score_entry: int = 2                 # ≥ 此值 → 开仓（od>0.02 或 range<0.5 即触发）
    score_add: int = 99                  # ≥ 此值 → 加仓（disabled）


# 默认配置实例
DEFAULT_CONFIG = FlipBacktestConfig()  # BTC 参数 (2026-08-13 Formula B: n=126, WR 46.0%, P&L +26.91, PF 2.60)
ETH_CONFIG = FlipBacktestConfig()      # ETH 参数 (沿用 BTC，待独立标定)
