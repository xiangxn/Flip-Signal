#!/usr/bin/env python3
"""分析 remaining_sec 对胜率的影响"""
import argparse
import sys, os
from pathlib import Path
os.chdir(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_flip_config import FlipBacktestConfig, DEFAULT_CONFIG, ETH_CONFIG
from backtest_flip_utils import (
    load_events_from_dir, compute_hist_avg_range,
    compute_path_efficiency, compute_pre_range, compute_noise_ratio,
    compute_flips, is_oscillating, compute_range_expansion,
    compute_btc_position, compute_other_delta,
)

parser = argparse.ArgumentParser(description="remaining_sec 分析")
parser.add_argument("--data", default="../data/btc/", help="JSONL 数据目录 (默认: ../data/btc/)")
parser.add_argument("--profile", choices=["btc", "eth"], default="btc", help="参数预设 (默认: btc)")
args = parser.parse_args()

cfg = DEFAULT_CONFIG if args.profile == "btc" else ETH_CONFIG
events = load_events_from_dir(args.data)

candidates = []
for event in events:
    if event.get("hist_avg_range") is None:
        continue
    for side in ("yes", "no"):
        this_key = "yes_price" if side == "yes" else "no_price"
        other_key = "no_price" if side == "yes" else "yes_price"
        snaps = event["snapshots"]
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
        _, _, pre_range = compute_pre_range(pre_prices)
        if pre_range == 0:
            continue
        path_eff = compute_path_efficiency(pre_prices, open_price)
        noise_ratio_val = compute_noise_ratio(pre_prices, net_move)
        if noise_ratio_val > cfg.noise_ratio_veto_max:
            continue
        if path_eff < cfg.path_eff_veto_min:
            continue
        range_expansion = compute_range_expansion(cross_snap["price"], open_price, event.get("hist_avg_range"))
        if range_expansion is not None and range_expansion >= cfg.range_exp_max:
            continue

        other_delta = compute_other_delta(snaps, cross_idx, other_key, cfg)
        flips_val = compute_flips(pre_prices)
        oscillating = is_oscillating(pre_prices, open_price, cfg)
        btc_position = compute_btc_position(cross_snap["price"], open_price, event.get("hist_avg_range", 0))
        if side == "yes":
            btc_extreme = (btc_position < cfg.btc_pos_min)
            won = (event["outcome"] == 1)
        else:
            btc_extreme = (btc_position > cfg.btc_pos_max)
            won = (event["outcome"] == 0)
        entry_price = cross_snap[other_key]

        # Compute score with Formula A
        score = 0
        if other_delta > cfg.other_delta_vstrong:
            score += cfg.w_other_d5_vstrong
        elif other_delta > cfg.other_delta_strong:
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

        remaining = cross_snap["remaining_sec"]
        pnl = (1.0 - entry_price) if won else (0.0 - entry_price)

        if score >= 5:
            candidates.append({
                "remaining_sec": remaining,
                "score": score,
                "won": won,
                "pnl": pnl,
                "side": side,
                "entry_price": entry_price,
                "oscillating": oscillating,
                "other_delta": other_delta,
            })

print(f"Formula A 信号总数: {len(candidates)}")
print()
print(f"{'rem_sec':>10s}  {'n':>5s}  {'win%':>7s}  {'P&L':>9s}  {'cum_n':>6s}  {'cum_win%':>8s}  {'cum_P&L':>9s}")
print("-" * 75)

# Sort by remaining_sec ascending (closer to close = better)
sorted_c = sorted(candidates, key=lambda x: x["remaining_sec"])

for bucket_desc, bucket_pred in [
    ("0-60s", lambda r: 0 <= r <= 60),
    ("60-120s", lambda r: 60 < r <= 120),
    ("120-180s", lambda r: 120 < r <= 180),
    ("180-240s", lambda r: 180 < r <= 240),
    ("240-270s", lambda r: 240 < r <= 270),
    ("270-300s", lambda r: 270 < r <= 300),
]:
    sub = [c for c in candidates if bucket_pred(c["remaining_sec"])]
    if sub:
        w = sum(1 for c in sub if c["won"])
        p = sum(c["pnl"] for c in sub)
        # Cumulative from close to far
        cum = [c for c in sorted_c if c["remaining_sec"] <= max(c["remaining_sec"] for c in sub)]
        cw = sum(1 for c in cum if c["won"])
        cp = sum(c["pnl"] for c in cum)
        print(f"{bucket_desc:>10s}  {len(sub):>5d}  {w/len(sub)*100:>6.1f}%  {p:>+9.2f}  {len(cum):>6d}  {cw/len(cum)*100:>7.1f}%  {cp:>+9.2f}")

# Hard filter analysis
print()
print("=" * 60)
print("  硬过滤阈值扫描: remaining_sec > X → 过滤")
print("=" * 60)
print(f"{'veto >':>10s}  {'signals':>7s}  {'win%':>8s}  {'P&L':>9s}  {'filtered':>8s}  {'filt_P&L':>9s}")
print("-" * 65)

for veto in [300, 290, 280, 270, 260, 250, 240, 230, 220, 210, 200]:
    kept = [c for c in candidates if c["remaining_sec"] <= veto]
    filt = [c for c in candidates if c["remaining_sec"] > veto]
    if kept:
        w = sum(1 for c in kept if c["won"])
        p = sum(c["pnl"] for c in kept)
        fp = sum(c["pnl"] for c in filt) if filt else 0
        print(f"{veto:>10d}  {len(kept):>7d}  {w/len(kept)*100:>7.1f}%  {p:>+9.2f}  {len(filt):>8d}  {fp:>+9.2f}")

# Scoring approach analysis
print()
print("=" * 60)
print("  评分方法: remaining_sec 越大 → 扣分")
print("=" * 60)
print(f"{'扣分规则':>25s}  {'signals':>7s}  {'win%':>8s}  {'P&L':>9s}")
print("-" * 65)

for desc, penalty_func in [
    (">240 → -1", lambda r: -1 if r > 240 else 0),
    (">250 → -1", lambda r: -1 if r > 250 else 0),
    (">260 → -1", lambda r: -1 if r > 260 else 0),
    (">240 → -2", lambda r: -2 if r > 240 else 0),
    (">250 → -2", lambda r: -2 if r > 250 else 0),
    ("分段: >240→-1, >270→-2", lambda r: -2 if r > 270 else (-1 if r > 240 else 0)),
]:
    new_candidates = []
    for c in candidates:
        adj_score = c["score"] + penalty_func(c["remaining_sec"])
        if adj_score >= 5:
            new_candidates.append({**c, "adj_score": adj_score})
    if new_candidates:
        w = sum(1 for c in new_candidates if c["won"])
        p = sum(c["pnl"] for c in new_candidates)
        print(f"{desc:>25s}  {len(new_candidates):>7d}  {w/len(new_candidates)*100:>7.1f}%  {p:>+9.2f}")

# Best: hard filter >250 + show detail
print()
print("=" * 60)
print("  推荐: 硬过滤 remaining_sec > 250")
print("=" * 60)
kept = [c for c in candidates if c["remaining_sec"] <= 250]
killed = [c for c in candidates if c["remaining_sec"] > 250]
wk = sum(1 for c in kept if c["won"])
pk = sum(c["pnl"] for c in kept)
wkd = sum(1 for c in killed if c["won"])
pkd = sum(c["pnl"] for c in killed)
print(f"  保留: {len(kept)} signals, {wk/len(kept)*100:.1f}% WR, P&L={pk:+.2f}")
print(f"  过滤: {len(killed)} signals, {wkd/len(killed)*100:.1f}% WR, P&L={pkd:+.2f}")
if killed:
    print(f"\n  被过滤的信号:")
    print(f"  {'rem':>5s} {'side':>4s} {'score':>5s} {'P&L':>+7s}")
    for c in sorted(killed, key=lambda x: x["remaining_sec"]):
        print(f"  {c['remaining_sec']:>5d} {c['side']:>4s} {c['score']:>5d} {c['pnl']:>+7.2f}")
