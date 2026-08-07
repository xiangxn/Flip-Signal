#!/usr/bin/env python3
"""
Flip Strategy Design - Part 3
==============================
Test "wait-and-see" strategy: after crossing 0.7, monitor BTC
for reversal signals before deciding to fade.

Also: test combined pre-crossing + post-crossing filters.
"""

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy import stats

DATA_DIR = Path(__file__).parent.parent / "data" / "lab"


def load_all_events():
    events = []
    for f in sorted(DATA_DIR.glob("events_*.jsonl")):
        with open(f) as fp:
            for line in fp:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    return events


def simulate_wait_and_see(events, grace_periods=[5, 10, 15, 20, 30]):
    """
    Strategy: When crossing 0.7, wait `grace` seconds.
    Then check:
    1. BTC direction in the grace period
    2. Flow direction in the grace period
    3. Target price movement in the grace period

    If reversal signals detected → fade (bet against strong side)
    Otherwise → don't bet
    """
    results = {}

    for grace in grace_periods:
        for reversal_threshold in [-0.005, -0.01, -0.02, -0.03]:  # BTC aligned change %
            for confirm_bars in [1, 2, 3]:  # consecutive bars confirming reversal
                pnl = 0
                wins = 0
                losses = 0
                total = 0
                details = []

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
                    other_series = np.array([s["no_price"] if favored == "YES" else s["yes_price"] for s in snaps])
                    btc_prices = np.array([s["price"] for s in snaps])
                    btc_rets = np.array([s["ret_10s"] for s in snaps])
                    signed_flows = np.array([s["signed_flow_5s"] for s in snaps])
                    remaining_arr = np.array([s["remaining_sec"] for s in snaps])

                    # Find crossing
                    cross_mask = target_series > 0.7
                    if not np.any(cross_mask):
                        continue
                    cross_idx = np.argmax(cross_mask)
                    cross_rem = remaining_arr[cross_idx]

                    # Need enough time after crossing for grace period + bet placement
                    if cross_rem < grace + 10:
                        continue

                    # Simulate: at cross_idx + grace, look back
                    grace_end_idx = cross_idx + grace
                    if grace_end_idx >= len(snaps):
                        continue

                    # Check BTC behavior during grace period
                    grace_btc_start = btc_prices[cross_idx]
                    grace_btc_end = btc_prices[grace_end_idx]
                    grace_btc_change = (grace_btc_end - grace_btc_start) / grace_btc_start * 100 * direction

                    # Check target price behavior during grace period
                    grace_target_change = target_series[grace_end_idx] - target_series[cross_idx]

                    # Check flow behavior during grace period
                    grace_flows = signed_flows[cross_idx:grace_end_idx+1] * direction
                    grace_flow_sum = np.sum(grace_flows)

                    # Check for sustained reversal: N consecutive bars with negative aligned rets
                    grace_rets = btc_rets[cross_idx+1:grace_end_idx+1] * direction  # aligned rets
                    if len(grace_rets) >= confirm_bars:
                        # Check if there are `confirm_bars` consecutive negative rets
                        has_sustained = False
                        for i in range(len(grace_rets) - confirm_bars + 1):
                            if np.all(grace_rets[i:i+confirm_bars] < reversal_threshold / 100):
                                has_sustained = True
                                break
                    else:
                        has_sustained = False

                    # Decision rule: fade if BTC showed reversal in grace period
                    should_fade = grace_btc_change < reversal_threshold

                    if not should_fade:
                        continue

                    # Entry: bet on the OTHER side
                    entry_price = other_series[grace_end_idx]
                    if entry_price <= 0.01:
                        continue  # can't get good odds

                    # Did the favored side win?
                    outcome = event["outcome"]
                    flip = outcome != (0 if favored == "YES" else 1)

                    total += 1
                    if flip:
                        pnl += (1.0 - entry_price)
                        wins += 1
                    else:
                        pnl += (-entry_price)
                        losses += 1

                    details.append({
                        "favored": favored,
                        "flip": flip,
                        "cross_rem": cross_rem,
                        "grace_btc_change": grace_btc_change,
                        "entry_price": entry_price,
                    })

                key = f"g{grace}_th{reversal_threshold}_cb{confirm_bars}"
                if total >= 5 and pnl > 0:
                    results[key] = {
                        "grace": grace,
                        "threshold": reversal_threshold,
                        "confirm_bars": confirm_bars,
                        "pnl": pnl,
                        "wins": wins,
                        "losses": losses,
                        "total": total,
                        "win_rate": wins / total,
                        "avg_pnl": pnl / total,
                    }

    return results


def simulate_combined_filter(events):
    """
    Combine pre-crossing and post-crossing filters for the best strategy.
    """
    print("\n--- Combined Strategy Simulation ---")
    print(f"{'Strategy':<65} {'Total':>6} {'Wins':>6} {'Win%':>7} {'PnL':>8} {'Avg':>8}")
    print("-" * 106)

    strategies = []

    for event in events:
        snaps = event["snapshots"]
        open_price = event["open_price"]

        yes_series = [s["yes_price"] for s in snaps]
        no_series = [s["no_price"] for s in snaps]
        btc_prices = np.array([s["price"] for s in snaps])
        btc_rets = np.array([s["ret_10s"] for s in snaps])
        signed_flows = np.array([s["signed_flow_5s"] for s in snaps])
        remaining_arr = np.array([s["remaining_sec"] for s in snaps])

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
        other_series = np.array([s["no_price"] if favored == "YES" else s["yes_price"] for s in snaps])

        cross_mask = target_series > 0.7
        if not np.any(cross_mask):
            continue
        cross_idx = np.argmax(cross_mask)
        cross_rem = remaining_arr[cross_idx]

        # Pre-crossing features
        btc_dist = (btc_prices[cross_idx] - open_price) / open_price * 100 * direction
        btc_aligned = btc_dist > 0

        # Momentum deceleration
        n_pre = cross_idx
        if n_pre >= 20:
            early = np.mean(btc_rets[max(0, n_pre-20):max(0, n_pre-10)] * direction)
            late = np.mean(btc_rets[max(0, n_pre-10):n_pre+1] * direction)
            mom_decel = early - late
        else:
            mom_decel = 0

        # Flow exhaustion
        if n_pre >= 15:
            early_flow = np.sum(signed_flows[max(0, n_pre-15):max(0, n_pre-8)] * direction)
            late_flow = np.sum(signed_flows[max(0, n_pre-7):n_pre+1] * direction)
            flow_exh = early_flow - late_flow
        else:
            flow_exh = 0

        # Reversals
        pre_target = target_series[:n_pre+1]
        diffs = np.diff(pre_target)
        reversals = np.sum(diffs < -0.01)

        flip = event["outcome"] != (0 if favored == "YES" else 1)

        strategies.append({
            "favored": favored,
            "flip": flip,
            "cross_idx": cross_idx,
            "cross_rem": cross_rem,
            "btc_dist": btc_dist,
            "btc_aligned": btc_aligned,
            "mom_decel": mom_decel,
            "flow_exh": flow_exh,
            "reversals": reversals,
            "target_series": target_series,
            "other_series": other_series,
            "btc_prices": btc_prices,
            "btc_rets": btc_rets,
            "signed_flows": signed_flows,
            "direction": direction,
            "remaining_arr": remaining_arr,
        })

    # Strategy definitions
    def make_btc_misaligned(grace=0):
        def fn(s):
            if s["cross_rem"] < 10 + grace:
                return None
            if not s["btc_aligned"]:
                if grace > 0:
                    gidx = s["cross_idx"] + grace
                    if gidx >= len(s["other_series"]):
                        return None
                    return {"entry": s["other_series"][gidx]}
                return {"entry": s["other_series"][s["cross_idx"]]}
            return None
        return fn

    def make_btc_misaligned_or_weak(grace=0, max_dist=0.01):
        def fn(s):
            if s["cross_rem"] < 10 + grace:
                return None
            if not s["btc_aligned"] or s["btc_dist"] < max_dist:
                if grace > 0:
                    gidx = s["cross_idx"] + grace
                    if gidx >= len(s["other_series"]):
                        return None
                    return {"entry": s["other_series"][gidx]}
                return {"entry": s["other_series"][s["cross_idx"]]}
            return None
        return fn

    def make_grace_reversal(grace=10, threshold=-0.005, confirm=1):
        def fn(s):
            if s["cross_rem"] < grace + 10:
                return None
            gidx = s["cross_idx"] + grace
            if gidx >= len(s["btc_prices"]):
                return None
            btc_change = (s["btc_prices"][gidx] - s["btc_prices"][s["cross_idx"]]) / s["btc_prices"][s["cross_idx"]] * 100 * s["direction"]

            # Check sustained reversal
            if confirm > 1 and gidx > s["cross_idx"] + confirm:
                grace_rets = s["btc_rets"][s["cross_idx"]+1:gidx+1] * s["direction"]
                has_confirm = False
                for i in range(len(grace_rets) - confirm + 1):
                    if np.all(grace_rets[i:i+confirm] < threshold):
                        has_confirm = True
                        break
                if not has_confirm:
                    return None

            if btc_change < threshold:
                entry = s["other_series"][gidx]
                if entry > 0.01:
                    return {"entry": entry}
            return None
        return fn

    def make_btc_misaligned_or_grace_reversal(grace=10, threshold=-0.005):
        def fn(s):
            if s["cross_rem"] < grace + 10:
                return None
            # Check pre-crossing misalignment
            if not s["btc_aligned"]:
                return {"entry": s["other_series"][s["cross_idx"]]}
            # Check grace period reversal
            gidx = s["cross_idx"] + grace
            if gidx >= len(s["btc_prices"]):
                return None
            btc_change = (s["btc_prices"][gidx] - s["btc_prices"][s["cross_idx"]]) / s["btc_prices"][s["cross_idx"]] * 100 * s["direction"]
            if btc_change < threshold:
                entry = s["other_series"][gidx]
                if entry > 0.01:
                    return {"entry": entry}
            return None
        return fn

    strategy_list = [
        ("S0: BTC misaligned (instant fade)", make_btc_misaligned(0)),
        ("S1: BTC misaligned OR BTC dist<0.01%", make_btc_misaligned_or_weak(0, 0.01)),
        ("S2: Grace 5s, BTC reversal > -0.005%", make_grace_reversal(5, -0.005, 1)),
        ("S3: Grace 10s, BTC reversal > -0.005%", make_grace_reversal(10, -0.005, 1)),
        ("S4: Grace 15s, BTC reversal > -0.005%", make_grace_reversal(15, -0.005, 1)),
        ("S5: Grace 10s, BTC reversal > -0.01%, confirm 2", make_grace_reversal(10, -0.01, 2)),
        ("S6: Grace 10s, BTC reversal > -0.005%, confirm 2", make_grace_reversal(10, -0.005, 2)),
        ("S7: Grace 5s, BTC reversal > -0.01%, confirm 2", make_grace_reversal(5, -0.01, 2)),
        ("S8: BTC misaligned OR (grace 10s reversal -0.005%)", make_btc_misaligned_or_grace_reversal(10, -0.005)),
        ("S9: Grace 20s, BTC reversal > -0.005%, confirm 3", make_grace_reversal(20, -0.005, 3)),
    ]

    for name, strat_fn in strategy_list:
        pnl = 0
        wins = 0
        total = 0
        entries = []

        for s in strategies:
            result = strat_fn(s)
            if result is None:
                continue
            entry = result["entry"]
            if entry <= 0.01:
                continue

            total += 1
            entries.append(entry)

            if s["flip"]:
                pnl += (1.0 - entry)
                wins += 1
            else:
                pnl += (-entry)

        if total >= 3:
            print(f"{name:<65} {total:>6} {wins:>6} {wins/total*100:>6.1f}% {pnl:>+8.3f} {pnl/total:>+8.4f}")
        else:
            print(f"{name:<65} {total:>6} {wins:>6} {'-':>7} {'-':>8} {'-':>8}")

    # --- Detailed PnL Analysis for Best Strategy ---
    print("\n\n--- DETAILED: Best Strategy PnL Breakdown ---")

    # Strategy 8: BTC misaligned OR (grace 10s reversal -0.005%)
    best_fn = make_btc_misaligned_or_grace_reversal(10, -0.005)
    trades = []
    for s in strategies:
        result = best_fn(s)
        if result is None:
            continue
        entry = result["entry"]
        if entry <= 0.01:
            continue
        pnl_trade = (1.0 - entry) if s["flip"] else (-entry)
        trades.append({
            "flip": s["flip"],
            "favored": s["favored"],
            "entry": entry,
            "pnl": pnl_trade,
            "cross_rem": s["cross_rem"],
            "btc_dist": s["btc_dist"],
        })

    trades.sort(key=lambda t: t["pnl"], reverse=True)
    print(f"\n  Total trades: {len(trades)}")
    print(f"  Total PnL: {sum(t['pnl'] for t in trades):+.3f}")
    print(f"  Win rate: {sum(1 for t in trades if t['pnl'] > 0)/len(trades)*100:.1f}%")
    print(f"  Avg win: {np.mean([t['pnl'] for t in trades if t['pnl'] > 0]):+.4f}")
    print(f"  Avg loss: {np.mean([t['pnl'] for t in trades if t['pnl'] < 0]):+.4f}")
    print(f"\n  Top 5 trades:")
    for t in trades[:5]:
        print(f"    {t['favored']} favored, entry={t['entry']:.3f}, PnL={t['pnl']:+.3f}, flip={t['flip']}, btc_dist={t['btc_dist']:+.4f}%")
    print(f"\n  Bottom 5 trades:")
    for t in trades[-5:]:
        print(f"    {t['favored']} favored, entry={t['entry']:.3f}, PnL={t['pnl']:+.3f}, flip={t['flip']}, btc_dist={t['btc_dist']:+.4f}%")

    # --- Entry Price Distribution ---
    print(f"\n  Entry price distribution:")
    entry_prices = [t["entry"] for t in trades]
    for lo, hi in [(0.01, 0.15), (0.15, 0.25), (0.25, 0.35), (0.35, 0.50)]:
        bucket = [t for t in trades if lo <= t["entry"] < hi]
        if bucket:
            b_pnl = sum(t["pnl"] for t in bucket)
            b_wr = sum(1 for t in bucket if t["pnl"] > 0) / len(bucket)
            print(f"    Entry {lo}-{hi}: {len(bucket)} trades, PnL={b_pnl:+.3f}, WR={b_wr*100:.0f}%")


def main():
    print("=" * 80)
    print("FLIP STRATEGY OPTIMIZATION")
    print("=" * 80)

    events = load_all_events()
    print(f"Loaded {len(events)} events")

    simulate_combined_filter(events)

    # Also find optimal grace period configs
    print("\n\n--- Grace Period Parameter Sweep (top results) ---")
    results = simulate_wait_and_see(events)
    sorted_results = sorted(results.values(), key=lambda r: r["pnl"], reverse=True)
    print(f"\n{'Config':<30} {'Grace':>6} {'Thresh':>8} {'Confirm':>8} {'Total':>6} {'Wins':>6} {'Win%':>7} {'PnL':>8} {'Avg':>8}")
    print("-" * 93)
    for r in sorted_results[:20]:
        print(f"{'g'+str(r['grace'])+'_th'+str(r['threshold'])+'_cb'+str(r['confirm_bars']):<30} "
              f"{r['grace']:>6} {r['threshold']:>8} {r['confirm_bars']:>8} "
              f"{r['total']:>6} {r['wins']:>6} {r['win_rate']*100:>6.1f}% {r['pnl']:>+8.3f} {r['avg_pnl']:>+8.4f}")

    print("\nDone!")


if __name__ == "__main__":
    main()
