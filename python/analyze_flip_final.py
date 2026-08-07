#!/usr/bin/env python3
"""
Final Strategy Analysis - Part 4
=================================
Focus: Entry price optimization, BTC runway analysis,
and combined signal filtering.
"""

import json
import numpy as np
from pathlib import Path
from collections import defaultdict

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


def analyze_entry_price_vs_flip(events):
    """Analyze how flip probability varies with entry price and other conditions."""
    print("=" * 80)
    print("ENTRY PRICE ANALYSIS")
    print("=" * 80)

    data = []
    for event in events:
        snaps = event["snapshots"]
        open_price = event["open_price"]

        yes_series = [s["yes_price"] for s in snaps]
        no_series = [s["no_price"] for s in snaps]
        btc_prices = np.array([s["price"] for s in snaps])
        remaining_arr = np.array([s["remaining_sec"] for s in snaps])
        signed_flows = np.array([s["signed_flow_5s"] for s in snaps])

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
        cross_idx = np.argmax(cross_mask)
        cross_rem = remaining_arr[cross_idx]

        if cross_rem < 10:
            continue

        flip = event["outcome"] != (0 if favored == "YES" else 1)
        entry_price = other_series[cross_idx]
        target_price = target_series[cross_idx]

        btc_dist = (btc_prices[cross_idx] - open_price) / open_price * 100 * direction
        btc_aligned = btc_dist > 0

        # BTC runway: how much time left per unit of BTC distance
        btc_speed = abs(btc_dist) / max(cross_idx, 1)  # % per second
        runway = cross_rem * btc_speed  # expected additional move if speed continues

        # BTC total move (open to close)
        btc_total = (event["close_price"] - open_price) / open_price * 100 * direction

        data.append({
            "flip": flip,
            "favored": favored,
            "entry_price": entry_price,
            "target_price": target_price,
            "cross_rem": cross_rem,
            "btc_dist": btc_dist,
            "btc_aligned": btc_aligned,
            "btc_speed": btc_speed,
            "runway": runway,
            "btc_total": btc_total,
        })

    flips = [d for d in data if d["flip"]]
    noflips = [d for d in data if not d["flip"]]

    # 1. Entry price vs win rate for fade strategy
    print("\n--- Fade Strategy: Win Rate by Entry Price ---")
    price_buckets = [(0.01, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.25),
                     (0.25, 0.30), (0.30, 0.35), (0.35, 0.45), (0.45, 0.60)]
    print(f"{'Entry Price':<16} {'Total':>6} {'Flips':>6} {'Flip%':>7} {'Fade PnL':>9} {'Avg PnL':>8}")
    print("-" * 55)
    for lo, hi in price_buckets:
        bucket = [d for d in data if lo <= d["entry_price"] < hi]
        bucket_flips = [d for d in bucket if d["flip"]]
        if not bucket:
            continue
        flip_rate = len(bucket_flips) / len(bucket)
        # Simulate fade strategy
        pnl = sum((1.0 - d["entry_price"]) if d["flip"] else (-d["entry_price"]) for d in bucket)
        avg_pnl = pnl / len(bucket)
        print(f"${lo:.2f}-${hi:.2f}:{'':>5} {len(bucket):>6} {len(bucket_flips):>6} {flip_rate*100:>6.1f}% {pnl:>+9.3f} {avg_pnl:>+8.4f}")

    # 2. BTC aligned + entry price
    print("\n--- BTC Aligned Events: Win Rate by Entry Price ---")
    aligned = [d for d in data if d["btc_aligned"]]
    print(f"{'Entry Price':<16} {'Total':>6} {'Flips':>6} {'Flip%':>7} {'Fade PnL':>9}")
    print("-" * 55)
    for lo, hi in price_buckets:
        bucket = [d for d in aligned if lo <= d["entry_price"] < hi]
        bucket_flips = [d for d in bucket if d["flip"]]
        if not bucket:
            continue
        flip_rate = len(bucket_flips) / len(bucket)
        pnl = sum((1.0 - d["entry_price"]) if d["flip"] else (-d["entry_price"]) for d in bucket)
        print(f"${lo:.2f}-${hi:.2f}:{'':>5} {len(bucket):>6} {len(bucket_flips):>6} {flip_rate*100:>6.1f}% {pnl:>+9.3f}")

    # 3. BTC misaligned + entry price (small sample but important)
    print("\n--- BTC MISALIGNED Events: Win Rate by Entry Price ---")
    misaligned = [d for d in data if not d["btc_aligned"]]
    for lo, hi in price_buckets:
        bucket = [d for d in misaligned if lo <= d["entry_price"] < hi]
        bucket_flips = [d for d in bucket if d["flip"]]
        if not bucket:
            continue
        flip_rate = len(bucket_flips) / len(bucket)
        pnl = sum((1.0 - d["entry_price"]) if d["flip"] else (-d["entry_price"]) for d in bucket)
        print(f"${lo:.2f}-${hi:.2f}:{'':>5} {len(bucket):>6} {len(bucket_flips):>6} {flip_rate*100:>6.1f}% {pnl:>+9.3f}")

    return data


def analyze_btc_runway(data):
    """Analyze BTC 'runway' - can BTC distance + time remaining predict flip?"""
    print("\n" + "=" * 80)
    print("BTC RUNWAY ANALYSIS")
    print("=" * 80)

    flips = [d for d in data if d["flip"]]
    noflips = [d for d in data if not d["flip"]]

    print("\nBTC runway = btc_speed * remaining_time")
    print("(Expected additional BTC move if current speed continues)")
    print(f"  Flips:     mean runway={np.mean([d['runway'] for d in flips]):.4f}")
    print(f"  Non-flips: mean runway={np.mean([d['runway'] for d in noflips]):.4f}")

    # But runway uses absolute speed. For aligned events, we want POSITIVE runway.
    aligned_flips = [d for d in flips if d["btc_aligned"]]
    aligned_noflips = [d for d in noflips if d["btc_aligned"]]

    print(f"\nBTC aligned events only:")
    print(f"  Flips:     btc_dist={np.mean([d['btc_dist'] for d in aligned_flips]):.4f}%, "
          f"cross_rem={np.mean([d['cross_rem'] for d in aligned_flips]):.0f}s, "
          f"btc_total={np.mean([d['btc_total'] for d in aligned_flips]):.4f}%")
    print(f"  Non-flips: btc_dist={np.mean([d['btc_dist'] for d in aligned_noflips]):.4f}%, "
          f"cross_rem={np.mean([d['cross_rem'] for d in aligned_noflips]):.0f}s, "
          f"btc_total={np.mean([d['btc_total'] for d in aligned_noflips]):.4f}%")

    # Key insight: btc_dist / btc_total ratio
    # For non-flips: btc_dist at cross is ~half of total (they continue)
    # For flips: btc_dist at cross is larger than total (they reverse)
    for d in aligned_flips:
        d["dist_ratio"] = d["btc_dist"] / max(abs(d["btc_total"]), 1e-10)
    for d in aligned_noflips:
        d["dist_ratio"] = d["btc_dist"] / max(abs(d["btc_total"]), 1e-10)

    print(f"\n  BTC dist / total ratio:")
    print(f"    Flips:     mean={np.mean([d['dist_ratio'] for d in aligned_flips]):.2f}")
    print(f"    Non-flips: mean={np.mean([d['dist_ratio'] for d in aligned_noflips]):.2f}")

    # Combined filters for aligned events
    print("\n--- Multi-Factor Analysis for Aligned Events ---")
    print(f"{'Filter':<55} {'Total':>6} {'Flips':>6} {'Flip%':>7} {'FadePnL':>9}")
    print("-" * 85)

    filters = [
        ("All aligned", lambda d: d["btc_aligned"]),
        ("Aligned + cross_rem < 60s", lambda d: d["btc_aligned"] and d["cross_rem"] < 60),
        ("Aligned + cross_rem < 30s", lambda d: d["btc_aligned"] and d["cross_rem"] < 30),
        ("Aligned + btc_dist < 0.005%", lambda d: d["btc_aligned"] and d["btc_dist"] < 0.005),
        ("Aligned + btc_dist < 0.01%", lambda d: d["btc_aligned"] and d["btc_dist"] < 0.01),
        ("Aligned + btc_dist < 0.02%", lambda d: d["btc_aligned"] and d["btc_dist"] < 0.02),
        ("Aligned + cross_rem < 60 + btc_dist < 0.01%", lambda d: d["btc_aligned"] and d["cross_rem"] < 60 and d["btc_dist"] < 0.01),
        ("Aligned + cross_rem < 60 + btc_dist < 0.02%", lambda d: d["btc_aligned"] and d["cross_rem"] < 60 and d["btc_dist"] < 0.02),
        ("Aligned + entry < $0.25", lambda d: d["btc_aligned"] and d["entry_price"] < 0.25),
        ("Aligned + entry < $0.20", lambda d: d["btc_aligned"] and d["entry_price"] < 0.20),
        ("Aligned + entry < $0.15", lambda d: d["btc_aligned"] and d["entry_price"] < 0.15),
        ("Aligned + entry < $0.25 + cross_rem < 60", lambda d: d["btc_aligned"] and d["entry_price"] < 0.25 and d["cross_rem"] < 60),
        ("Aligned + entry < $0.20 + btc_dist < 0.01%", lambda d: d["btc_aligned"] and d["entry_price"] < 0.20 and d["btc_dist"] < 0.01),
        ("Aligned + entry < $0.25 + btc_dist < 0.02%", lambda d: d["btc_aligned"] and d["entry_price"] < 0.25 and d["btc_dist"] < 0.02),
    ]

    for name, fn in filters:
        bucket = [d for d in data if fn(d)]
        bucket_flips = [d for d in bucket if d["flip"]]
        if len(bucket) < 3:
            continue
        pnl = sum((1.0 - d["entry_price"]) if d["flip"] else (-d["entry_price"]) for d in bucket)
        print(f"{name:<55} {len(bucket):>6} {len(bucket_flips):>6} {len(bucket_flips)/len(bucket)*100:>6.1f}% {pnl:>+9.3f}")


def analyze_two_stage_strategy(data):
    """Two-stage strategy: BTC misaligned → instant fade, BTC aligned → monitor for reversal."""
    print("\n" + "=" * 80)
    print("TWO-STAGE STRATEGY")
    print("=" * 80)

    print("\nStage 1: BTC misaligned at cross → instant fade")
    print("Stage 2: BTC aligned at cross → wait for reversal confirmation, then fade")

    # Stage 1: straightforward
    stage1 = [d for d in data if not d["btc_aligned"]]
    s1_pnl = sum((1.0 - d["entry_price"]) if d["flip"] else (-d["entry_price"]) for d in stage1)
    s1_wins = sum(1 for d in stage1 if d["flip"])
    print(f"\nStage 1: {len(stage1)} bets, {s1_wins} wins ({s1_wins/len(stage1)*100:.1f}%), PnL={s1_pnl:+.3f}")

    # Stage 2: We need the raw event data to check post-crossing BTC behavior
    # Can't do from pre-computed data
    # But we can approximate

    print("\nStage 2 candidates (aligned + low entry + late cross):")
    stage2_candidates = [d for d in data if d["btc_aligned"] and d["entry_price"] < 0.25 and d["cross_rem"] < 120]
    s2_flips = [d for d in stage2_candidates if d["flip"]]

    # If we faded all of them
    s2_pnl_all = sum((1.0 - d["entry_price"]) if d["flip"] else (-d["entry_price"]) for d in stage2_candidates)

    print(f"  Aligned + entry<$0.25 + rem<120s: {len(stage2_candidates)} bets, "
          f"{len(s2_flips)} flips ({len(s2_flips)/len(stage2_candidates)*100:.1f}%), "
          f"Fade PnL={s2_pnl_all:+.3f}")

    # What if we only fade the ones where target price stalls after crossing?
    # (We can't test this without raw event data here)

    # Combined strategy
    print(f"\nCombined (Stage 1 + Stage 2 candidates):")
    combined = stage1 + stage2_candidates
    combined_pnl = s1_pnl + s2_pnl_all
    combined_wins = s1_wins + len(s2_flips)
    print(f"  Total: {len(combined)} bets, {combined_wins} wins ({combined_wins/len(combined)*100:.1f}%), PnL={combined_pnl:+.3f}")


def analyze_target_price_stall(events):
    """When target price crosses 0.7, does it stall or continue?
    Stalling may indicate a false breakout."""
    print("\n" + "=" * 80)
    print("TARGET PRICE STALL ANALYSIS")
    print("=" * 80)

    results = []
    for event in events:
        snaps = event["snapshots"]
        open_price = event["open_price"]

        yes_series = [s["yes_price"] for s in snaps]
        no_series = [s["no_price"] for s in snaps]
        btc_prices = np.array([s["price"] for s in snaps])
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
        cross_idx = np.argmax(cross_mask)
        cross_rem = remaining_arr[cross_idx]

        if cross_idx + 10 >= len(target_series):
            continue

        # Post-crossing target price behavior
        # Check 10 seconds after crossing
        post_target = target_series[cross_idx:min(cross_idx+15, len(target_series))]
        post_btc = btc_prices[cross_idx:min(cross_idx+15, len(btc_prices))]

        # Target price change in 10s after crossing
        target_change_10s = post_target[min(10, len(post_target)-1)] - target_series[cross_idx]

        # Target price max in 10s after crossing
        target_max_10s = np.max(post_target[:min(10, len(post_target))])

        # Did target price stall (change < 0.02 in 10s)?
        stalled = target_change_10s < 0.02

        # Did target price retreat (change < -0.02 in 10s)?
        retreated = target_change_10s < -0.02

        # BTC direction
        btc_dist = (btc_prices[cross_idx] - open_price) / open_price * 100 * direction

        flip = event["outcome"] != (0 if favored == "YES" else 1)

        results.append({
            "flip": flip,
            "favored": favored,
            "btc_aligned": btc_dist > 0,
            "btc_dist": btc_dist,
            "stalled": stalled,
            "retreated": retreated,
            "target_change_10s": target_change_10s,
            "target_max_10s": target_max_10s,
            "cross_rem": cross_rem,
            "target_at_cross": target_series[cross_idx],
            "other_at_cross": other_series[cross_idx],
        })

    flips = [r for r in results if r["flip"]]
    noflips = [r for r in results if not r["flip"]]

    print(f"\nTarget price behavior 10s after crossing 0.7:")
    print(f"  Flips:     {sum(1 for r in flips if r['stalled'])}/{len(flips)} stalled, "
          f"{sum(1 for r in flips if r['retreated'])}/{len(flips)} retreated")
    print(f"  Non-flips: {sum(1 for r in noflips if r['stalled'])}/{len(noflips)} stalled, "
          f"{sum(1 for r in noflips if r['retreated'])}/{len(noflips)} retreated")
    print(f"  Mean target change 10s: flips={np.mean([r['target_change_10s'] for r in flips]):.4f}, "
          f"noflips={np.mean([r['target_change_10s'] for r in noflips]):.4f}")

    # Now test: what if we fade when target price stalls after crossing?
    print(f"\n--- Fade when target price stalls after crossing ---")
    strategies = [
        ("Stall (target_change < 0.02 in 10s)", lambda r: r["stalled"]),
        ("Retreat (target_change < -0.02 in 10s)", lambda r: r["retreated"]),
        ("Stall + BTC aligned", lambda r: r["stalled"] and r["btc_aligned"]),
        ("Retreat + BTC aligned", lambda r: r["retreated"] and r["btc_aligned"]),
        ("Stall + cross_rem > 30s", lambda r: r["stalled"] and r["cross_rem"] > 30),
        ("Stall + BTC misaligned", lambda r: r["stalled"] and not r["btc_aligned"]),
    ]

    for name, fn in strategies:
        bucket = [r for r in results if fn(r) and r["cross_rem"] > 15]
        if len(bucket) < 3:
            continue
        pnl = sum((1.0 - r["other_at_cross"]) if r["flip"] else (-r["other_at_cross"]) for r in bucket)
        wins = sum(1 for r in bucket if r["flip"])
        print(f"  {name:<50} {len(bucket):>4} bets, {wins:>3} wins ({wins/len(bucket)*100:>4.1f}%), PnL={pnl:>+8.3f}")


def final_strategy_summary(data):
    """Print final recommended strategy parameters."""
    print("\n" + "=" * 80)
    print("FINAL STRATEGY RECOMMENDATIONS")
    print("=" * 80)

    # Best single filter: BTC misaligned
    misaligned = [d for d in data if not d["btc_aligned"] and d["entry_price"] > 0.01]
    s1_pnl = sum((1.0 - d["entry_price"]) if d["flip"] else (-d["entry_price"]) for d in misaligned)
    s1_wins = sum(1 for d in misaligned if d["flip"])

    print(f"""
Strategy A: "Pure Contrarian"
  Signal: BTC direction OPPOSITE to favored side at 0.7 cross
  Action: Fade (bet against strong side)
  Result: {len(misaligned)} trades, {s1_wins} wins ({s1_wins/len(misaligned)*100:.1f}%), PnL={s1_pnl:+.3f}
  Frequency: ~{len(misaligned)/416*100:.1f}% of markets
  Entry: At other side's price when target crosses 0.7
  Risk: Small sample (18 trades), but strong edge

Strategy B: "Late Weak Signal"
  Signal: BTC aligned but barely (dist<0.01%) AND crosses late (<60s rem)
  Action: Fade
  Filter: entry_price < $0.25 for better risk/reward
  Backtest needed with proper entry timing

Strategy C: "Target Price Stall"
  Signal: Target price crosses 0.7, then stalls (doesn't rise further)
  Action: Fade at stall confirmation
  This uses the target price behavior itself as confirmation
""")

    # Frequency analysis
    total = len(data)
    print(f"\nSignal Frequency Analysis ({total} total events):")
    print(f"  BTC misaligned at cross:          {len(misaligned)} events ({len(misaligned)/total*100:.1f}%)")
    aligned = [d for d in data if d["btc_aligned"]]
    late_aligned = [d for d in aligned if d["cross_rem"] < 60]
    weak_aligned = [d for d in aligned if d["btc_dist"] < 0.01]
    late_weak = [d for d in aligned if d["cross_rem"] < 60 and d["btc_dist"] < 0.01]
    cheap_entry = [d for d in aligned if d["entry_price"] < 0.20]
    print(f"  BTC aligned + cross < 60s:        {len(late_aligned)} events ({len(late_aligned)/total*100:.1f}%)")
    print(f"  BTC aligned + dist < 0.01%:       {len(weak_aligned)} events ({len(weak_aligned)/total*100:.1f}%)")
    print(f"  BTC aligned + late + weak:        {len(late_weak)} events ({len(late_weak)/total*100:.1f}%)")
    print(f"  BTC aligned + entry < $0.20:      {len(cheap_entry)} events ({len(cheap_entry)/total*100:.1f}%)")

    # For late_weak events, what's the flip rate?
    late_weak_flips = [d for d in late_weak if d["flip"]]
    lw_pnl = sum((1.0 - d["entry_price"]) if d["flip"] else (-d["entry_price"]) for d in late_weak)
    if late_weak:
        print(f"    → Flip rate: {len(late_weak_flips)}/{len(late_weak)} ({len(late_weak_flips)/len(late_weak)*100:.1f}%), Fade PnL={lw_pnl:+.3f}")


def main():
    events = load_all_events()
    print(f"Loaded {len(events)} events")

    data = analyze_entry_price_vs_flip(events)
    analyze_btc_runway(data)
    analyze_two_stage_strategy(data)
    analyze_target_price_stall(events)
    final_strategy_summary(data)

    print("\nDone!")


if __name__ == "__main__":
    main()
