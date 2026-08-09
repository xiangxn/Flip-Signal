#!/usr/bin/env python3
"""
Detailed analysis: how many signals are killed by each filter stage.
"""

import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_flip_config import FlipBacktestConfig, DEFAULT_CONFIG, ETH_CONFIG
from backtest_flip_utils import (
    load_events_from_dir, compute_hist_avg_range,
    compute_path_efficiency, compute_pre_range, compute_noise_ratio,
    compute_flips, is_oscillating, compute_range_expansion,
    compute_btc_position, compute_other_delta,
)

parser = argparse.ArgumentParser(description="过滤管线分析")
parser.add_argument("--data", default="../data/btc/", help="JSONL 数据目录 (默认: ../data/btc/)")
parser.add_argument("--profile", choices=["btc", "eth"], default="btc", help="参数预设 (默认: btc)")
args = parser.parse_args()

cfg = DEFAULT_CONFIG if args.profile == "btc" else ETH_CONFIG
events = load_events_from_dir(args.data)

# Track filter stats
stats = {
    "total_checks": 0,        # all side checks attempted
    "no_cross": 0,
    "min_pre_snaps": 0,
    "pre_range_zero": 0,
    "noise_gt_3": 0,
    "path_eff_lt_0.4": 0,
    "range_exp_max": 0,
    "other_delta_lt_003": 0,
    "score_lt_5": 0,
    "passed": 0,
}

# Track details of signals killed by other_delta filter
killed_by_other_delta = []
passed_all = []

for event in events:
    if event.get("hist_avg_range") is None:
        continue

    for side in ("yes", "no"):
        stats["total_checks"] += 1

        this_key = "yes_price" if side == "yes" else "no_price"
        other_key = "no_price" if side == "yes" else "yes_price"
        snaps = event["snapshots"]

        # Find crossing
        cross_idx = None
        for i, s in enumerate(snaps):
            if s[this_key] > cfg.trigger_threshold:
                cross_idx = i
                break

        if cross_idx is None:
            stats["no_cross"] += 1
            continue
        if cross_idx < cfg.min_pre_snaps:
            stats["min_pre_snaps"] += 1
            continue

        cross_snap = snaps[cross_idx]
        pre_prices = [s["price"] for s in snaps[: cross_idx + 1]]
        open_price = event["open_price"]

        net_move = abs(cross_snap["price"] - open_price)
        pre_high, pre_low, pre_range = compute_pre_range(pre_prices)
        if pre_range == 0:
            stats["pre_range_zero"] += 1
            continue

        path_eff = compute_path_efficiency(pre_prices, open_price)
        noise_ratio_val = compute_noise_ratio(pre_prices, net_move)

        # Track which filters block
        if noise_ratio_val > cfg.noise_ratio_veto_max:
            stats["noise_gt_3"] += 1
            continue
        if path_eff < cfg.path_eff_veto_min:
            stats["path_eff_lt_0.4"] += 1
            continue

        range_expansion = compute_range_expansion(cross_snap["price"], open_price,
                                                  event.get("hist_avg_range"))
        if range_expansion is not None and range_expansion >= cfg.range_exp_max:
            stats["range_exp_max"] += 1
            continue

        # Now check other_delta
        other_delta = compute_other_delta(snaps, cross_idx, other_key, cfg)

        if other_delta < 0.03:
            stats["other_delta_lt_003"] += 1
            killed_by_other_delta.append({
                "event_time": event["start_time"],
                "side": side,
                "other_delta": other_delta,
                "path_eff": path_eff,
                "noise_ratio": noise_ratio_val,
                "range_expansion": range_expansion,
                "entry_price": cross_snap[other_key],
                "outcome": event["outcome"],
            })
            continue

        # Check scoring
        # Quick scoring check (simplified)
        flips_val = compute_flips(pre_prices)
        oscillating = is_oscillating(pre_prices, open_price, cfg)
        btc_position = compute_btc_position(cross_snap["price"], open_price,
                                            event.get("hist_avg_range", 0))
        if side == "yes":
            btc_extreme = (cfg.btc_pos_min < btc_position < 0)
        else:
            btc_extreme = (0 < btc_position < cfg.btc_pos_max)
        entry_price = cross_snap[other_key]

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
            stats["score_lt_5"] += 1
            continue

        stats["passed"] += 1

        # Determine if would have won
        if side == "yes":
            won = (event["outcome"] == 1)
        else:
            won = (event["outcome"] == 0)
        pnl = (1.0 - entry_price) if won else (0.0 - entry_price)

        passed_all.append({
            "event_time": event["start_time"],
            "side": side,
            "score": score,
            "other_delta": other_delta,
            "entry_price": entry_price,
            "won": won,
            "pnl": pnl,
        })

# Print filter pipeline analysis
print("=" * 70)
print("  Signal Pipeline Filter Analysis")
print("=" * 70)
total = stats["total_checks"]
print(f"  Total side-checks:           {total:>5d}")
remaining = total
for label, key in [
    ("No >0.7 crossing", "no_cross"),
    ("Too few pre-snaps", "min_pre_snaps"),
    ("pre_range == 0", "pre_range_zero"),
    ("noise_ratio > 3.0", "noise_gt_3"),
    ("path_eff < 0.4", "path_eff_lt_0.4"),
    ("range_exp >= 2.0 (F0 veto)", "range_exp_max"),
    ("🔴 other_delta < 0.03", "other_delta_lt_003"),
    ("score < 5", "score_lt_5"),
]:
    n = stats[key]
    pct = n / total * 100
    remaining -= n
    bar = "█" * int(pct / 2)
    print(f"  {label:30s} {n:>5d} ({pct:>5.1f}%) {bar}")
print(f"  {'✅ PASSED':30s} {stats['passed']:>5d}")
print()

# Analyze killed by other_delta
print("=" * 70)
print(f"  Signals KILLED by other_delta < 0.03: {len(killed_by_other_delta)}")
print("=" * 70)

if killed_by_other_delta:
    # Check how many would have been profitable
    n_would_win = 0
    would_pnl = 0.0
    for s in killed_by_other_delta:
        entry = s["entry_price"]
        if s["side"] == "yes":
            won = (s["outcome"] == 1)
        else:
            won = (s["outcome"] == 0)
        if won:
            n_would_win += 1
            would_pnl += (1.0 - entry)
        else:
            would_pnl += (0.0 - entry)

    print(f"  Of these, would have won: {n_would_win}/{len(killed_by_other_delta)} ({n_would_win/len(killed_by_other_delta)*100:.1f}%)")
    print(f"  Would-be P&L (1 share each): {would_pnl:+.2f}")
    print()

    # Distribution of other_delta among killed signals
    print(f"  Distribution of other_delta among killed signals:")
    bins = [
        ("< -0.02", lambda d: d < -0.02),
        ("-0.02 to -0.01", lambda d: -0.02 <= d < -0.01),
        ("-0.01 to 0.0", lambda d: -0.01 <= d < 0.0),
        ("0.0 to 0.01", lambda d: 0.0 <= d < 0.01),
        ("0.01 to 0.02", lambda d: 0.01 <= d < 0.02),
        ("0.02 to 0.03", lambda d: 0.02 <= d < 0.03),
    ]
    for label, pred in bins:
        subset = [s for s in killed_by_other_delta if pred(s["other_delta"])]
        if subset:
            wins = sum(1 for s in subset if (
                (s["side"] == "yes" and s["outcome"] == 1) or
                (s["side"] == "no" and s["outcome"] == 0)
            ))
            pnl = sum(
                (1.0 - s["entry_price"]) if (
                    (s["side"] == "yes" and s["outcome"] == 1) or
                    (s["side"] == "no" and s["outcome"] == 0)
                ) else (0.0 - s["entry_price"])
                for s in subset
            )
            print(f"    {label:20s}: n={len(subset):>2d}, would_win={wins}/{len(subset)}, would_P&L={pnl:+.2f}")

print()

# Show each killed signal
print(f"  Individual killed signals:")
print(f"  {'time':>12s}  {'side':>4s}  {'other_d':>8s}  {'entry':>7s}  {'path_eff':>8s}  {'noise':>7s}  {'range_exp':>9s}  {'outcome':>8s}")
print(f"  {'-'*80}")
for s in sorted(killed_by_other_delta, key=lambda x: x["other_delta"]):
    outcome_str = "UP" if s["outcome"] == 0 else "DOWN"
    entry = s["entry_price"]
    would_win = (s["side"] == "yes" and s["outcome"] == 1) or (s["side"] == "no" and s["outcome"] == 0)
    win_str = "WIN" if would_win else "LOSE"
    re_str = f"{s['range_expansion']:.2f}" if s['range_expansion'] is not None else "None"
    print(f"  {s['event_time']:>12d}  {s['side']:>4s}  {s['other_delta']:>+8.4f}  {entry:>7.3f}  {s['path_eff']:>8.3f}  {s['noise_ratio']:>7.2f}  {re_str:>9s}  {outcome_str:>5s} {win_str}")
