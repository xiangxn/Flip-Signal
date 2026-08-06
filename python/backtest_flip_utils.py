#!/usr/bin/env python3
"""
回测工具函数 — 复合评分版翻转信号回测。

特征提取 + 信号检测的纯函数，不依赖 IO。
所有函数均为无副作用纯函数，便于测试和调参。

对应 docs/flip_backtest_plan.md §2-§3。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from backtest_flip_config import FlipBacktestConfig, DEFAULT_CONFIG


# ═══════════════════════════════════════════════════════════════
# 事件级预处理
# ═══════════════════════════════════════════════════════════════

def compute_hist_avg_range(events: list[dict], window_N: int = 18) -> None:
    """对每个事件计算前 N 根 K 线的平均振幅，原地注入 hist_avg_range。

    events 需按 start_time 升序排列。
    振幅 = close_price - open_price 的绝对值。
    不足 3 个历史事件时设为 None。
    """
    for i, event in enumerate(events):
        prev_ranges = []
        for j in range(max(0, i - window_N), i):
            r = abs(events[j]["close_price"] - events[j]["open_price"])
            prev_ranges.append(r)
        if len(prev_ranges) >= 3:
            event["hist_avg_range"] = sum(prev_ranges) / len(prev_ranges)
        else:
            event["hist_avg_range"] = None


def load_events_from_dir(data_dir: str) -> list[dict]:
    """从 JSONL 目录加载所有 event，按 start_time 排序并计算历史振幅。"""
    import json
    from pathlib import Path

    events = []
    for path in sorted(Path(data_dir).glob("events_*.jsonl")):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))

    events.sort(key=lambda e: e["start_time"])
    compute_hist_avg_range(events)
    return events


# ═══════════════════════════════════════════════════════════════
# 特征提取 (T=0 实时特征)
# ═══════════════════════════════════════════════════════════════

def extract_pre_prices(snapshots: list[dict], cross_idx: int) -> list[float]:
    """提取穿越时刻之前 (含穿越时刻) 的所有 BTC 价格。"""
    return [s["price"] for s in snapshots[: cross_idx + 1]]


def compute_path_efficiency(pre_prices: list[float], open_price: float) -> float:
    """§2.2 路径效率 = |穿越时BTC价 - Open| / pre_range。

    ~1.0 = 单边趋势, ~0.0 = 来回振荡。
    """
    net_move = abs(pre_prices[-1] - open_price)
    pre_high = max(pre_prices)
    pre_low = min(pre_prices)
    pre_range = pre_high - pre_low
    if pre_range == 0:
        return 0.0
    return net_move / pre_range


def compute_pre_range(pre_prices: list[float]) -> tuple[float, float, float]:
    """返回 (pre_high, pre_low, pre_range)。"""
    pre_high = max(pre_prices)
    pre_low = min(pre_prices)
    return pre_high, pre_low, pre_high - pre_low


def compute_noise_ratio(pre_prices: list[float], net_move: float) -> float:
    """§2.3 噪声比 = total_path / net_move。

    total_path = Σ|p[i] - p[i-1]| (tick 级别累计路径长度)。
    """
    total_path = sum(
        abs(pre_prices[i] - pre_prices[i - 1])
        for i in range(1, len(pre_prices))
    )
    if net_move == 0:
        return total_path  # 纯震荡，净位移为 0
    return total_path / net_move


def compute_flips(pre_prices: list[float]) -> int:
    """§2.4 方向翻转次数。

    统计价格方向变化的次数 (忽略平盘 tick)。
    """
    flips = 0
    for i in range(2, len(pre_prices)):
        d1 = pre_prices[i - 1] - pre_prices[i - 2]
        d2 = pre_prices[i] - pre_prices[i - 1]
        if d1 != 0 and d2 != 0 and (d1 > 0) != (d2 > 0):
            flips += 1
    return flips


def is_oscillating(pre_prices: list[float], open_price: float,
                 cfg: FlipBacktestConfig) -> bool:
    """§2.5 来回振荡综合判定: 三个条件需同时满足。"""
    path_eff = compute_path_efficiency(pre_prices, open_price)
    net_move = abs(pre_prices[-1] - open_price)
    noise_ratio = compute_noise_ratio(pre_prices, net_move)
    flips = compute_flips(pre_prices)

    return (
        path_eff <= cfg.path_eff_oscillating
        and noise_ratio > cfg.noise_ratio_oscillating
        and flips > cfg.flips_oscillating
    )


def compute_range_expansion(price_at_cross: float, open_price: float,
                            hist_avg_range: float | None) -> float | None:
    """§2.6 振幅扩张 = abs(price - open) / hist_avg_range。

    Tick-independent — 不依赖采样频率。
    无历史数据时返回 None。
    """
    if hist_avg_range is None or hist_avg_range == 0:
        return None
    return abs(price_at_cross - open_price) / hist_avg_range


def compute_btc_position(price_at_cross: float, open_price: float,
                         hist_avg_range: float) -> float:
    """§2.8 BTC 位移 vs Open，以历史平均振幅为单位。

    btc_position = (price - open) / hist_avg_range

    Tick-independent — 不依赖采样频率，实盘流式数据同样适用。
    +1.0 = BTC 涨了 1x 历史振幅, -1.0 = BTC 跌了 1x 历史振幅。
    """
    if hist_avg_range == 0:
        return 0.0
    return (price_at_cross - open_price) / hist_avg_range


def compute_other_delta(snapshots: list[dict], cross_idx: int, other_key: str,
                        cfg: FlipBacktestConfig) -> float:
    """§2.7 对面价格 N 秒变化。

    other_delta = other_price[t + confirm_delay_ticks] - other_price[t]。
    """
    conf_idx = min(cross_idx + cfg.confirm_delay_ticks, len(snapshots) - 1)
    return snapshots[conf_idx][other_key] - snapshots[cross_idx][other_key]


# ═══════════════════════════════════════════════════════════════
# 信号检测 & 评分
# ═══════════════════════════════════════════════════════════════

@dataclass
class FlipSignal:
    """单笔翻转信号。"""
    event_time: int         # unix 秒
    condition_id: str
    side: str               # "yes" 或 "no"
    score: int              # 复合评分
    entry_price: float      # 入场价 (对面价)
    won: bool               # 是否赢
    pnl: float              # 盈亏
    shares: int             # 下单量
    remaining_sec: int      # 入场时剩余秒数

    # 特征详情 (debug / 分析用)
    path_eff: float
    noise_ratio: float
    flips: int
    is_oscillating: bool
    range_expansion: float | None
    btc_position: float
    btc_extreme: bool
    other_delta: float


def check_signal(event: dict, side: str,
                 cfg: FlipBacktestConfig) -> FlipSignal | None:
    """对给定 side (yes/no) 检测 >0.7 穿越并评估信号。

    这是核心信号检测函数，对应 §3.2 check_signal。
    """
    this_key = "yes_price" if side == "yes" else "no_price"
    other_key = "no_price" if side == "yes" else "yes_price"
    snaps = event["snapshots"]

    # Step 1: 找第一次 >0.7 穿越
    cross_idx = None
    for i, s in enumerate(snaps):
        if s[this_key] > cfg.trigger_threshold:
            cross_idx = i
            break

    if cross_idx is None or cross_idx < cfg.min_pre_snaps:
        return None

    cross_snap = snaps[cross_idx]
    pre_prices = extract_pre_prices(snaps, cross_idx)
    open_price = event["open_price"]

    # ── T=0 实时特征 ──

    # 路径效率 + 子特征
    net_move = abs(cross_snap["price"] - open_price)
    pre_high, pre_low, pre_range = compute_pre_range(pre_prices)
    if pre_range == 0:
        return None

    path_eff = compute_path_efficiency(pre_prices, open_price)
    noise_ratio_val = compute_noise_ratio(pre_prices, net_move)
    flips_val = compute_flips(pre_prices)
    oscillating = is_oscillating(pre_prices, open_price, cfg)

    # Hard filter: noise_ratio > 3.0 → 不触发
    # OPT#3: 高噪声信号胜率 0%，noise > 3.0 的 5 笔全亏
    if noise_ratio_val > 3.0:
        return None

    # Hard filter: path_eff < 0.4 → 不触发
    # OPT#7: 极低路径效率 = 趋势不明确，剩余 2 亏中 L1=0.38
    if path_eff < 0.4:
        return None

    # 振幅扩张 (tick-independent: |price-open|/hist_avg_range)
    range_expansion = compute_range_expansion(cross_snap["price"], open_price,
                                              event.get("hist_avg_range"))

    # F0: 振幅过大 — 一票否决 (BTC 大幅移动 = PM 是对的)
    if range_expansion is not None and range_expansion >= cfg.range_exp_max:
        return None

    # BTC 位置 (tick-independent: vs Open, 以历史振幅为单位)
    btc_position = compute_btc_position(cross_snap["price"], open_price,
                                        event.get("hist_avg_range", 0))
    # OPT#2: 反转 btc_extreme 方向 — 奖励 BTC 与 PM 背离
    # Flip 策略赌 PM 过度反应。BTC 与 PM 同向 = PM 正确，不应加分
    # BTC 与 PM 反向 = PM 可能错了，这才是 flip 的 edge
    if side == "yes":
        # YES>0.7 (PM看涨), BTC 微跌 → PM 过度反应，flip edge
        btc_extreme = (cfg.btc_pos_min < btc_position < 0)
    else:
        # NO>0.7 (PM看跌), BTC 微涨 → PM 过度反应，flip edge
        btc_extreme = (0 < btc_position < cfg.btc_pos_max)

    # 入场价
    entry_price = cross_snap[other_key]

    # ── T+5s 确认特征 ──
    other_delta = compute_other_delta(snaps, cross_idx, other_key, cfg)

    # Hard filter: 对面涨幅 < 0.03 → 不触发 (§2.7)
    # OPT#1: 从 -0.02 提高到 0.03。other_delta < 0.03 的 9 笔全亏，零胜率
    if other_delta < 0.03:
        return None

    # ── Step 4: 计算评分 ──
    score = 0

    # F1/F2: 对面价格变化
    if other_delta > cfg.other_delta_strong:
        score += cfg.w_other_d5_strong
    elif other_delta > cfg.other_delta_weak:
        score += cfg.w_other_d5_weak

    # F3: 来回振荡
    if oscillating:
        score += cfg.w_oscillating

    # F4/F5: 低价入场
    if entry_price < cfg.entry_cheap_strong:
        score += cfg.w_cheap_entry_strong
    elif entry_price < cfg.entry_cheap_weak:
        score += cfg.w_cheap_entry_weak

    # F6: BTC 还没怎么动 (range_expansion < threshold) → PM 过度自信 → +2
    if range_expansion is not None and range_expansion < cfg.range_exp_threshold:
        score += cfg.w_range_expansion

    # F7: BTC 极端位置
    if btc_extreme:
        score += cfg.w_btc_extreme

    # ── Step 5: 判断入场 ──
    if score < cfg.score_entry:
        return None

    shares = 2 if score >= cfg.score_add else 1

    # ── Step 6: 判定输赢 ──
    # outcome=0 → UP 赢, outcome=1 → DOWN 赢
    if side == "yes":
        # YES>0.7, 我们买 NO (赌 DOWN)
        won = (event["outcome"] == 1)
    else:
        # NO>0.7, 我们买 YES (赌 UP)
        won = (event["outcome"] == 0)

    pnl = (1.0 - entry_price) * shares if won else (0.0 - entry_price) * shares

    return FlipSignal(
        event_time=event["start_time"],
        condition_id=event["condition_id"],
        side=side,
        score=score,
        entry_price=entry_price,
        won=won,
        pnl=pnl,
        shares=shares,
        remaining_sec=cross_snap["remaining_sec"],
        path_eff=path_eff,
        noise_ratio=noise_ratio_val,
        flips=flips_val,
        is_oscillating=oscillating,
        range_expansion=range_expansion,
        btc_position=btc_position,
        btc_extreme=btc_extreme,
        other_delta=other_delta,
    )


# ═══════════════════════════════════════════════════════════════
# 回测运行
# ═══════════════════════════════════════════════════════════════

def run_backtest(events: list[dict],
                 cfg: FlipBacktestConfig = None) -> tuple[list[FlipSignal], int, int]:
    """对全部事件运行回测，返回 (信号列表, 总事件数, 有效事件数)。

    每个事件最多触发一次 (first_crossing_only)。
    """
    if cfg is None:
        cfg = DEFAULT_CONFIG

    signals: list[FlipSignal] = []
    total = len(events)

    # Ensure hist_avg_range uses the configured window size
    compute_hist_avg_range(events, window_N=cfg.hist_window_N)
    active = sum(1 for e in events if e.get("hist_avg_range") is not None)

    for event in events:
        # Step 0: 跳过历史振幅不足的事件
        if event.get("hist_avg_range") is None:
            continue

        # Step 1: 寻找第一次 >0.7 穿越 (两边都检查)
        for side in ("yes", "no"):
            signal = check_signal(event, side, cfg)
            if signal is not None:
                signals.append(signal)
                if cfg.first_crossing_only:
                    break  # 每个事件最多触发一次

    return signals, total, active


def print_summary(signals: list[FlipSignal],
                  total_events: int = 0,
                  active_events: int = 0) -> None:
    """打印回测汇总报告 (§3.3, §5)。"""
    if not signals:
        print("无信号。")
        return

    n = len(signals)
    wins = sum(1 for s in signals if s.won)
    total_pnl = sum(s.pnl for s in signals)
    avg_entry = sum(s.entry_price for s in signals) / n
    avg_score = sum(s.score for s in signals) / n
    avg_shares = sum(s.shares for s in signals) / n

    # 从事件时间跨度估算天数
    if signals:
        t_min = min(s.event_time for s in signals)
        t_max = max(s.event_time for s in signals)
        days = max((t_max - t_min) / 86400, 1)
        daily_rate = n / days
    else:
        daily_rate = 0

    print("=" * 58)
    print("  复合评分版翻转信号回测 — 结果汇总")
    print("=" * 58)
    if total_events:
        print(f"  总事件数:      {total_events:>5d}")
    if active_events:
        print(f"  有效事件:      {active_events:>5d}  (有历史振幅)")
    print(f"  总信号数:      {n:>5d}")
    print(f"  日均信号:      {daily_rate:>5.0f}")
    print(f"  胜率:          {wins / n * 100:>5.1f}%  ({wins}/{n})")
    print(f"  总 P&L:        {total_pnl:>+7.2f}")
    print(f"  平均入场价:    {avg_entry:>7.3f}")
    print(f"  平均分数:      {avg_score:>7.1f}")
    print(f"  平均下单量:    {avg_shares:>7.1f} shares")
    losers_pnl = sum(s.pnl for s in signals if s.pnl < 0)
    if losers_pnl < 0 and total_pnl > 0:
        print(f"  盈亏比:        {total_pnl / abs(losers_pnl):>7.1f}")
    else:
        print(f"  盈亏比:        N/A")
    print("-" * 58)

    # 按分数分桶
    for lo, hi, label in [(0, 5, "Score 0-4 (未触发)"),
                            (5, 7, "Score 5-6 (开仓)"),
                            (7, 99, "Score 7+  (加仓)")]:
        subset = [s for s in signals if lo <= s.score < hi]
        if subset:
            w = sum(1 for s in subset if s.won)
            p = sum(s.pnl for s in subset)
            avg_e = sum(s.entry_price for s in subset) / len(subset)
            print(f"  {label:22s}: n={len(subset):>3d}, "
                  f"win={w / len(subset) * 100:>5.1f}%, "
                  f"P&L={p:>+7.2f}, "
                  f"avg_entry={avg_e:.3f}")
    print("-" * 58)

    # 按特征分桶
    print("  特征分桶:")
    for label, pred in [
        ("来回振荡 (oscillating)      ", lambda s: s.is_oscillating),
        ("单边趋势 (trending)       ", lambda s: not s.is_oscillating),
        ("对面上升 (other_delta>0)  ", lambda s: s.other_delta > 0),
        ("对面下跌 (other_delta<=0) ", lambda s: s.other_delta <= 0),
    ]:
        subset = [s for s in signals if pred(s)]
        if subset:
            w = sum(1 for s in subset if s.won)
            p = sum(s.pnl for s in subset)
            print(f"  {label}: n={len(subset):>3d}, "
                  f"win={w / len(subset) * 100:>5.1f}%, "
                  f"P&L={p:>+7.2f}")
    print("=" * 58)


def export_signals_jsonl(signals: list[FlipSignal], path: str) -> None:
    """将信号导出为 JSONL 文件 (§5.3)。"""
    import json
    from dataclasses import asdict

    with open(path, "w") as f:
        for s in signals:
            d = asdict(s)
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
