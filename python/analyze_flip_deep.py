#!/usr/bin/env python3
"""
Deep Flip Analysis - Part 2
============================
Analyze BTC post-crossing behavior, flip sub-types,
and whether flips are predictable before market close.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy import stats

from backtest_flip_config import DEFAULT_CONFIG, ETH_CONFIG


def load_all_events(data_dir):
    data_path = Path(data_dir)
    events = []
    for f in sorted(data_path.glob("events_*.jsonl")):
        with open(f) as fp:
            for line in fp:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    print(f"Loaded {len(events)} events")
    return events


def analyze_post_crossing_btc(event, favored_side):
    """
    Analyze BTC behavior after the market crosses 0.7.

    Key question: Does BTC momentum continue, stall, or reverse?

    Returns detailed post-crossing metrics.
    """
    snaps = event["snapshots"]
    open_price = event["open_price"]

    # Determine target price series
    if favored_side == "YES":
        target_series = [s["yes_price"] for s in snaps]
        btc_direction = 1  # expect BTC UP
    else:
        target_series = [s["no_price"] for s in snaps]
        btc_direction = -1  # expect BTC DOWN

    remaining_arr = np.array([s["remaining_sec"] for s in snaps])
    btc_prices = np.array([s["price"] for s in snaps])
    btc_rets = np.array([s["ret_10s"] for s in snaps])
    signed_flows = np.array([s["signed_flow_5s"] for s in snaps])

    # Find first crossing of 0.7
    cross_mask = np.array(target_series) > 0.7
    if not np.any(cross_mask):
        return None

    cross_idx = np.argmax(cross_mask)

    # Split into pre and post crossing
    pre_btc = btc_prices[:cross_idx + 1]
    post_btc = btc_prices[cross_idx:]
    post_rets = btc_rets[cross_idx:]
    post_flows = signed_flows[cross_idx:]
    post_remaining = remaining_arr[cross_idx:]

    if len(post_btc) < 5:
        return None

    # BTC change from open to crossing
    btc_at_cross = btc_prices[cross_idx]
    btc_change_to_cross = (btc_at_cross - open_price) / open_price * 100  # pct

    # BTC change AFTER crossing (to end of window)
    btc_at_end = btc_prices[-1]
    btc_change_after_cross = (btc_at_end - btc_at_cross) / btc_at_cross * 100  # pct

    # BTC direction alignment
    # "Aligned" means BTC moved in the expected direction from open to cross
    aligned_at_cross = (btc_change_to_cross * btc_direction) > 0

    # "Continued" means BTC continued in same direction after cross
    btc_continued = (btc_change_after_cross * btc_direction) > 0

    # "Reversed" means BTC reversed direction after cross
    btc_reversed = (btc_change_after_cross * btc_direction) < 0

    # BTC momentum in the last N seconds of the window
    n_last = min(30, len(post_btc))
    last_btc_segment = btc_prices[-n_last:]
    btc_last_change = (last_btc_segment[-1] - last_btc_segment[0]) / last_btc_segment[0] * 100

    # BTC trajectory: how did BTC move post-crossing in aligned direction
    btc_aligned_pct = btc_change_after_cross * btc_direction  # positive = continued, negative = reversed

    # Signed flow post-crossing: net flow in the aligned direction
    post_aligned_flow = np.sum(post_flows * btc_direction)

    # Flow reversal count post-crossing
    flow_signs = np.sign(post_flows)
    flow_flips = np.sum(np.abs(np.diff(flow_signs)) > 0)
    flow_flip_rate = flow_flips / len(post_flows) if len(post_flows) > 1 else 0

    # BTC "distance from open" at various checkpoints
    btc_dist_from_open = (btc_prices - open_price) / open_price * 100 * btc_direction

    # Max BTC distance in the aligned direction post-crossing
    post_btc_dist = btc_dist_from_open[cross_idx:]
    max_aligned_dist = np.max(post_btc_dist)  # most aligned
    min_aligned_dist = np.min(post_btc_dist)  # most against

    # Did BTC ever cross below open after the crossing? (reversal signal)
    crossed_below_open = np.any(post_btc_dist < 0)

    # Return timing (time from crossing to when btc_rets turn negative in aligned direction)
    # i.e., first sustained reversal
    post_aligned_rets = post_rets * btc_direction  # positive = moving in favored dir
    reversal_indices = np.where(post_aligned_rets < 0)[0]
    first_reversal_idx = reversal_indices[0] if len(reversal_indices) > 0 else len(post_aligned_rets)

    # Sustained reversal: 3+ consecutive negative aligned rets
    sustained_reversal_idx = len(post_aligned_rets)
    for i in range(len(post_aligned_rets) - 2):
        if np.all(post_aligned_rets[i:i+3] < 0):
            sustained_reversal_idx = i
            break

    return {
        "condition_id": event["condition_id"],
        "favored": favored_side,
        "flip": event["outcome"] != (0 if favored_side == "YES" else 1),
        "outcome": event["outcome"],

        # Pre-crossing
        "btc_change_to_cross_pct": btc_change_to_cross,
        "aligned_at_cross": aligned_at_cross,

        # Post-crossing BTC
        "btc_change_after_cross_pct": btc_change_after_cross,
        "btc_aligned_pct": btc_aligned_pct,
        "btc_continued": btc_continued,
        "btc_reversed": btc_reversed,
        "btc_last_30s_change_pct": btc_last_change,

        # Post-crossing flow
        "post_aligned_flow": post_aligned_flow,
        "post_flow_flip_rate": flow_flip_rate,

        # Extremes
        "max_aligned_dist": max_aligned_dist,
        "min_aligned_dist": min_aligned_dist,
        "crossed_below_open": crossed_below_open,

        # Reversal timing
        "first_reversal_idx": int(first_reversal_idx),
        "sustained_reversal_idx": int(sustained_reversal_idx),

        # General
        "open_price": event["open_price"],
        "close_price": event["close_price"],
        "btc_total_change_pct": (event["close_price"] - event["open_price"]) / event["open_price"] * 100,
        "cross_remaining": float(remaining_arr[cross_idx]),
        "n_post_snapshots": len(post_btc),
    }


def main():
    parser = argparse.ArgumentParser(description="Deep Flip Analysis")
    parser.add_argument("--data", default="../data/btc/", help="JSONL 数据目录 (默认: ../data/btc/)")
    parser.add_argument("--profile", choices=["btc", "eth"], default="btc", help="参数预设 (默认: btc)")
    args = parser.parse_args()

    cfg = DEFAULT_CONFIG if args.profile == "btc" else ETH_CONFIG

    print("=" * 80)
    print(f"DEEP FLIP ANALYSIS [{args.profile.upper()}] - POST-CROSSING BEHAVIOR")
    print("=" * 80)

    events = load_all_events(args.data)

    # Analyze each event that has a side > 0.7
    results = []
    for event in events:
        snaps = event["snapshots"]
        yes_series = [s["yes_price"] for s in snaps]
        no_series = [s["no_price"] for s in snaps]

        max_yes = max(yes_series)
        max_no = max(no_series)

        if max_yes > 0.7 and max_no > 0.7:
            # Both crossed - analyze the side that crossed first
            first_yes = next((s["remaining_sec"] for s in snaps if s["yes_price"] > 0.7), 999)
            first_no = next((s["remaining_sec"] for s in snaps if s["no_price"] > 0.7), 999)
            favored = "YES" if first_yes < first_no else "NO"
        elif max_yes > 0.7:
            favored = "YES"
        elif max_no > 0.7:
            favored = "NO"
        else:
            continue

        r = analyze_post_crossing_btc(event, favored)
        if r:
            results.append(r)

    flips = [r for r in results if r["flip"]]
    noflips = [r for r in results if not r["flip"]]

    print(f"\nTotal analyzed: {len(results)}, Flips: {len(flips)}, Non-flips: {len(noflips)}")

    # ---- PART A: BTC Reversal Analysis ----
    print("\n" + "=" * 80)
    print("PART A: BTC REVERSAL AFTER CROSSING")
    print("=" * 80)

    for label, group in [("FLIPS", flips), ("NON-FLIPS", noflips)]:
        n_total = len(group)
        n_continued = sum(1 for r in group if r["btc_continued"])
        n_reversed = sum(1 for r in group if r["btc_reversed"])
        n_neutral = n_total - n_continued - n_reversed

        print(f"\n{label} (n={n_total}):")
        print(f"  BTC continued in favored direction: {n_continued} ({n_continued/n_total*100:.1f}%)")
        print(f"  BTC reversed after crossing:        {n_reversed} ({n_reversed/n_total*100:.1f}%)")
        print(f"  BTC flat:                           {n_neutral} ({n_neutral/n_total*100:.1f}%)")

    # BTC aligned_pct distribution
    print(f"\n{'Metric':<40} {'Flip Mean':>10} {'NoFlip Mean':>10} {'T-stat':>8} {'P-value':>8}")
    print("-" * 80)

    numeric_keys = [
        "btc_aligned_pct",
        "btc_change_after_cross_pct",
        "btc_last_30s_change_pct",
        "post_aligned_flow",
        "post_flow_flip_rate",
        "max_aligned_dist",
        "min_aligned_dist",
        "first_reversal_idx",
        "sustained_reversal_idx",
        "cross_remaining",
    ]

    for key in numeric_keys:
        fv = [r[key] for r in flips]
        nv = [r[key] for r in noflips]
        if len(fv) < 3 or len(nv) < 3:
            continue
        fm, nm = np.mean(fv), np.mean(nv)
        try:
            t, p = stats.ttest_ind(fv, nv, equal_var=False)
        except:
            t, p = 0, 1
        sig = " ***" if p < 0.001 else " **" if p < 0.01 else " *" if p < 0.05 else ""
        print(f"{key:<40} {fm:>10.4f} {nm:>10.4f} {t:>8.2f} {p:>8.4f}{sig}")

    # ---- PART B: Flip Sub-types ----
    print("\n" + "=" * 80)
    print("PART B: FLIP SUB-TYPE CLASSIFICATION")
    print("=" * 80)

    # Type 1: BTC was NEVER aligned (misaligned from start)
    # Type 2: BTC was aligned but reversed AFTER crossing
    # Type 3: BTC was aligned, continued, but not enough (tiny margin)

    type1 = [r for r in flips if not r["aligned_at_cross"]]
    type2 = [r for r in flips if r["aligned_at_cross"] and r["btc_reversed"]]
    type3 = [r for r in flips if r["aligned_at_cross"] and r["btc_continued"]]

    print(f"\nFlip sub-types:")
    print(f"  Type 1 (BTC never aligned at cross):  {len(type1)} ({len(type1)/len(flips)*100:.1f}%)")
    print(f"  Type 2 (BTC aligned then reversed):   {len(type2)} ({len(type2)/len(flips)*100:.1f}%)")
    print(f"  Type 3 (BTC continued but not enough): {len(type3)} ({len(type3)/len(flips)*100:.1f}%)")

    # Detailed analysis of Type 2 (reversal flips) - most actionable
    if type2:
        print(f"\n--- Type 2 Details (Reversal Flips) ---")
        for r in type2:
            print(f"  {r['condition_id'][:20]}... | {r['favored']} favored | "
                  f"BTC to cross: {r['btc_change_to_cross_pct']:+.4f}% | "
                  f"BTC after cross: {r['btc_aligned_pct']:+.4f}% | "
                  f"Last 30s: {r['btc_last_30s_change_pct']:+.4f}% | "
                  f"1st reversal at idx: {r['first_reversal_idx']} | "
                  f"Sustained reversal: {r['sustained_reversal_idx']} | "
                  f"Cross at rem={r['cross_remaining']:.0f}s")

    # ---- PART C: BTC Margin Analysis ----
    print("\n" + "=" * 80)
    print("PART C: BTC MARGIN ANALYSIS (How decisive was the BTC move?)")
    print("=" * 80)

    # For flips, how big was the BTC move against the favored side?
    print("\nBTC total change (open to close) in favored direction:")
    for label, group in [("FLIPS", flips), ("NON-FLIPS", noflips)]:
        btc_changes = [r["btc_total_change_pct"] * (1 if r["favored"] == "YES" else -1) for r in group]
        print(f"  {label}: mean={np.mean(btc_changes):+.4f}%, median={np.median(btc_changes):+.4f}%, "
              f"std={np.std(btc_changes):.4f}%, "
              f"min={np.min(btc_changes):+.4f}%, max={np.max(btc_changes):+.4f}%")

    # Distribution of absolute BTC change
    print("\nAbsolute BTC change distribution:")
    for label, group in [("FLIPS", flips), ("NON-FLIPS", noflips)]:
        abs_changes = [abs(r["btc_total_change_pct"]) for r in group]
        print(f"  {label}: mean={np.mean(abs_changes):.4f}%, median={np.median(abs_changes):.4f}%")

    # ---- PART D: Last-Second Flip Detection ----
    print("\n" + "=" * 80)
    print("PART D: CAN WE DETECT FLIPS BEFORE MARKET CLOSE?")
    print("=" * 80)

    # At various "decision points" (remaining seconds),
    # check if flip was already predicted by BTC direction
    for check_rem in [60, 30, 15, 10, 5]:
        detectable = 0
        for r in flips:
            # At check_rem seconds remaining, what was BTC doing?
            # We need to see if BTC had already moved against the favored direction
            if r["cross_remaining"] <= check_rem:
                continue  # hadn't crossed yet

            # Check: was the aligned_pct already negative by check_rem?
            # We can approximate from min_aligned_dist
            if r["min_aligned_dist"] < -0.01:  # BTC went at least 0.01% against
                detectable += 1

        eligible = sum(1 for r in flips if r["cross_remaining"] > check_rem)
        print(f"  At {check_rem}s remaining: {detectable}/{eligible} flips had already shown BTC reversal")

    # ---- PART E: Strategy Simulation with Timing ----
    print("\n" + "=" * 80)
    print("PART E: CONDITIONAL FADE STRATEGY WITH POST-CROSSING FEATURES")
    print("=" * 80)

    print("\nStrategy: Fade when crossing has occurred AND BTC momentum is weakening")

    conditions = [
        ("Always fade (baseline)", lambda r: True),
        ("BTC reversed post-cross", lambda r: r["btc_reversed"]),
        ("BTC reversed + cross before 120s", lambda r: r["btc_reversed"] and r["cross_remaining"] > 120),
        ("BTC reversed + cross before 60s", lambda r: r["btc_reversed"] and r["cross_remaining"] > 60),
        ("BTC reversed + cross before 30s", lambda r: r["btc_reversed"] and r["cross_remaining"] > 30),
        ("Sustained reversal before 30s", lambda r: r["sustained_reversal_idx"] < 30),
        ("Sustained reversal before 60s", lambda r: r["sustained_reversal_idx"] < 60),
        ("Crossed below open post-cross", lambda r: r["crossed_below_open"]),
        ("Min aligned dist < -0.02%", lambda r: r["min_aligned_dist"] < -0.02),
        ("Min aligned dist < -0.05%", lambda r: r["min_aligned_dist"] < -0.05),
        ("BTC last 30s against favored", lambda r: r["btc_last_30s_change_pct"] * (1 if r["favored"] == "YES" else -1) < 0),
        ("Flow flip rate > 0.3 post-cross", lambda r: r["post_flow_flip_rate"] > 0.3),
        ("Flow flip rate > 0.4 post-cross", lambda r: r["post_flow_flip_rate"] > 0.4),
        ("BTC reversed + high flow flip", lambda r: r["btc_reversed"] and r["post_flow_flip_rate"] > 0.3),
        ("Not aligned at cross (Type 1)", lambda r: not r["aligned_at_cross"]),
    ]

    # But we can only bet at the crossing moment! We need to think about this differently.
    # The features above (btc_reversed, etc.) are POST-FACTUM - we can't know them at crossing.
    # We need features COMPUTABLE AT CROSSING TIME.

    print("\nNOTE: Post-crossing BTC behavior is NOT knowable at crossing time.")
    print("The following is an UPPER BOUND on what's achievable with perfect foresight.")

    print(f"\n{'Condition':<45} {'Total':>6} {'Wins':>6} {'Loss':>6} {'Win%':>7} {'PnL':>8}")
    print("-" * 84)

    for cond_name, cond_fn in conditions:
        pnl = 0
        wins = 0
        total = 0
        for r in results:
            if r["cross_remaining"] < 10:
                continue
            if not cond_fn(r):
                continue
            total += 1
            # Entry price: bet on the OTHER side
            if r["favored"] == "YES":
                entry = 1.0 - r.get("target_price_at_cross", 0.75)  # approximate
            else:
                entry = 1.0 - r.get("target_price_at_cross", 0.75)

            # Actually we need the exact entry price. Let's approximate:
            # When target price is 0.7, other is ~0.28-0.30
            # When target price is 0.8, other is ~0.18-0.20
            # When target price is 0.9, other is ~0.08-0.10
            entry = 0.28  # approximate entry for fade bet

            if r["flip"]:
                pnl += (1.0 - entry)
                wins += 1
            else:
                pnl += (-entry)

        if total > 0:
            print(f"{cond_name:<45} {total:>6} {wins:>6} {total-wins:>6} {wins/total*100:>6.1f}% {pnl:>+8.3f}")

    # ---- PART F: Pre-Crossing Features That Predict Post-Crossing Reversal ----
    print("\n" + "=" * 80)
    print("PART F: CAN PRE-CROSSING FEATURES PREDICT POST-CROSSING BTC REVERSAL?")
    print("=" * 80)

    # This is the key question: at the moment of crossing, can we predict
    # whether BTC will continue or reverse?

    # We need to go back to the event-level data and extract features
    # at the crossing moment that might predict reversal

    print("\nComputing pre-crossing features to predict post-crossing reversal...")

    reversal_predictors = []
    for event in events:
        snaps = event["snapshots"]
        open_price = event["open_price"]

        yes_series = [s["yes_price"] for s in snaps]
        no_series = [s["no_price"] for s in snaps]

        max_yes = max(yes_series)
        max_no = max(no_series)

        if max_yes > 0.7 and max_no > 0.7:
            first_yes = next((s["remaining_sec"] for s in snaps if s["yes_price"] > 0.7), 999)
            first_no = next((s["remaining_sec"] for s in snaps if s["no_price"] > 0.7), 999)
            favored = "YES" if first_yes < first_no else "NO"
        elif max_yes > 0.7:
            favored = "YES"
        elif max_no > 0.7:
            favored = "NO"
        else:
            continue

        direction = 1 if favored == "YES" else -1
        target_series = np.array([s["yes_price"] if favored == "YES" else s["no_price"] for s in snaps])

        cross_mask = target_series > 0.7
        if not np.any(cross_mask):
            continue
        cross_idx = np.argmax(cross_mask)

        btc_prices = np.array([s["price"] for s in snaps])
        btc_rets = np.array([s["ret_10s"] for s in snaps])
        signed_flows = np.array([s["signed_flow_5s"] for s in snaps])
        bid_depths = np.array([s["bid_depth"] for s in snaps])
        ask_depths = np.array([s["ask_depth"] for s in snaps])

        # What happens after crossing?
        post_btc = btc_prices[cross_idx:]
        btc_at_cross = btc_prices[cross_idx]
        btc_end = btc_prices[-1]
        post_aligned_change = (btc_end - btc_at_cross) / btc_at_cross * 100 * direction
        will_reverse = post_aligned_change < 0
        will_continue = post_aligned_change > 0.005  # continued at least 0.005%

        # Pre-crossing features
        n_pre = cross_idx

        # 1. BTC momentum at cross: are rets accelerating or decelerating?
        if n_pre >= 20:
            early_rets = btc_rets[max(0, n_pre-20):max(0, n_pre-10)] * direction
            late_rets = btc_rets[max(0, n_pre-10):n_pre+1] * direction
            early_momentum = np.mean(early_rets) if len(early_rets) > 0 else 0
            late_momentum = np.mean(late_rets) if len(late_rets) > 0 else 0
            momentum_deceleration = early_momentum - late_momentum  # positive = slowing
        else:
            momentum_deceleration = 0

        # 2. BTC distance from open at cross
        btc_dist = (btc_at_cross - open_price) / open_price * 100 * direction

        # 3. Flow exhaustion: is buying/selling pressure fading?
        if n_pre >= 15:
            early_flow = np.sum(signed_flows[max(0, n_pre-15):max(0, n_pre-8)] * direction)
            late_flow = np.sum(signed_flows[max(0, n_pre-7):n_pre+1] * direction)
            flow_exhaustion = early_flow - late_flow  # positive = fading
        else:
            flow_exhaustion = 0

        # 4. Volatility regime
        vol_30s = np.array([s["vol_30s"] for s in snaps])
        pre_vol = vol_30s[:n_pre+1]
        vol_now = vol_30s[n_pre]
        vol_trend = vol_now / np.mean(pre_vol[-15:]) if n_pre >= 15 and np.mean(pre_vol[-15:]) > 0 else 1.0

        # 5. Order book pressure
        ob_pressure = bid_depths[n_pre] / max(ask_depths[n_pre], 1e-10)
        if direction == -1:
            ob_pressure = 1.0 / max(ob_pressure, 1e-10)  # invert for NO favored

        # 6. Speed of the rise to 0.7
        # Find when price crossed 0.5
        cross_05_idx = np.argmax(target_series > 0.5) if np.any(target_series > 0.5) else 0
        speed = n_pre - cross_05_idx  # seconds from 0.5 to 0.7

        # 7. Price path smoothness
        pre_target = target_series[:n_pre+1]
        diffs = np.diff(pre_target)
        reversals = np.sum(diffs < -0.01)

        # 8. Other side behavior
        other_series = np.array([s["no_price"] if favored == "YES" else s["yes_price"] for s in snaps])
        other_at_cross = other_series[n_pre]
        if n_pre >= 10:
            other_change = other_series[n_pre] - other_series[n_pre - 10]
        else:
            other_change = 0

        # Outcome
        flip = event["outcome"] != (0 if favored == "YES" else 1)

        reversal_predictors.append({
            "favored": favored,
            "flip": flip,
            "will_reverse": will_reverse,
            "will_continue": will_continue,
            "post_aligned_change": post_aligned_change,
            "momentum_deceleration": momentum_deceleration,
            "btc_dist_from_open": btc_dist,
            "flow_exhaustion": flow_exhaustion,
            "vol_trend": vol_trend,
            "ob_pressure": ob_pressure,
            "speed_05_to_07": speed,
            "reversals": reversals,
            "other_change": other_change,
            "cross_remaining": float(np.array([s["remaining_sec"] for s in snaps])[cross_idx]),
            "other_at_cross": float(other_at_cross),
        })

    # Now analyze: what pre-crossing features predict BTC reversal?
    print(f"\nTotal crossing events: {len(reversal_predictors)}")

    will_reverse = [r for r in reversal_predictors if r["will_reverse"]]
    will_continue = [r for r in reversal_predictors if r["will_continue"]]
    print(f"BTC reversed post-cross: {len(will_reverse)} ({len(will_reverse)/len(reversal_predictors)*100:.1f}%)")
    print(f"BTC continued post-cross: {len(will_continue)} ({len(will_continue)/len(reversal_predictors)*100:.1f}%)")

    # Feature comparison: will_reverse vs will_continue
    print(f"\nPre-crossing features: reverse vs continue")
    print(f"{'Feature':<30} {'Reverse Mean':>12} {'Continue Mean':>12} {'T-stat':>8} {'P-value':>8}")
    print("-" * 76)

    pred_keys = [
        "momentum_deceleration", "btc_dist_from_open", "flow_exhaustion",
        "vol_trend", "ob_pressure", "speed_05_to_07", "reversals", "other_change",
        "cross_remaining", "other_at_cross"
    ]

    for key in pred_keys:
        rv = [r[key] for r in will_reverse if r[key] is not None]
        cv = [r[key] for r in will_continue if r[key] is not None]
        if len(rv) < 3 or len(cv) < 3:
            continue
        rm, cm = np.mean(rv), np.mean(cv)
        try:
            t, p = stats.ttest_ind(rv, cv, equal_var=False)
        except:
            t, p = 0, 1
        sig = " ***" if p < 0.001 else " **" if p < 0.01 else " *" if p < 0.05 else ""
        print(f"{key:<30} {rm:>12.4f} {cm:>12.4f} {t:>8.2f} {p:>8.4f}{sig}")

    # ---- PART G: PRACTICAL STRATEGY DESIGN ----
    print("\n" + "=" * 80)
    print("PART G: PRACTICAL STRATEGY - WHAT'S ACTIONABLE AT CROSSING TIME?")
    print("=" * 80)

    print("\nAt the moment target price crosses 0.7, we know:")
    print("  1. BTC distance from open (direction & magnitude)")
    print("  2. BTC momentum deceleration")
    print("  3. Order flow exhaustion")
    print("  4. Speed of crossing (0.5 -> 0.7)")
    print("  5. Reversals in target price path")
    print("  6. Volatility trend")
    print("  7. Other side behavior")

    print("\nStrategy candidates:")

    # Use pre-crossing features only
    strategies = [
        ("A: BTC misaligned at cross",
         lambda r: r["btc_dist_from_open"] < 0),
        ("B: BTC barely aligned (<0.01%)",
         lambda r: 0 <= r["btc_dist_from_open"] < 0.01),
        ("C: Momentum decelerating + low distance",
         lambda r: r["momentum_deceleration"] > 0 and r["btc_dist_from_open"] < 0.02),
        ("D: Flow exhaustion + low distance",
         lambda r: r["flow_exhaustion"] > 0 and r["btc_dist_from_open"] < 0.02),
        ("E: Fast cross (speed<15s) + low distance",
         lambda r: r["speed_05_to_07"] < 15 and r["btc_dist_from_open"] < 0.02),
        ("F: High reversals + low distance",
         lambda r: r["reversals"] > 10 and r["btc_dist_from_open"] < 0.02),
        ("G: BTC misaligned OR momentum decel + low dist",
         lambda r: r["btc_dist_from_open"] < 0 or (r["momentum_deceleration"] > 0 and r["btc_dist_from_open"] < 0.02)),
        ("H: BTC misaligned OR (flow exhaustion + low dist)",
         lambda r: r["btc_dist_from_open"] < 0 or (r["flow_exhaustion"] > 0 and r["btc_dist_from_open"] < 0.01)),
        ("I: Cross late (<60s rem) + BTC misaligned",
         lambda r: r["cross_remaining"] < 60 and r["btc_dist_from_open"] < 0),
        ("J: Cross late (<60s) + BTC barely aligned",
         lambda r: r["cross_remaining"] < 60 and r["btc_dist_from_open"] < 0.01),
        ("K: BTC misaligned + cross early (>120s rem)",
         lambda r: r["btc_dist_from_open"] < 0 and r["cross_remaining"] > 120),
    ]

    print(f"\n{'Strategy':<55} {'Total':>6} {'Wins':>6} {'Win%':>7} {'PnL':>8} {'Avg':>8}")
    print("-" * 96)

    for strat_name, strat_fn in strategies:
        pnl = 0
        wins = 0
        total = 0

        for r in reversal_predictors:
            if r["cross_remaining"] < 10:
                continue
            if not strat_fn(r):
                continue

            total += 1
            entry = r["other_at_cross"]
            if entry <= 0.01:
                entry = 0.28  # fallback

            if r["flip"]:
                pnl += (1.0 - entry)
                wins += 1
            else:
                pnl += (-entry)

        if total >= 3:
            print(f"{strat_name:<55} {total:>6} {wins:>6} {wins/total*100:>6.1f}% {pnl:>+8.3f} {pnl/total:>+8.4f}")
        else:
            print(f"{strat_name:<55} {total:>6} {wins:>6} {'-':>7} {'-':>8} {'-':>8}")

    print("\nDone!")


if __name__ == "__main__":
    main()
