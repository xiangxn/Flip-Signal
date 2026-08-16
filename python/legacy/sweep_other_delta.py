#!/usr/bin/env python3
"""
Sweep other_delta hard filter thresholds to find optimal setting.
"""

import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_flip_config import FlipBacktestConfig, DEFAULT_CONFIG, ETH_CONFIG
from backtest_flip_utils import load_events_from_dir, run_backtest

parser = argparse.ArgumentParser(description="other_delta 阈值扫描")
parser.add_argument("--data", default="../data/btc/", help="JSONL 数据目录 (默认: ../data/btc/)")
parser.add_argument("--profile", choices=["btc", "eth"], default="btc", help="参数预设 (默认: btc)")
args = parser.parse_args()

cfg = DEFAULT_CONFIG if args.profile == "btc" else ETH_CONFIG

# Load data once
events = load_events_from_dir(args.data)
print(f"Loaded {len(events)} events")

# Sweep other_delta thresholds
thresholds = [0.03, 0.02, 0.01, 0.005, 0.0, -0.005, -0.01, -0.015, -0.02, -0.03, -0.05, -100.0]

print(f"\n{'threshold':>10s}  {'signals':>7s}  {'win_rate':>8s}  {'total_pnl':>9s}  {'avg_score':>9s}  {'avg_entry':>9s}  {'win/loss':>8s}")
print("-" * 85)

for thresh in thresholds:
    cfg = FlipBacktestConfig()
    # We need to patch the hard filter. The simplest way: set other_delta_strong/weak
    # But the hard filter is hardcoded in check_signal at line 273.
    # Let's instead temporarily patch it via monkey-patching.

# Actually, the hard filter is hardcoded. Let's just modify the source temporarily
# or test by setting other_delta_weak=thresh and other_delta_strong higher.
# But the HARD FILTER at line 273 kills everything below 0.03 regardless.
#
# Let's just read the code and do a proper sweep by temporarily editing the file.
# Better approach: write a standalone sweep that bypasses the hard filter.

import json
import math
from dataclasses import dataclass
from typing import Any

# Re-implement key pieces with configurable hard filter
from backtest_flip_utils import (
    compute_path_efficiency, compute_pre_range, compute_noise_ratio,
    compute_flips, is_oscillating, compute_range_expansion,
    compute_btc_position, compute_other_delta, FlipSignal,
    compute_hist_avg_range,
)
from backtest_flip_config import FlipBacktestConfig, DEFAULT_CONFIG

@dataclass
class SweepResult:
    threshold: float
    signals: int
    wins: int
    total_pnl: float
    avg_score: float
    avg_entry: float

def check_signal_sweep(event, side, cfg, other_delta_hard_filter):
    """Patched version with configurable other_delta hard filter."""
    this_key = "yes_price" if side == "yes" else "no_price"
    other_key = "no_price" if side == "yes" else "yes_price"
    snaps = event["snapshots"]

    # Step 1: find first >0.7 crossing
    cross_idx = None
    for i, s in enumerate(snaps):
        if s[this_key] > cfg.trigger_threshold:
            cross_idx = i
            break

    if cross_idx is None or cross_idx < cfg.min_pre_snaps:
        return None

    cross_snap = snaps[cross_idx]
    pre_prices = [s["price"] for s in snaps[: cross_idx + 1]]
    open_price = event["open_price"]

    # Path efficiency
    net_move = abs(cross_snap["price"] - open_price)
    pre_high, pre_low, pre_range = compute_pre_range(pre_prices)
    if pre_range == 0:
        return None

    path_eff = compute_path_efficiency(pre_prices, open_price)
    noise_ratio_val = compute_noise_ratio(pre_prices, net_move)
    flips_val = compute_flips(pre_prices)
    oscillating = is_oscillating(pre_prices, open_price, cfg)

    # Hard filters (keeping these)
    if noise_ratio_val > cfg.noise_ratio_veto_max:
        return None
    if path_eff < cfg.path_eff_veto_min:
        return None

    # Range expansion
    range_expansion = compute_range_expansion(cross_snap["price"], open_price,
                                              event.get("hist_avg_range"))
    if range_expansion is not None and range_expansion >= cfg.range_exp_max:
        return None

    # BTC position
    btc_position = compute_btc_position(cross_snap["price"], open_price,
                                        event.get("hist_avg_range", 0))
    if side == "yes":
        btc_extreme = (cfg.btc_pos_min < btc_position < 0)
    else:
        btc_extreme = (0 < btc_position < cfg.btc_pos_max)

    entry_price = cross_snap[other_key]

    # T+5s confirmation
    other_delta = compute_other_delta(snaps, cross_idx, other_key, cfg)

    # ── PATCHED: configurable hard filter ──
    if other_delta < other_delta_hard_filter:
        return None

    # Scoring
    score = 0
    if other_delta > cfg.other_delta_strong:
        score += cfg.w_other_d5_strong
    elif other_delta > cfg.other_delta_weak:
        score += cfg.w_other_d5_weak

    if oscillating:
        score += cfg.w_oscillating

    if entry_price < cfg.entry_cheap_strong:
        score += cfg.w_cheap_entry_strong
    elif entry_price < cfg.entry_cheap_weak:
        score += cfg.w_cheap_entry_weak

    if range_expansion is not None and range_expansion < cfg.range_exp_threshold:
        score += cfg.w_range_expansion

    if btc_extreme:
        score += cfg.w_btc_extreme

    if score < cfg.score_entry:
        return None

    shares = 2 if score >= cfg.score_add else 1

    if side == "yes":
        won = (event["outcome"] == 1)
    else:
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


def run_sweep(events, cfg, other_delta_hard_filter):
    compute_hist_avg_range(events, window_N=cfg.hist_window_N)

    signals = []
    for event in events:
        if event.get("hist_avg_range") is None:
            continue
        for side in ("yes", "no"):
            signal = check_signal_sweep(event, side, cfg, other_delta_hard_filter)
            if signal is not None:
                signals.append(signal)
                break  # 每事件最多一注
    return signals


# Run sweep (cfg already set from --profile args above)

print(f"\n[{args.profile.upper()}] other_delta 阈值扫描")
print(f"{'other_delta':>12s}  {'signals':>7s}  {'win_rate':>8s}  {'total_pnl':>9s}  {'avg_score':>7s}  {'avg_entry':>9s}  {'win/loss':>9s}  {'avg_other_d':>11s}")
print("-" * 100)

for thresh in [-100.0, -0.05, -0.03, -0.02, -0.015, -0.01, -0.005, 0.0, 0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.05]:
    sigs = run_sweep(events, cfg, thresh)
    if sigs:
        n = len(sigs)
        wins = sum(1 for s in sigs if s.won)
        total_pnl = sum(s.pnl for s in sigs)
        avg_score = sum(s.score for s in sigs) / n
        avg_entry = sum(s.entry_price for s in sigs) / n
        avg_other_d = sum(s.other_delta for s in sigs) / n
        print(f"{thresh:>12.3f}  {n:>7d}  {wins/n*100:>7.1f}%  {total_pnl:>+9.2f}  {avg_score:>7.1f}  {avg_entry:>9.3f}  {wins:>3d}/{n-wins:<3d}    {avg_other_d:>+11.4f}")
    else:
        print(f"{thresh:>12.3f}  {0:>7d}  {'N/A':>8s}  {'N/A':>9s}  {'N/A':>7s}  {'N/A':>9s}  {'N/A':>9s}")
