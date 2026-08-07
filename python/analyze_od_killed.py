#!/usr/bin/env python3
"""
精确分析: 被 other_delta < 0.03 拦住的 210 个候选,
如果去掉这道硬过滤, 有多少能过 score ≥ 5?
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_flip_config import FlipBacktestConfig, DEFAULT_CONFIG
from backtest_flip_utils import (
    load_events_from_dir, compute_hist_avg_range,
    compute_path_efficiency, compute_pre_range, compute_noise_ratio,
    compute_flips, is_oscillating, compute_range_expansion,
    compute_btc_position, compute_other_delta,
)

events = load_events_from_dir("../data/lab/")
cfg = DEFAULT_CONFIG

# Pass 1: find all candidates that reach other_delta stage (pass all other hard filters)
# and track what happens at scoring

killed_by_od = []  # other_delta < 0.03
would_pass_scoring = []  # would score >= 5 if not hard-filtered
would_fail_scoring = []  # would still fail score < 5

for event in events:
    if event.get("hist_avg_range") is None:
        continue

    for side in ("yes", "no"):
        this_key = "yes_price" if side == "yes" else "no_price"
        other_key = "no_price" if side == "yes" else "yes_price"
        snaps = event["snapshots"]

        # Find crossing
        cross_idx = None
        for i, s in enumerate(snaps):
            if s[this_key] > cfg.trigger_threshold:
                cross_idx = i
                break

        if cross_idx is None or cross_idx < cfg.min_pre_snaps:
            continue

        cross_snap = snaps[cross_idx]
        pre_prices = [s["price"] for s in snaps[: cross_idx + 1]]
        open_price = event["open_price"]

        net_move = abs(cross_snap["price"] - open_price)
        pre_high, pre_low, pre_range = compute_pre_range(pre_prices)
        if pre_range == 0:
            continue

        path_eff = compute_path_efficiency(pre_prices, open_price)
        noise_ratio_val = compute_noise_ratio(pre_prices, net_move)

        # Other hard filters
        if noise_ratio_val > 3.0:
            continue
        if path_eff < 0.4:
            continue

        range_expansion = compute_range_expansion(cross_snap["price"], open_price,
                                                  event.get("hist_avg_range"))
        if range_expansion is not None and range_expansion >= cfg.range_exp_max:
            continue

        # Now at other_delta stage
        other_delta = compute_other_delta(snaps, cross_idx, other_key, cfg)

        if other_delta >= 0.03:
            continue  # not killed by this filter

        # This candidate is killed by other_delta < 0.03
        # Now compute what its score WOULD be
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

        # Determine if would win
        if side == "yes":
            won = (event["outcome"] == 1)
        else:
            won = (event["outcome"] == 0)
        pnl_1share = (1.0 - entry_price) if won else (0.0 - entry_price)

        record = {
            "event_time": event["start_time"],
            "side": side,
            "other_delta": other_delta,
            "score": score,
            "entry_price": entry_price,
            "won": won,
            "pnl_1share": pnl_1share,
            "oscillating": oscillating,
            "path_eff": path_eff,
            "noise_ratio": noise_ratio_val,
            "range_expansion": range_expansion,
            "btc_extreme": btc_extreme,
        }

        if score >= cfg.score_entry:
            would_pass_scoring.append(record)
        else:
            would_fail_scoring.append(record)

        killed_by_od.append(record)

# ── Summary ──
print("=" * 75)
print("  被 other_delta < 0.03 拦住的 210 个候选 — 去掉硬过滤后的命运")
print("=" * 75)
print(f"  总计被拦:                {len(killed_by_od):>5d}")
print(f"  去掉硬过滤后 score ≥ 5:   {len(would_pass_scoring):>5d}  ← 真正新增信号")
print(f"  去掉硬过滤后 score < 5:   {len(would_fail_scoring):>5d}  ← 仍被评分拦下")
print()
print(f"  关键: 210 个中只有 {len(would_pass_scoring)} 个能过评分线!")
print(f"  因为 other_delta < 0.03 最多只能拿 +2 (other_delta_weak)")
print(f"  甚至 0 (other_delta ≤ 0.01)，需要其他维度凑够 5 分。")
print()

# Breakdown of would_pass
if would_pass_scoring:
    print("=" * 75)
    print(f"  去掉硬过滤后能过 score≥5 的 {len(would_pass_scoring)} 个信号:")
    print("=" * 75)
    print(f"  {'time':>12s}  {'side':>4s}  {'other_d':>8s}  {'score':>5s}  {'entry':>7s}  {'osc':>5s}  {'path_eff':>8s}  {'noise':>7s}  {'range_exp':>9s}  {'btc_ext':>7s}  {'would':>6s}")
    print("-" * 105)
    for s in sorted(would_pass_scoring, key=lambda x: x["score"], reverse=True):
        re_str = f"{s['range_expansion']:.2f}" if s['range_expansion'] is not None else "None"
        print(f"  {s['event_time']:>12d}  {s['side']:>4s}  {s['other_delta']:>+8.4f}  {s['score']:>5d}  {s['entry_price']:>7.3f}  {str(s['oscillating']):>5s}  {s['path_eff']:>8.3f}  {s['noise_ratio']:>7.2f}  {re_str:>9s}  {str(s['btc_extreme']):>7s}  {'WIN' if s['won'] else 'LOSE':>6s}")

    # P&L breakdown
    wins = sum(1 for s in would_pass_scoring if s['won'])
    total_pnl = sum(s['pnl_1share'] for s in would_pass_scoring)
    print(f"\n  胜率: {wins}/{len(would_pass_scoring)} ({wins/len(would_pass_scoring)*100:.1f}%)  P&L: {total_pnl:+.2f}")

# Score distribution of the 210
print()
print("=" * 75)
print("  被拦 210 个的评分分布:")
print("=" * 75)
for lo, hi in [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 99)]:
    subset = [s for s in killed_by_od if lo <= s["score"] < hi]
    if subset:
        wins = sum(1 for s in subset if s["won"])
        pnl = sum(s["pnl_1share"] for s in subset)
        print(f"  Score {lo}-{hi}: n={len(subset):>3d}, would_win={wins}/{len(subset)}, would_P&L={pnl:+.2f}")

# Detailed: why each of the 210 fails scoring
print()
print("=" * 75)
print("  评分维度拆解: 被拦的 210 个各项得分情况")
print("=" * 75)

# How many get each scoring component
n_od_strong = sum(1 for s in killed_by_od if s["other_delta"] > cfg.other_delta_strong)
n_od_weak = sum(1 for s in killed_by_od if cfg.other_delta_weak < s["other_delta"] <= cfg.other_delta_strong)
n_od_none = sum(1 for s in killed_by_od if s["other_delta"] <= cfg.other_delta_weak)
n_osc = sum(1 for s in killed_by_od if s["oscillating"])
n_cheap_strong = sum(1 for s in killed_by_od if s["entry_price"] < cfg.entry_cheap_strong)
n_cheap_weak = sum(1 for s in killed_by_od if cfg.entry_cheap_strong <= s["entry_price"] < cfg.entry_cheap_weak)
n_range = sum(1 for s in killed_by_od if s["range_expansion"] is not None and s["range_expansion"] < cfg.range_exp_threshold)
n_btc_ext = sum(1 for s in killed_by_od if s["btc_extreme"])

print(f"  other_delta > {cfg.other_delta_strong} (+{cfg.w_other_d5_strong}): {n_od_strong}")
print(f"  other_delta > {cfg.other_delta_weak} (+{cfg.w_other_d5_weak}):  {n_od_weak}")
print(f"  other_delta ≤ {cfg.other_delta_weak} (+0):     {n_od_none}")
print(f"  oscillating (+{cfg.w_oscillating}):              {n_osc}")
print(f"  entry < {cfg.entry_cheap_strong} (+{cfg.w_cheap_entry_strong}):         {n_cheap_strong} (weight=0!)")
print(f"  entry < {cfg.entry_cheap_weak} (+{cfg.w_cheap_entry_weak}):         {n_cheap_weak}")
print(f"  range_exp < {cfg.range_exp_threshold} (+{cfg.w_range_expansion}):        {n_range}")
print(f"  btc_extreme (+{cfg.w_btc_extreme}):              {n_btc_ext}")
