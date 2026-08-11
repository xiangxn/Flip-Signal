#!/usr/bin/env python3
"""
BTC Price Position 回测 — 按 data_flip_analysis.md 原始逻辑重建。

核心改动 vs 现有 backtest_flip_scoring.py:
  - 新增 btc_range_position = (price - preLow) / (preHigh - preLow)
    这是 data_flip_analysis.md §3 中效应量 1.25-1.47 的主导因子，
    在后续版本中被替换为 (price-open)/histAvgRange，现在恢复。
  - 用 5s-tick 数据重新校准阈值。
  - 独立测试 btc_range_position + 与现有 7 特征组合。

Usage:
    python backtest_btc_position.py --data ../data_0/lab/
    python backtest_btc_position.py --data ../data_0/lab/ --verbose
"""

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ── Config ────────────────────────────────────────────────────────


@dataclass
class Config:
    # Layer 0: 前置条件
    trigger_threshold: float = 0.7
    min_pre_snaps: int = 5
    max_remaining_sec: int = 260

    # 确认延迟 (tick 数, 1 tick = 5s)
    confirm_delay_ticks: int = 1

    # ── 原始 BTC Range Position (data_flip_analysis.md §3) ──
    # btc_range_position = (price - preLow) / (preHigh - preLow)
    # YES>0.7: 高位 (>0.7) → flip 概率高 (BTC 涨不动了)
    # NO>0.7:  低位 (<0.3) → flip 概率高 (BTC 跌不动了)
    btc_range_pos_high: float = 0.7   # YES side: position > this → extreme
    btc_range_pos_low: float = 0.3    # NO  side: position < this → extreme
    w_btc_range_pos: int = 3          # 权重 (最强单因子, 应高于 other_delta)

    # ── 现有特征阈值 & 权重 (沿用 Formula A, 可覆盖) ──

    # other_delta
    od_vstrong: float = 0.05
    od_strong: float = 0.02
    od_weak: float = 0.01
    w_od_vstrong: int = 3
    w_od_strong: int = 2
    w_od_weak: int = 1

    # 振荡
    path_eff_osc: float = 0.8
    noise_ratio_osc: float = 1.5
    flips_osc: int = 1
    w_oscillating: int = 2

    # 入场价 (elif)
    entry_cheap_strong: float = 0.20
    entry_cheap_weak: float = 0.25
    w_entry_strong: int = 1
    w_entry_weak: int = 1

    # 振幅扩张 (vs hist_avg_range)
    hist_window_n: int = 18
    range_exp_small: float = 0.5
    range_exp_veto: float = 2.0
    w_range_small: int = 2

    # BTC 方向背离 (Formula A F7, 保留)
    btc_pos_max: float = 0.1
    btc_pos_min: float = -0.1
    w_btc_diverge: int = 1

    # T=0 否决
    path_eff_veto_min: float = 0.4
    noise_ratio_veto_max: float = 3.0

    # 入场阈值
    score_entry: int = 5

    # 多穿越模式
    allow_retry: bool = True


CFG = Config()


# ── Feature Extraction ────────────────────────────────────────────


def compute_hist_avg_range(events: list[dict], window_n: int = 18) -> None:
    """原地注入 hist_avg_range (前 N 根 K 线 |close-open| 均值)."""
    for i, event in enumerate(events):
        prev = []
        for j in range(max(0, i - window_n), i):
            r = abs(events[j]["close_price"] - events[j]["open_price"])
            prev.append(r)
        event["hist_avg_range"] = sum(prev) / len(prev) if len(prev) >= 3 else None


def load_events(data_dir: str) -> list[dict]:
    events = []
    for path in sorted(Path(data_dir).glob("events_*.jsonl")):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    events.sort(key=lambda e: e["start_time"])
    compute_hist_avg_range(events, CFG.hist_window_n)
    return events


def path_efficiency(pre_prices: list[float], open_price: float) -> float:
    net = abs(pre_prices[-1] - open_price)
    hi, lo = max(pre_prices), min(pre_prices)
    rng = hi - lo
    return net / rng if rng > 0 else 0.0


def noise_ratio(pre_prices: list[float], net_move: float) -> float:
    total = sum(abs(pre_prices[i] - pre_prices[i - 1]) for i in range(1, len(pre_prices)))
    return total / net_move if net_move > 0 else total


def count_flips(pre_prices: list[float]) -> int:
    flips = 0
    for i in range(2, len(pre_prices)):
        d1 = pre_prices[i - 1] - pre_prices[i - 2]
        d2 = pre_prices[i] - pre_prices[i - 1]
        if d1 != 0 and d2 != 0 and (d1 > 0) != (d2 > 0):
            flips += 1
    return flips


def is_oscillating(path_eff: float, noise: float, flips: int) -> bool:
    return (
        path_eff <= CFG.path_eff_osc
        and noise > CFG.noise_ratio_osc
        and flips > CFG.flips_osc
    )


def btc_range_position(price: float, pre_high: float, pre_low: float) -> float:
    """data_flip_analysis.md §3 原始定义:
    BTC 在穿越前区间内的归一化位置 (0=最低点, 1=最高点)."""
    rng = pre_high - pre_low
    return (price - pre_low) / rng if rng > 0 else 0.5


def btc_position_vs_open(price: float, open_price: float, hist_avg: float) -> float:
    """当前代码的 btc_position: (price-open)/histAvgRange (保留用于对比)."""
    return (price - open_price) / hist_avg if hist_avg and hist_avg > 0 else 0.0


# ── Signal Output ──────────────────────────────────────────────────


@dataclass
class FlipSignal:
    time: int
    condition_id: str
    side: str
    score: int
    entry_price: float
    shares: int
    won: bool
    pnl: float
    remaining_sec: int

    # 特征值
    btc_range_pos: float     # ★ 新增: 原始 BTC Position
    btc_pos_vs_open: float   # 旧 btc_position (对比用)
    path_eff: float
    noise_ratio: float
    flips: int
    is_oscillating: bool
    range_expansion: Optional[float]
    btc_extreme_diverge: bool  # 旧 F7
    other_delta: float

    # 评分明细
    score_detail: dict


# ── Scoring ───────────────────────────────────────────────────────


def score_crossing(event: dict, side: str, cross_idx: int) -> Optional[FlipSignal]:
    """对指定穿越点评分, 包含原始 BTC Range Position + 现有 7 特征."""
    this_key = "yes_price" if side == "yes" else "no_price"
    other_key = "no_price" if side == "yes" else "yes_price"
    snaps = event["snapshots"]
    cross_snap = snaps[cross_idx]
    open_price = event["open_price"]

    pre_prices = [s["price"] for s in snaps[: cross_idx + 1]]
    pre_high = max(pre_prices)
    pre_low = min(pre_prices)
    pre_range = pre_high - pre_low
    if pre_range == 0:
        return None

    net_move = abs(pre_prices[-1] - open_price)

    # ── T=0 特征 ──

    path_eff = path_efficiency(pre_prices, open_price)
    noise = noise_ratio(pre_prices, net_move)
    flips = count_flips(pre_prices)

    # T=0 否决
    if path_eff < CFG.path_eff_veto_min:
        return None
    if noise > CFG.noise_ratio_veto_max:
        return None

    oscillating = is_oscillating(path_eff, noise, flips)

    # ★ 原始 BTC Range Position
    btc_rp = btc_range_position(cross_snap["price"], pre_high, pre_low)

    # 旧 btc_position (保留对比)
    hist_avg = event.get("hist_avg_range")
    btp_open = btc_position_vs_open(cross_snap["price"], open_price, hist_avg or 0)

    # Range expansion (vs hist avg)
    range_exp = None
    if hist_avg is not None and hist_avg > 0:
        range_exp = abs(cross_snap["price"] - open_price) / hist_avg
    else:
        return None  # hist_avg 未就绪则跳过

    # F0 否决
    if range_exp >= CFG.range_exp_veto:
        return None

    # ── T+confirm 特征 ──

    conf_idx = min(cross_idx + CFG.confirm_delay_ticks, len(snaps) - 1)
    other_delta = snaps[conf_idx][other_key] - cross_snap[other_key]

    entry_price = cross_snap[other_key]
    if entry_price <= 0.01:
        return None

    # ── 评分 ──

    score = 0
    detail = {}

    # ★ F8 (新): BTC Range Position — 原始主导因子
    if side == "yes":
        # YES>0.7 且 BTC 在高位 → PM 过度看涨 → flip 概率高
        if btc_rp > CFG.btc_range_pos_high:
            score += CFG.w_btc_range_pos
            detail["btc_range_pos_high"] = CFG.w_btc_range_pos
    else:
        # NO>0.7 且 BTC 在低位 → PM 过度看跌 → flip 概率高
        if btc_rp < CFG.btc_range_pos_low:
            score += CFG.w_btc_range_pos
            detail["btc_range_pos_low"] = CFG.w_btc_range_pos

    # F1v/F1/F2: other_delta (elif)
    if other_delta > CFG.od_vstrong:
        score += CFG.w_od_vstrong
        detail["od>0.05"] = CFG.w_od_vstrong
    elif other_delta > CFG.od_strong:
        score += CFG.w_od_strong
        detail["od>0.02"] = CFG.w_od_strong
    elif other_delta > CFG.od_weak:
        score += CFG.w_od_weak
        detail["od>0.01"] = CFG.w_od_weak

    # F3: 振荡
    if oscillating:
        score += CFG.w_oscillating
        detail["osc"] = CFG.w_oscillating

    # F4/F5: 入场价 (elif)
    if entry_price < CFG.entry_cheap_strong:
        score += CFG.w_entry_strong
        detail["entry<0.20"] = CFG.w_entry_strong
    elif entry_price < CFG.entry_cheap_weak:
        score += CFG.w_entry_weak
        detail["entry<0.25"] = CFG.w_entry_weak

    # F6: BTC 振幅萎缩
    if range_exp < CFG.range_exp_small:
        score += CFG.w_range_small
        detail["re<0.5"] = CFG.w_range_small

    # F7: BTC 方向背离 (保留旧逻辑)
    btc_diverge = False
    if side == "yes":
        btc_diverge = btp_open < CFG.btc_pos_min
    else:
        btc_diverge = btp_open > CFG.btc_pos_max
    if btc_diverge:
        score += CFG.w_btc_diverge
        detail["btc_diverge"] = CFG.w_btc_diverge

    if score < CFG.score_entry:
        return None

    # 判定输赢
    if side == "yes":
        won = event["outcome"] == 1  # DOWN
    else:
        won = event["outcome"] == 0  # UP

    pnl = (1.0 - entry_price) if won else (0.0 - entry_price)

    return FlipSignal(
        time=event["start_time"],
        condition_id=event["condition_id"],
        side=side,
        score=score,
        entry_price=entry_price,
        shares=1,
        won=won,
        pnl=pnl,
        remaining_sec=cross_snap["remaining_sec"],
        btc_range_pos=btc_rp,
        btc_pos_vs_open=btp_open,
        path_eff=path_eff,
        noise_ratio=noise,
        flips=flips,
        is_oscillating=oscillating,
        range_expansion=range_exp,
        btc_extreme_diverge=btc_diverge,
        other_delta=other_delta,
        score_detail=detail,
    )


# ── Backtest ──────────────────────────────────────────────────────


def run_backtest(events: list[dict]) -> list[FlipSignal]:
    signals = []
    for event in events:
        if event.get("hist_avg_range") is None:
            continue

        for side in ("yes", "no"):
            this_key = "yes_price" if side == "yes" else "no_price"
            snaps = event["snapshots"]

            if CFG.allow_retry:
                was_above = False
                for i, s in enumerate(snaps):
                    is_above = (
                        s[this_key] > CFG.trigger_threshold
                        and s["remaining_sec"] < CFG.max_remaining_sec
                    )
                    if is_above and not was_above and i >= CFG.min_pre_snaps:
                        sig = score_crossing(event, side, i)
                        if sig is not None:
                            signals.append(sig)
                            break  # 本 side 已命中
                    was_above = is_above
                if any(s.side == "yes" for s in signals[-1:]):
                    break  # YES 优先, 已下注则跳过 NO
            else:
                cross_idx = None
                for i, s in enumerate(snaps):
                    if (
                        s[this_key] > CFG.trigger_threshold
                        and s["remaining_sec"] < CFG.max_remaining_sec
                    ):
                        cross_idx = i
                        break
                if cross_idx is not None and cross_idx >= CFG.min_pre_snaps:
                    sig = score_crossing(event, side, cross_idx)
                    if sig is not None:
                        signals.append(sig)
                        break

    return signals


# ── Reporting ─────────────────────────────────────────────────────


def print_summary(signals: list[FlipSignal], label: str = "") -> None:
    if not signals:
        print("无信号。")
        return

    n = len(signals)
    wins = sum(1 for s in signals if s.won)
    total_pnl = sum(s.pnl for s in signals)
    avg_entry = sum(s.entry_price for s in signals) / n
    avg_score = sum(s.score for s in signals) / n
    losers_pnl = abs(sum(s.pnl for s in signals if s.pnl < 0))
    pf = total_pnl / losers_pnl if losers_pnl > 0 else float("inf")

    avg_btc_rp = sum(s.btc_range_pos for s in signals) / n

    # 时间跨度
    t_min = min(s.time for s in signals)
    t_max = max(s.time for s in signals)
    days = max((t_max - t_min) / 86400, 1)

    print(f"\n{'='*72}")
    print(f"  {label}" if label else "  回测结果")
    print(f"{'='*72}")
    print(f"  信号数:        {n:>6d}")
    print(f"  日均信号:      {n / days:>6.1f}")
    print(f"  胜率:          {wins / n * 100:>6.1f}%  ({wins}/{n})")
    print(f"  总 P&L:        {total_pnl:>+8.2f}")
    print(f"  平均入场价:    {avg_entry:>8.3f}")
    print(f"  平均分数:      {avg_score:>8.1f}")
    print(f"  盈亏比:        {pf:>8.1f}")
    print(f"  平均 BTC RP:   {avg_btc_rp:>8.3f}  (0=low, 1=high)")
    print(f"{'─'*72}")

    # 按分数分桶
    for lo, hi, desc in [
        (0, 5, "Score 0-4 (未触发)"),
        (5, 7, "Score 5-6 (开仓)"),
        (7, 99, "Score 7+  (加仓)"),
    ]:
        sub = [s for s in signals if lo <= s.score < hi]
        if sub:
            w = sum(1 for s in sub if s.won)
            p = sum(s.pnl for s in sub)
            print(f"  {desc:22s}: n={len(sub):>3d}, win={w / len(sub) * 100:>5.1f}%, P&L={p:>+8.2f}")

    # ★ BTC Range Position 分桶
    print(f"  ── BTC Range Position 分桶 ──")
    for lo, hi, desc in [
        (0.0, 0.1, "RP 0.0-0.1 (底部)"),
        (0.1, 0.2, "RP 0.1-0.2"),
        (0.2, 0.3, "RP 0.2-0.3"),
        (0.3, 0.4, "RP 0.3-0.4"),
        (0.4, 0.5, "RP 0.4-0.5"),
        (0.5, 0.6, "RP 0.5-0.6"),
        (0.6, 0.7, "RP 0.6-0.7"),
        (0.7, 0.8, "RP 0.7-0.8"),
        (0.8, 0.9, "RP 0.8-0.9"),
        (0.9, 1.1, "RP 0.9-1.0 (顶部)"),
    ]:
        sub = [s for s in signals if lo <= s.btc_range_pos < hi]
        if sub:
            w = sum(1 for s in sub if s.won)
            p = sum(s.pnl for s in sub)
            print(f"    {desc:22s}: n={len(sub):>3d}, win={w / len(sub) * 100:>5.1f}%, P&L={p:>+8.2f}")

    # 特征触发统计
    print(f"  ── 特征触发频率 ──")
    features = [
        ("btc_range_pos", lambda s: (s.side == "yes" and s.btc_range_pos > CFG.btc_range_pos_high)
                                    or (s.side == "no" and s.btc_range_pos < CFG.btc_range_pos_low)),
        ("oscillating", lambda s: s.is_oscillating),
        ("od>0.01", lambda s: s.other_delta > 0.01),
        ("od>0.02", lambda s: s.other_delta > 0.02),
        ("od>0.05", lambda s: s.other_delta > 0.05),
        ("entry<0.25", lambda s: s.entry_price < 0.25),
        ("entry<0.20", lambda s: s.entry_price < 0.20),
        ("re<0.5", lambda s: s.range_expansion is not None and s.range_expansion < 0.5),
        ("btc_diverge(F7)", lambda s: s.btc_extreme_diverge),
    ]
    for name, pred in features:
        trig = sum(1 for s in signals if pred(s))
        if trig > 0:
            sub = [s for s in signals if pred(s)]
            w = sum(1 for s in sub if s.won)
            print(f"    {name:18s}: triggered {trig}/{n} ({trig/n*100:.0f}%), win={w/trig*100:.1f}%")

    print(f"{'='*72}\n")


def print_comparison(original: list[FlipSignal], new: list[FlipSignal]) -> None:
    """对比: 仅用旧 F7 (btc_diverge) vs 仅用新 BTC Range Position."""
    print(f"\n{'='*72}")
    print(f"  特征贡献对比")
    print(f"{'='*72}")

    for label, pred in [
        ("仅 btc_range_pos 触发      ", lambda s: (s.side == "yes" and s.btc_range_pos > CFG.btc_range_pos_high)
                                                    or (s.side == "no" and s.btc_range_pos < CFG.btc_range_pos_low)),
        ("仅 btc_diverge(F7) 触发    ", lambda s: s.btc_extreme_diverge),
        ("两者同时触发               ", lambda s: (
            (s.side == "yes" and s.btc_range_pos > CFG.btc_range_pos_high)
            or (s.side == "no" and s.btc_range_pos < CFG.btc_range_pos_low)
        ) and s.btc_extreme_diverge),
        ("btc_range_pos 触发但 F7 未 ", lambda s: (
            (s.side == "yes" and s.btc_range_pos > CFG.btc_range_pos_high)
            or (s.side == "no" and s.btc_range_pos < CFG.btc_range_pos_low)
        ) and not s.btc_extreme_diverge),
    ]:
        sub = [s for s in new if pred(s)]
        if sub:
            w = sum(1 for s in sub if s.won)
            p = sum(s.pnl for s in sub)
            print(f"  {label}: n={len(sub):>3d}, win={w / len(sub) * 100:>5.1f}%, P&L={p:>+8.2f}")


def print_detailed(signals: list[FlipSignal]) -> None:
    """逐笔打印."""
    print(f"\n{'─'*110}")
    print(f"  {'time':>12s} {'side':>4s} {'score':>5s} {'entry':>7s} {'pnl':>+7s}  "
          f"{'btcRP':>6s} {'od':>+7s} {'osc':>5s} {'re<0.5':>7s} {'F7':>5s}  detail")
    print(f"{'─'*110}")
    for s in sorted(signals, key=lambda x: x.score, reverse=True):
        detail_str = " + ".join(f"{k}({v})" for k, v in sorted(s.score_detail.items()))
        print(f"  {s.time:>12d} {s.side:>4s} {s.score:>5d} {s.entry_price:>7.4f} {s.pnl:>+7.2f}  "
              f"{s.btc_range_pos:>6.3f} {s.other_delta:>+7.4f} {str(s.is_oscillating):>5s} "
              f"{str(s.range_expansion is not None and s.range_expansion < 0.5):>7s} "
              f"{str(s.btc_extreme_diverge):>5s}  {detail_str}")
    print(f"{'─'*110}")


# ── Standalone btc_range_position strategy (data_flip_analysis §7) ──


def backtest_btc_rp_only(events: list[dict],
                         yes_threshold: float = 0.7,
                         no_threshold: float = 0.3) -> list[FlipSignal]:
    """纯 BTC Range Position 策略 (不含其他特征).

    data_flip_analysis.md §7:
      YES>0.7 + BTC RP > threshold → 买 NO
      NO>0.7  + BTC RP < threshold → 买 YES
    """
    signals = []
    for event in events:
        snaps = event["snapshots"]
        open_price = event["open_price"]

        for side in ("yes", "no"):
            this_key = "yes_price" if side == "yes" else "no_price"
            other_key = "no_price" if side == "yes" else "yes_price"

            cross_idx = None
            for i, s in enumerate(snaps):
                if s[this_key] > CFG.trigger_threshold and s["remaining_sec"] < CFG.max_remaining_sec:
                    cross_idx = i
                    break

            if cross_idx is None or cross_idx < CFG.min_pre_snaps:
                continue

            pre_prices = [s["price"] for s in snaps[: cross_idx + 1]]
            pre_high = max(pre_prices)
            pre_low = min(pre_prices)
            btc_rp = btc_range_position(snaps[cross_idx]["price"], pre_high, pre_low)

            entry_price = snaps[cross_idx][other_key]
            if entry_price <= 0.01:
                continue

            # 触发条件
            if side == "yes" and btc_rp > yes_threshold:
                pass
            elif side == "no" and btc_rp < no_threshold:
                pass
            else:
                continue

            # 判定输赢
            won = (event["outcome"] == 1) if side == "yes" else (event["outcome"] == 0)
            pnl = (1.0 - entry_price) if won else (0.0 - entry_price)

            signals.append(FlipSignal(
                time=event["start_time"],
                condition_id=event["condition_id"],
                side=side,
                score=int(btc_rp * 10),  # pseudo score
                entry_price=entry_price,
                shares=1,
                won=won,
                pnl=pnl,
                remaining_sec=snaps[cross_idx]["remaining_sec"],
                btc_range_pos=btc_rp,
                btc_pos_vs_open=0,
                path_eff=0, noise_ratio=0, flips=0,
                is_oscillating=False,
                range_expansion=None,
                btc_extreme_diverge=False,
                other_delta=0,
                score_detail={"btc_rp_only": 1},
            ))
            break  # YES 优先

    return signals


# ── Main ──────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="BTC Price Position 回测")
    parser.add_argument("--data", default="../data_0/lab/", help="JSONL 数据目录")
    parser.add_argument("--verbose", "-v", action="store_true", help="逐笔详情")
    parser.add_argument("--sweep", action="store_true", help="BTC RP 阈值扫描")
    args = parser.parse_args()

    print(f"加载数据: {args.data}")
    events = load_events(args.data)
    n_with_hist = sum(1 for e in events if e.get("hist_avg_range") is not None)
    print(f"  → {len(events)} 个事件, {n_with_hist} 有历史振幅")

    # ── 1. 纯 BTC Range Position 策略 (data_flip_analysis §7) ──
    print("\n" + "=" * 72)
    print("  1. 纯 BTC Range Position 策略 (data_flip_analysis.md §7)")
    print("=" * 72)

    if args.sweep:
        print(f"\n  {'YES阈值':>8s} {'NO阈值':>8s} {'信号':>5s} {'胜率':>7s} {'P&L':>9s} {'PF':>6s}")
        print(f"  {'─'*50}")
        for yes_t in [0.65, 0.70, 0.75, 0.80, 0.85, 0.90]:
            for no_t in [0.35, 0.30, 0.25, 0.20, 0.15, 0.10]:
                sigs = backtest_btc_rp_only(events, yes_threshold=yes_t, no_threshold=no_t)
                if sigs:
                    n = len(sigs)
                    w = sum(1 for s in sigs if s.won)
                    p = sum(s.pnl for s in sigs)
                    lp = abs(sum(s.pnl for s in sigs if s.pnl < 0))
                    pf = p / lp if lp > 0 else float("inf")
                    if n >= 5:
                        marker = " ★" if w / n > 0.5 and p > 0 else ""
                        print(f"  {yes_t:>8.2f} {no_t:>8.2f} {n:>5d} {w / n * 100:>6.1f}% {p:>+9.2f} {pf:>6.1f}{marker}")

    # 默认阈值
    for yes_t, no_t in [(0.75, 0.25), (0.80, 0.20), (0.70, 0.30)]:
        sigs = backtest_btc_rp_only(events, yes_threshold=yes_t, no_threshold=no_t)
        if sigs:
            n = len(sigs)
            w = sum(1 for s in sigs if s.won)
            p = sum(s.pnl for s in sigs)
            print(f"\n  YES>{yes_t} / NO<{no_t}: ", end="")
            print(f"n={n}, win={w / n * 100:.1f}%, P&L={p:+.2f}")

    # ── 2. 复合评分 (含 BTC Range Position) ──
    print("\n" + "=" * 72)
    print("  2. 复合评分 (Formula A + BTC Range Position)")
    print("=" * 72)

    signals = run_backtest(events)

    if signals:
        print_summary(signals, f"score≥{CFG.score_entry}, w_btc_rp={CFG.w_btc_range_pos}")

        # 对比分析
        print_comparison([], signals)

        if args.verbose:
            print_detailed(signals)
    else:
        print("  无信号 (可能阈值过严)")

    # ── 3. 旧 Formula A 对照组 (不含 BTC Range Position) ──
    print("\n" + "=" * 72)
    print("  3. 对照组: 移除 BTC Range Position (等同于旧 Formula A)")
    print("=" * 72)

    old_weight = CFG.w_btc_range_pos
    CFG.w_btc_range_pos = 0  # 禁用新特征
    signals_old = run_backtest(events)
    CFG.w_btc_range_pos = old_weight  # 恢复

    if signals_old:
        print_summary(signals_old, "仅 7 特征 (无 btc_range_pos)")

        # 增量分析
        new_ids = {(s.time, s.side) for s in signals}
        old_ids = {(s.time, s.side) for s in signals_old}
        only_new = [s for s in signals if (s.time, s.side) in new_ids - old_ids]
        only_old = [s for s in signals_old if (s.time, s.side) in old_ids - new_ids]

        print(f"  btc_range_pos 带来的新增信号: {len(only_new)}")
        if only_new:
            w = sum(1 for s in only_new if s.won)
            p = sum(s.pnl for s in only_new)
            print(f"    胜率: {w / len(only_new) * 100:.1f}%, P&L: {p:+.2f}")
        print(f"  btc_range_pos 排除的旧信号: {len(only_old)}")
        if only_old:
            w = sum(1 for s in only_old if s.won)
            p = sum(s.pnl for s in only_old)
            print(f"    胜率: {w / len(only_old) * 100:.1f}%, P&L: {p:+.2f}")

    # ── 4. BTC Range Position 自身预测力 (全候选集) ──
    print("\n" + "=" * 72)
    print("  4. BTC Range Position 自身预测力 (全部穿越候选)")
    print("=" * 72)

    candidates = []
    for event in events:
        if event.get("hist_avg_range") is None:
            continue
        for side in ("yes", "no"):
            this_key = "yes_price" if side == "yes" else "no_price"
            other_key = "no_price" if side == "yes" else "yes_price"
            snaps = event["snapshots"]

            for i, s in enumerate(snaps):
                if (s[this_key] > CFG.trigger_threshold
                        and s["remaining_sec"] < CFG.max_remaining_sec
                        and i >= CFG.min_pre_snaps):
                    pre_prices = [x["price"] for x in snaps[: i + 1]]
                    pre_high = max(pre_prices)
                    pre_low = min(pre_prices)
                    btc_rp = btc_range_position(s["price"], pre_high, pre_low)

                    entry_price = s[other_key]
                    won = (event["outcome"] == 1) if side == "yes" else (event["outcome"] == 0)
                    pnl = (1.0 - entry_price) if won else (0.0 - entry_price)

                    candidates.append({
                        "side": side,
                        "btc_rp": btc_rp,
                        "entry_price": entry_price,
                        "won": won,
                        "pnl": pnl,
                    })
                    break  # 每 side 取首次穿越

    print(f"  总候选: {len(candidates)}")
    print(f"\n  {'BTC RP 区间':>20s} {'N':>5s} {'Flip%':>7s} {'Avg P&L':>9s}")
    print(f"  {'─'*48}")
    for lo, hi in [(0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5),
                   (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0)]:
        sub = [c for c in candidates if lo <= c["btc_rp"] < hi]
        if sub:
            w = sum(1 for c in sub if c["won"])
            p = sum(c["pnl"] for c in sub)
            print(f"  [{lo:.1f}-{hi:.1f}){'':>8s} {len(sub):>5d} {w/len(sub)*100:>6.1f}% {p/len(sub):>+9.4f}")

    # 按 side 分
    for side_lbl, side_val in [("YES>0.7 (赌DOWN)", "yes"), ("NO>0.7  (赌UP)", "no")]:
        sub = [c for c in candidates if c["side"] == side_val]
        w = sum(1 for c in sub if c["won"])
        p = sum(c["pnl"] for c in sub)
        print(f"\n  {side_lbl}: n={len(sub)}, flip_rate={w/len(sub)*100:.1f}%, avg_pnl={p/len(sub):+.4f}")

        for lo, hi in [(0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5),
                       (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0)]:
            sub2 = [c for c in sub if lo <= c["btc_rp"] < hi]
            if sub2:
                w2 = sum(1 for c in sub2 if c["won"])
                p2 = sum(c["pnl"] for c in sub2)
                bar = "█" * int(w2 / len(sub2) * 50) if len(sub2) >= 3 else ""
                print(f"    [{lo:.1f}-{hi:.1f}): n={len(sub2):>4d}, flip={w2/len(sub2)*100:>5.1f}%, avg={p2/len(sub2):>+7.4f}  {bar}")

    print("\nDone!")


if __name__ == "__main__":
    main()
