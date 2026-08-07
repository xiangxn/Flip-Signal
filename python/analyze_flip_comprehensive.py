#!/usr/bin/env python3
"""
Comprehensive Flip Analysis - Fresh Start
==========================================
Analyze when one side of the Polymarket BTC 5m market exceeds 0.7,
and whether the market eventually "flips" (the opposite outcome happens).

Core question: When yes_price or no_price > 0.7, does that side actually win?
If not, can we predict the flip?

outcome mapping:
  0 = YES token won (BTC went UP)
  1 = NO token won (BTC went DOWN)
"""

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy import stats

# --- Data Loading ---

DATA_DIR = Path(__file__).parent.parent / "data" / "lab"

def load_all_events():
    """Load all events from data/lab/"""
    events = []
    for f in sorted(DATA_DIR.glob("events_*.jsonl")):
        with open(f) as fp:
            for line in fp:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    print(f"Loaded {len(events)} events from {len(list(DATA_DIR.glob('events_*.jsonl')))} files")
    return events


# --- Event Classification ---

def classify_event(event):
    """
    For each event, find:
    - First time yes_price > 0.7 (and at what remaining_sec)
    - First time no_price > 0.7
    - Max yes_price and when
    - Max no_price and when
    - Whether the favored side actually won
    """
    snaps = event["snapshots"]
    outcome = event["outcome"]  # 0=YES won, 1=NO won

    first_yes_gt_07 = None
    first_no_gt_07 = None
    max_yes = 0
    max_yes_at = None
    max_no = 0
    max_no_at = None

    # Track the full price series
    yes_series = []
    no_series = []
    remaining_series = []
    btc_prices = []
    btc_rets = []
    signed_flows = []
    vol_30s_series = []
    bid_ask_ratios = []

    for s in snaps:
        yp = s["yes_price"]
        np_ = s["no_price"]
        rem = s["remaining_sec"]

        yes_series.append(yp)
        no_series.append(np_)
        remaining_series.append(rem)
        btc_prices.append(s["price"])
        btc_rets.append(s["ret_10s"])
        signed_flows.append(s["signed_flow_5s"])
        vol_30s_series.append(s["vol_30s"])
        bid_ask_ratios.append(s["bid_depth"] / max(s["ask_depth"], 1e-10))

        if yp > 0.7 and first_yes_gt_07 is None:
            first_yes_gt_07 = rem
        if np_ > 0.7 and first_no_gt_07 is None:
            first_no_gt_07 = rem

        if yp > max_yes:
            max_yes = yp
            max_yes_at = rem
        if np_ > max_no:
            max_no = np_
            max_no_at = rem

    # Determine which side was "strong" (>0.7)
    yes_strong = max_yes > 0.7
    no_strong = max_no > 0.7

    # Did the favored side win?
    if yes_strong and not no_strong:
        favored = "YES"
        flip = outcome != 0  # flip if YES favored but NO won
    elif no_strong and not yes_strong:
        favored = "NO"
        flip = outcome != 1  # flip if NO favored but YES won
    elif yes_strong and no_strong:
        # Both sides were >0.7 at some point
        # Which was first?
        if first_yes_gt_07 < first_no_gt_07:  # YES crossed first
            favored = "YES"
            flip = outcome != 0
        else:
            favored = "NO"
            flip = outcome != 1
    else:
        favored = "NONE"
        flip = False

    # Determine the "target_side" for analysis - the side that went >0.7
    if yes_strong or no_strong:
        target_side = "yes" if yes_strong else "no"
    else:
        target_side = None

    return {
        "condition_id": event["condition_id"],
        "start_time": event["start_time"],
        "open_price": event["open_price"],
        "close_price": event["close_price"],
        "btc_change": event["close_price"] - event["open_price"],
        "btc_change_pct": (event["close_price"] - event["open_price"]) / event["open_price"] * 100,
        "outcome": outcome,
        "n_snapshots": len(snaps),
        "max_yes": max_yes,
        "max_no": max_no,
        "first_yes_gt_07": first_yes_gt_07,
        "first_no_gt_07": first_no_gt_07,
        "max_yes_at": max_yes_at,
        "max_no_at": max_no_at,
        "yes_strong": yes_strong,
        "no_strong": no_strong,
        "favored": favored,
        "flip": flip,
        "yes_series": yes_series,
        "no_series": no_series,
        "remaining_series": remaining_series,
        "btc_prices": btc_prices,
        "btc_rets": btc_rets,
        "signed_flows": signed_flows,
        "vol_30s_series": vol_30s_series,
        "bid_ask_ratios": bid_ask_ratios,
    }


# --- Feature Engineering at the Crossing Point ---

def extract_crossing_features(event_info):
    """
    At the FIRST moment one side exceeds 0.7, extract features
    that might predict whether this is a true signal or a flip.
    """
    if event_info["favored"] == "NONE":
        return None

    event_info_ = event_info  # alias

    # Determine the crossing point
    if event_info_["favored"] == "YES":
        cross_rem = event_info_["first_yes_gt_07"]
        target_price_series = event_info_["yes_series"]
        other_price_series = event_info_["no_series"]
    else:
        cross_rem = event_info_["first_no_gt_07"]
        target_price_series = event_info_["no_series"]
        other_price_series = event_info_["yes_series"]

    remaining_arr = np.array(event_info_["remaining_series"])
    # Find index of the crossing point
    cross_idx = np.argmin(np.abs(remaining_arr - cross_rem))

    if cross_idx >= len(remaining_arr):
        return None

    # --- Feature groups ---

    # 1. TIME: How early/late is the crossing?
    total_duration = remaining_arr[0]  # max remaining (should be ~295)
    time_remaining = cross_rem
    time_elapsed = total_duration - cross_rem
    time_pct = time_elapsed / total_duration if total_duration > 0 else 0

    # 2. PRICE AT CROSSING: How high is the favored side?
    target_price_at_cross = target_price_series[cross_idx]
    other_price_at_cross = other_price_series[cross_idx]

    # 3. BTC MOVEMENT up to crossing
    btc_prices = np.array(event_info_["btc_prices"])
    btc_at_cross = btc_prices[cross_idx]
    btc_open = event_info_["open_price"]
    btc_change_from_open = (btc_at_cross - btc_open) / btc_open  # pct

    # BTC direction relative to favored side
    # If YES favored, we expect BTC to be UP; if NO favored, BTC should be DOWN
    btc_direction = 1 if event_info_["favored"] == "YES" else -1
    btc_aligned = (btc_change_from_open * btc_direction) > 0

    # 4. BTC MOMENTUM at crossing
    btc_rets = np.array(event_info_["btc_rets"])

    # Short-term momentum (last few seconds)
    lookback_5s = max(0, cross_idx - 5)
    recent_rets = btc_rets[lookback_5s:cross_idx + 1]
    recent_rets = recent_rets[recent_rets != 0]  # filter zeros

    # 5. VOLATILITY
    vol_30s = np.array(event_info_["vol_30s_series"])
    vol_at_cross = vol_30s[cross_idx]
    vol_mean_before = np.mean(vol_30s[:cross_idx + 1]) if cross_idx > 0 else vol_at_cross
    vol_acceleration = vol_at_cross / vol_mean_before if vol_mean_before > 1e-10 else 1.0

    # 6. ORDER FLOW
    signed_flows = np.array(event_info_["signed_flows"])

    # Cumulative signed flow up to crossing
    cum_flow = np.sum(signed_flows[:cross_idx + 1])
    # Recent flow (last 10 seconds)
    recent_flow = np.sum(signed_flows[max(0, cross_idx - 10):cross_idx + 1])

    # Flow consistency: ratio of buy-dominated snapshots
    flow_direction = 1 if event_info_["favored"] == "YES" else -1
    n_recent = min(20, cross_idx + 1)
    recent_flows = signed_flows[max(0, cross_idx - n_recent):cross_idx + 1]
    aligned_flows = np.sum(recent_flows * flow_direction > 0)
    flow_consistency = aligned_flows / len(recent_flows) if len(recent_flows) > 0 else 0.5

    # 7. ORDER BOOK IMBALANCE
    bid_ask = np.array(event_info_["bid_ask_ratios"])
    ob_imbalance = bid_ask[cross_idx]
    # High bid/ask ratio = more bid depth (buying pressure)
    ob_mean = np.mean(bid_ask[:cross_idx + 1])

    # 8. SPEED OF CROSSING: How fast did the price go from 0.5 to 0.7?
    target_series = np.array(target_price_series)
    # Find when it first crossed 0.5
    cross_05_mask = target_series > 0.5
    if np.any(cross_05_mask):
        first_05_idx = np.argmax(cross_05_mask)
        if first_05_idx < cross_idx:
            speed_05_to_07 = (cross_idx - first_05_idx)  # in snapshots (seconds)
        else:
            speed_05_to_07 = 0  # crossed 0.5 and 0.7 at same time
    else:
        speed_05_to_07 = cross_idx  # never below 0.5

    # 9. PRICE ACCELERATION: How fast is the target price moving?
    if cross_idx >= 5:
        target_price_5s_ago = target_series[cross_idx - 5]
        target_price_change = target_price_at_cross - target_price_5s_ago
    else:
        target_price_change = target_price_at_cross - target_series[0] if cross_idx > 0 else 0

    # 10. PATH: How smooth was the rise?
    if cross_idx >= 5:
        segment = target_series[:cross_idx + 1]
        # Count reversals (drops) in the segment
        diffs = np.diff(segment)
        reversals = np.sum(diffs < -0.01)  # dropped by >1 cent
        # Total variation vs net change
        total_variation = np.sum(np.abs(diffs))
        net_change = segment[-1] - segment[0]
        path_inefficiency = total_variation / (abs(net_change) + 1e-10)
    else:
        reversals = 0
        path_inefficiency = 1.0

    # 11. OTHER SIDE BEHAVIOR
    other_series = np.array(other_price_series)
    other_at_cross = other_series[cross_idx]
    # Is the other side also rising? (suggesting uncertainty)
    if cross_idx >= 10:
        other_change = other_series[cross_idx] - other_series[cross_idx - 10]
    else:
        other_change = 0

    # 12. SPREAD
    spread = abs(target_price_at_cross - other_price_at_cross)

    return {
        "condition_id": event_info_["condition_id"],
        "favored": event_info_["favored"],
        "flip": event_info_["flip"],
        "outcome": event_info_["outcome"],

        # Time
        "time_remaining": time_remaining,
        "time_elapsed": time_elapsed,
        "time_pct": time_pct,

        # Price
        "target_price": target_price_at_cross,
        "other_price": other_price_at_cross,
        "spread": spread,

        # BTC
        "btc_change_pct": btc_change_from_open * 100,
        "btc_aligned": btc_aligned,
        "btc_momentum_5s": np.mean(recent_rets[-5:]) if len(recent_rets) >= 1 else 0,

        # Volatility
        "vol_at_cross": vol_at_cross,
        "vol_acceleration": vol_acceleration,

        # Order flow
        "cum_signed_flow": cum_flow,
        "recent_flow_10s": recent_flow,
        "flow_consistency": flow_consistency,

        # Order book
        "ob_imbalance": ob_imbalance,
        "ob_mean": ob_mean,

        # Speed/Path
        "speed_05_to_07": speed_05_to_07,
        "target_price_velocity": target_price_change,
        "reversals": reversals,
        "path_inefficiency": path_inefficiency,

        # Other side
        "other_change_10s": other_change,

        # BTC range position
        "btc_range_pct": None,  # computed later
    }


# --- Main Analysis ---

def main():
    print("=" * 80)
    print("COMPREHENSIVE FLIP ANALYSIS")
    print("=" * 80)

    events = load_all_events()
    event_infos = [classify_event(e) for e in events]

    # ---- PART 1: Overall Statistics ----
    print("\n" + "=" * 80)
    print("PART 1: OVERALL STATISTICS")
    print("=" * 80)

    total = len(event_infos)
    yes_strong_events = [ei for ei in event_infos if ei["yes_strong"]]
    no_strong_events = [ei for ei in event_infos if ei["no_strong"]]
    any_strong_events = [ei for ei in event_infos if ei["yes_strong"] or ei["no_strong"]]
    both_strong_events = [ei for ei in event_infos if ei["yes_strong"] and ei["no_strong"]]
    neither_strong_events = [ei for ei in event_infos if not ei["yes_strong"] and not ei["no_strong"]]

    print(f"\nTotal events: {total}")
    print(f"  YES > 0.7 at any point: {len(yes_strong_events)} ({len(yes_strong_events)/total*100:.1f}%)")
    print(f"  NO > 0.7 at any point:  {len(no_strong_events)} ({len(no_strong_events)/total*100:.1f}%)")
    print(f"  At least one side > 0.7: {len(any_strong_events)} ({len(any_strong_events)/total*100:.1f}%)")
    print(f"  Both sides > 0.7:        {len(both_strong_events)} ({len(both_strong_events)/total*100:.1f}%)")
    print(f"  Neither side > 0.7:      {len(neither_strong_events)} ({len(neither_strong_events)/total*100:.1f}%)")

    # Flip rates
    flips = [ei for ei in any_strong_events if ei["flip"]]
    non_flips = [ei for ei in any_strong_events if not ei["flip"]]
    print(f"\nFlip analysis (when one side > 0.7):")
    print(f"  Flips:     {len(flips)} ({len(flips)/len(any_strong_events)*100:.1f}%)")
    print(f"  Non-flips: {len(non_flips)} ({len(non_flips)/len(any_strong_events)*100:.1f}%)")

    # By favored side
    yes_flips = [ei for ei in flips if ei["favored"] == "YES"]
    no_flips = [ei for ei in flips if ei["favored"] == "NO"]
    print(f"\nFlip by favored side:")
    print(f"  YES favored flips: {len(yes_flips)} / {len([e for e in any_strong_events if e['favored']=='YES'])}")
    print(f"  NO favored flips:  {len(no_flips)} / {len([e for e in any_strong_events if e['favored']=='NO'])}")

    # ---- PART 2: Extract Crossing Features ----
    print("\n" + "=" * 80)
    print("PART 2: FEATURE ANALYSIS AT CROSSING POINT (>0.7)")
    print("=" * 80)

    all_features = []
    for ei in any_strong_events:
        feats = extract_crossing_features(ei)
        if feats:
            all_features.append(feats)

    flip_feats = [f for f in all_features if f["flip"]]
    noflip_feats = [f for f in all_features if not f["flip"]]

    print(f"\nFeature samples: {len(all_features)} total, {len(flip_feats)} flips, {len(noflip_feats)} non-flips")

    # Analyze each numeric feature
    numeric_keys = [
        "time_remaining", "time_elapsed", "time_pct",
        "target_price", "other_price", "spread",
        "btc_change_pct", "btc_momentum_5s",
        "vol_at_cross", "vol_acceleration",
        "cum_signed_flow", "recent_flow_10s", "flow_consistency",
        "ob_imbalance", "ob_mean",
        "speed_05_to_07", "target_price_velocity",
        "reversals", "path_inefficiency",
        "other_change_10s",
    ]

    print(f"\n{'Feature':<30} {'Flip Mean':>10} {'NoFlip Mean':>10} {'Diff':>10} {'T-stat':>8} {'P-value':>8}")
    print("-" * 82)

    significant_features = []

    for key in numeric_keys:
        flip_vals = [f[key] for f in flip_feats if f[key] is not None]
        noflip_vals = [f[key] for f in noflip_feats if f[key] is not None]

        if len(flip_vals) < 3 or len(noflip_vals) < 3:
            continue

        flip_mean = np.mean(flip_vals)
        noflip_mean = np.mean(noflip_vals)
        diff = flip_mean - noflip_mean

        try:
            t_stat, p_val = stats.ttest_ind(flip_vals, noflip_vals, equal_var=False)
        except:
            t_stat, p_val = 0, 1

        marker = ""
        if p_val < 0.05:
            marker = " *"
            significant_features.append((key, p_val, diff, flip_mean, noflip_mean))
        if p_val < 0.01:
            marker = " **"
        if p_val < 0.001:
            marker = " ***"

        print(f"{key:<30} {flip_mean:>10.4f} {noflip_mean:>10.4f} {diff:>10.4f} {t_stat:>8.2f} {p_val:>8.4f}{marker}")

    print("\n* p<0.05, ** p<0.01, *** p<0.001")
    print(f"\nSignificant features ({len(significant_features)}):")
    for key, p_val, diff, flip_m, noflip_m in sorted(significant_features, key=lambda x: x[1]):
        direction = "HIGHER" if diff > 0 else "LOWER"
        print(f"  {key}: p={p_val:.4f}, flips {direction} ({flip_m:.4f} vs {noflip_m:.4f})")

    # ---- PART 3: Time Analysis ----
    print("\n" + "=" * 80)
    print("PART 3: TIME-OF-CROSSING ANALYSIS")
    print("=" * 80)

    # When do flips vs non-flips happen?
    time_buckets = [(0, 30), (30, 60), (60, 120), (120, 180), (180, 240), (240, 300)]
    print(f"\n{'Bucket (remaining sec)':<25} {'Total':>8} {'Flips':>8} {'Flip%':>8}")
    print("-" * 52)

    for lo, hi in time_buckets:
        bucket = [f for f in all_features if lo <= f["time_remaining"] < hi]
        bucket_flips = [f for f in bucket if f["flip"]]
        flip_pct = len(bucket_flips) / len(bucket) * 100 if bucket else 0
        print(f"{lo}-{hi}s remaining:{'':>5} {len(bucket):>8} {len(bucket_flips):>8} {flip_pct:>7.1f}%")

    # ---- PART 4: BTC Alignment Analysis ----
    print("\n" + "=" * 80)
    print("PART 4: BTC DIRECTION ALIGNMENT")
    print("=" * 80)

    aligned_flips = [f for f in flip_feats if f["btc_aligned"]]
    misaligned_flips = [f for f in flip_feats if not f["btc_aligned"]]
    aligned_noflips = [f for f in noflip_feats if f["btc_aligned"]]
    misaligned_noflips = [f for f in noflip_feats if not f["btc_aligned"]]

    print(f"\nBTC aligned with favored side:")
    print(f"  Flips:     {len(aligned_flips)}")
    print(f"  Non-flips: {len(aligned_noflips)}")
    print(f"  Flip rate: {len(aligned_flips)/(len(aligned_flips)+len(aligned_noflips))*100:.1f}%" if (len(aligned_flips)+len(aligned_noflips)) > 0 else "  N/A")

    print(f"\nBTC NOT aligned with favored side:")
    print(f"  Flips:     {len(misaligned_flips)}")
    print(f"  Non-flips: {len(misaligned_noflips)}")
    print(f"  Flip rate: {len(misaligned_flips)/(len(misaligned_flips)+len(misaligned_noflips))*100:.1f}%" if (len(misaligned_flips)+len(misaligned_noflips)) > 0 else "  N/A")

    # ---- PART 5: Confusion Matrix Analysis ----
    print("\n" + "=" * 80)
    print("PART 5: FLIP PATTERN DETAILS")
    print("=" * 80)

    # What happens AFTER the crossing? Does the strong side continue or reverse?
    print("\nPost-crossing behavior (after hitting 0.7):")
    for ei in any_strong_events[:5]:  # show first 5
        favored = ei["favored"]
        flip_str = "FLIP!" if ei["flip"] else "OK"
        outcome_str = "YES won" if ei["outcome"] == 0 else "NO won"
        btc_dir = "UP" if ei["close_price"] > ei["open_price"] else "DOWN"
        print(f"\n  {flip_str} | {favored} favored | Outcome: {outcome_str} | BTC: {btc_dir}")
        print(f"    max_yes={ei['max_yes']:.3f} (at {ei['max_yes_at']}s), max_no={ei['max_no']:.3f} (at {ei['max_no_at']}s)")
        print(f"    First yes>0.7: {ei['first_yes_gt_07']}s, First no>0.7: {ei['first_no_gt_07']}s")
        print(f"    BTC open={ei['open_price']:.1f} close={ei['close_price']:.1f} change={ei['btc_change']:.1f}")

        # Show price trajectory around crossing
        if favored == "YES":
            cross_rem = ei["first_yes_gt_07"]
        else:
            cross_rem = ei["first_no_gt_07"]

        if cross_rem is not None and len(ei["remaining_series"]) > 0:
            rem_arr = np.array(ei["remaining_series"])
            cross_idx = np.argmin(np.abs(rem_arr - cross_rem))
            # Show 5 seconds before and after
            start = max(0, cross_idx - 5)
            end = min(len(ei["yes_series"]), cross_idx + 10)
            print(f"    Trajectory around crossing (idx {cross_idx}, rem {cross_rem}s):")
            for i in range(start, end):
                marker = " <<< CROSS" if i == cross_idx else ""
                print(f"      rem={ei['remaining_series'][i]:3d}s yes={ei['yes_series'][i]:.3f} no={ei['no_series'][i]:.3f} btc={ei['btc_prices'][i]:.1f}{marker}")

    # ---- PART 6: Strategy Simulation ----
    print("\n" + "=" * 80)
    print("PART 6: SIMPLE STRATEGY BACKTEST - FADE THE STRONG SIDE")
    print("=" * 80)

    # Strategy: When one side > 0.7, bet AGAINST it (bet on the other side)
    # This is a "fade" strategy - we're betting on the flip
    # Only consider events where one side was clearly > 0.7

    print("\nStrategy: At first >0.7 crossing, bet AGINST the strong side")
    print("-" * 60)

    # Simulate at different entry thresholds
    for entry_threshold in [0.7, 0.75, 0.8, 0.85, 0.9]:
        pnl_total = 0
        wins = 0
        losses = 0
        total_bets = 0

        for ei in any_strong_events:
            favored = ei["favored"]

            # Determine which side to look at
            if favored == "YES":
                target_series = ei["yes_series"]
                other_series = ei["no_series"]
                flip_wins = ei["outcome"] == 1  # NO wins = flip win
            else:
                target_series = ei["no_series"]
                other_series = ei["yes_series"]
                flip_wins = ei["outcome"] == 0  # YES wins = flip win

            remaining_arr = np.array(ei["remaining_series"])

            # Find first index where target > threshold
            cross_mask = np.array(target_series) > entry_threshold
            if not np.any(cross_mask):
                continue

            cross_idx = np.argmax(cross_mask)
            cross_rem = remaining_arr[cross_idx]

            # Only bet if crossing happens early enough (before last 10 seconds)
            if cross_rem < 10:
                continue

            # Entry: bet on the OTHER side (fading the strong side)
            entry_price = other_series[cross_idx]
            if entry_price <= 0:
                continue

            total_bets += 1

            if flip_wins:
                # The other side won - our fade bet wins
                pnl_total += (1.0 - entry_price)  # won 1 share worth $1
                wins += 1
            else:
                # The strong side won - our fade bet loses
                pnl_total += (-entry_price)  # lost our bet
                losses += 1

        print(f"\n  Threshold >{entry_threshold}:")
        print(f"    Bets: {total_bets}, Wins: {wins}, Losses: {losses}")
        print(f"    Win rate: {wins/total_bets*100:.1f}%" if total_bets > 0 else "    N/A")
        print(f"    Total PnL: {pnl_total:+.3f}")
        print(f"    Avg PnL/bet: {pnl_total/total_bets:+.4f}" if total_bets > 0 else "    N/A")

    # ---- PART 7: Conditional Strategy ----
    print("\n" + "=" * 80)
    print("PART 7: CONDITIONAL STRATEGY - FILTER FLIPS")
    print("=" * 80)

    print("\nTry to filter: only fade when certain conditions met")

    # Condition combos to test
    conditions = [
        ("Always fade", lambda f: True),
        ("BTC misaligned", lambda f: not f["btc_aligned"]),
        ("BTC misaligned + early crossing (<120s)", lambda f: not f["btc_aligned"] and f["time_remaining"] > 120),
        ("BTC misaligned + high spread", lambda f: not f["btc_aligned"] and f["spread"] > 0.15),
        ("BTC misaligned + flow inconsistency", lambda f: not f["btc_aligned"] and f["flow_consistency"] < 0.6),
        ("High path inefficiency (>3)", lambda f: f["path_inefficiency"] > 3),
        ("Low flow consistency (<0.5)", lambda f: f["flow_consistency"] < 0.5),
        ("Fast crossing (>0.5 to >0.7 in <30s)", lambda f: f["speed_05_to_07"] < 30),
        ("Early crossing + low flow consistency", lambda f: f["time_remaining"] > 150 and f["flow_consistency"] < 0.55),
        ("BTC misaligned + path inefficient", lambda f: not f["btc_aligned"] and f["path_inefficiency"] > 2),
        ("Late crossing (<60s) + BTC misaligned", lambda f: f["time_remaining"] < 60 and not f["btc_aligned"]),
        ("High volatility acceleration", lambda f: f["vol_acceleration"] > 2.0),
        ("BTC misaligned + fast crossing", lambda f: not f["btc_aligned"] and f["speed_05_to_07"] < 20),
        ("BTC misaligned + other side rising", lambda f: not f["btc_aligned"] and f["other_change_10s"] > 0.02),
        ("BTC misaligned + reversals > 3", lambda f: not f["btc_aligned"] and f["reversals"] > 3),
    ]

    print(f"\n{'Condition':<50} {'Total':>6} {'Wins':>6} {'Loss':>6} {'Win%':>7} {'PnL':>8} {'Avg':>8}")
    print("-" * 95)

    for cond_name, cond_fn in conditions:
        pnl_total = 0
        wins = 0
        losses = 0
        total_bets = 0

        for f in all_features:
            if f["time_remaining"] < 10:
                continue  # too late to bet

            if not cond_fn(f):
                continue

            total_bets += 1
            entry_price = f["other_price"]  # bet on the OTHER side
            if entry_price <= 0:
                continue

            if f["flip"]:
                pnl_total += (1.0 - entry_price)
                wins += 1
            else:
                pnl_total += (-entry_price)
                losses += 1

        if total_bets > 0:
            print(f"{cond_name:<50} {total_bets:>6} {wins:>6} {losses:>6} {wins/total_bets*100:>6.1f}% {pnl_total:>+8.3f} {pnl_total/total_bets:>+8.4f}")
        else:
            print(f"{cond_name:<50} {total_bets:>6} {'-':>6} {'-':>6} {'-':>7} {'-':>8} {'-':>8}")

    # ---- PART 8: Feature Importance via Decision Tree heuristic ----
    print("\n" + "=" * 80)
    print("PART 8: FEATURE COMBINATIONS SEARCH")
    print("=" * 80)

    print("\nBrute-force search for best 2-condition filter...")
    print("(Evaluating combinations that maximize PnL)")

    simple_conditions = [
        ("btc_misaligned", lambda f, th=0: not f["btc_aligned"]),
        ("time_rem_gt", lambda f, th: f["time_remaining"] > th),
        ("time_rem_lt", lambda f, th: f["time_remaining"] < th),
        ("spread_gt", lambda f, th: f["spread"] > th),
        ("flow_cons_lt", lambda f, th: f["flow_consistency"] < th),
        ("path_ineff_gt", lambda f, th: f["path_inefficiency"] > th),
        ("speed_lt", lambda f, th: f["speed_05_to_07"] < th),
        ("speed_gt", lambda f, th: f["speed_05_to_07"] > th),
        ("reversals_gt", lambda f, th: f["reversals"] > th),
        ("vol_accel_gt", lambda f, th: f["vol_acceleration"] > th),
        ("other_change_gt", lambda f, th: f["other_change_10s"] > th),
        ("other_change_lt", lambda f, th: f["other_change_10s"] < th),
        ("btc_momentum_lt", lambda f, th: f["btc_momentum_5s"] < th),
        ("btc_momentum_gt", lambda f, th: f["btc_momentum_5s"] > th),
        ("target_price_lt", lambda f, th: f["target_price"] < th),
        ("target_price_gt", lambda f, th: f["target_price"] > th),
    ]

    best_results = []
    # Test single conditions with different thresholds
    for cond_name, cond_fn in simple_conditions:
        for th in [0, 30, 60, 120, 180, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1, 2, 3, 5, 10, 20, 50, -0.01, -0.02, 0.01, 0.02, 0.05]:
            if cond_name == "btc_misaligned" and th != 0:
                continue  # only th=0 makes sense for boolean

            pnl_total = 0
            wins = 0
            total_bets = 0

            for f in all_features:
                if f["time_remaining"] < 10:
                    continue
                try:
                    if not cond_fn(f, th):
                        continue
                except:
                    continue

                total_bets += 1
                entry_price = f["other_price"]
                if entry_price <= 0:
                    continue

                if f["flip"]:
                    pnl_total += (1.0 - entry_price)
                    wins += 1
                else:
                    pnl_total += (-entry_price)

            if total_bets >= 5 and pnl_total > 0:
                best_results.append((pnl_total, wins/total_bets, total_bets, f"{cond_name}({th})"))

    best_results.sort(key=lambda x: x[0], reverse=True)
    print(f"\nTop 20 single-condition filters:")
    print(f"{'Rank':<6} {'Condition':<35} {'PnL':>8} {'Win%':>7} {'Bets':>6}")
    print("-" * 65)
    for i, (pnl, wr, bets, desc) in enumerate(best_results[:20]):
        print(f"{i+1:<6} {desc:<35} {pnl:>+8.3f} {wr*100:>6.1f}% {bets:>6}")

    # ---- PART 9: Detailed flip case studies ----
    print("\n" + "=" * 80)
    print("PART 9: ALL FLIP CASES SUMMARY")
    print("=" * 80)

    print(f"\nTotal flips: {len(flips)}")
    for i, ei in enumerate(flips):
        btc_dir = "UP" if ei["close_price"] > ei["open_price"] else "DOWN"
        print(f"\n--- Flip #{i+1} ---")
        print(f"  Favored: {ei['favored']}, Outcome: {'YES' if ei['outcome']==0 else 'NO'} won")
        print(f"  BTC: {ei['open_price']:.1f} -> {ei['close_price']:.1f} ({btc_dir}, {ei['btc_change']:+.1f})")
        print(f"  Max YES: {ei['max_yes']:.3f} at {ei['max_yes_at']}s")
        print(f"  Max NO:  {ei['max_no']:.3f} at {ei['max_no_at']}s")
        print(f"  First YES>0.7: {ei['first_yes_gt_07']}s, First NO>0.7: {ei['first_no_gt_07']}s")

        # Check if this was a "late flip" (strong side flipped at the very end)
        if ei["favored"] == "YES":
            final_yes = ei["yes_series"][-1]
            final_no = ei["no_series"][-1]
        else:
            final_yes = ei["no_series"][-1]
            final_no = ei["yes_series"][-1]
        print(f"  Final prices: yes={ei['yes_series'][-1]:.3f}, no={ei['no_series'][-1]:.3f}")

    # ---- Save for further analysis ----
    output = {
        "summary": {
            "total_events": total,
            "any_strong": len(any_strong_events),
            "total_flips": len(flips),
            "flip_rate": len(flips) / len(any_strong_events) if any_strong_events else 0,
        },
        "significant_features": [{"feature": k, "p_value": p, "flip_mean": fm, "noflip_mean": nm}
                                 for k, p, _, fm, nm in significant_features],
        "all_features": all_features,
    }

    out_path = Path(__file__).parent.parent / "data" / "flip_comprehensive_analysis.json"
    # Save only summary + metadata, not the full feature arrays
    with open(out_path, "w") as fp:
        json.dump(output, fp, default=str)

    print(f"\nAnalysis saved to {out_path}")
    print("\nDone!")


if __name__ == "__main__":
    main()
