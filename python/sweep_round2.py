#!/usr/bin/env python3
"""
Round 2: Focused sweep including confirm_delay_ticks + best promising params.
Run after sweep_params.py reveals confirm_delay=5 as the best lever.
"""

import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_flip_config import FlipBacktestConfig
from backtest_flip_utils import (
    load_events_from_dir,
    run_backtest,
    print_summary,
)


def main():
    data_dir = "../data_0/lab/"
    print(f"加载数据: {data_dir}")
    events = load_events_from_dir(data_dir)
    print(f"  → {len(events)} 个事件")

    base = FlipBacktestConfig()

    # Parameters to sweep
    confirm_delays = [3, 4, 5]
    score_entries = [4, 5, 6]
    path_eff_vals = [0.6, 0.7, 0.8]
    noise_ratio_vals = [1.5, 2.0, 2.5]
    flips_vals = [0, 1, 2]
    range_exp_max_vals = [1.5, 2.0, 2.5]
    btc_pos_vals = [0.1, 0.15]

    # Also test specific range_exp_threshold
    range_exp_th_vals = [0.4, 0.5]

    all_combinations = list(itertools.product(
        confirm_delays, score_entries, path_eff_vals, noise_ratio_vals,
        flips_vals, range_exp_max_vals, btc_pos_vals, range_exp_th_vals
    ))

    print(f"共 {len(all_combinations)} 种组合，正在搜索...")

    all_results = []
    for cd, se, pe, nr, fl, re_max, bp, re_th in all_combinations:
        cfg = FlipBacktestConfig(**{
            **base.__dict__,
            "confirm_delay_ticks": cd,
            "score_entry": se,
            "path_eff_oscillating": pe,
            "noise_ratio_oscillating": nr,
            "flips_oscillating": fl,
            "range_exp_max": re_max,
            "range_exp_threshold": re_th,
            "btc_pos_min": -bp,
            "btc_pos_max": bp,
            "w_oscillating": 1,  # Round 1 showed w_osc=1 is slightly better
        })
        signals, total, active = run_backtest(events, cfg)
        n = len(signals)
        wins = sum(1 for s in signals if s.won)
        total_pnl = sum(s.pnl for s in signals)
        win_rate = wins / n * 100 if n > 0 else 0
        all_results.append({
            "cfg": cfg, "n": n, "wins": wins, "pnl": total_pnl, "wr": win_rate,
            "cd": cd, "se": se, "pe": pe, "nr": nr, "fl": fl,
            "re_max": re_max, "bp": bp, "re_th": re_th
        })

    # Filter: at least 30 signals (reasonable daily avg)
    valid_results = [r for r in all_results if r["n"] >= 30]
    valid_results.sort(key=lambda r: r["pnl"], reverse=True)

    print(f"\nTop 30 by P&L (最少 30 个信号):")
    header = (f"{'Rank':>4s} │ {'n':>5s} │ {'Win':>5s} │ {'Win%':>6s} │ {'P&L':>8s} │ "
              f"{'Avg/sig':>8s} │cd│se│pe │ nr │fl│rmax│bp │rth")
    print(header)
    print("-" * len(header))
    for i, r in enumerate(valid_results[:30]):
        cd = r["cd"]; se = r["se"]; pe = r["pe"]; nr = r["nr"]
        fl = r["fl"]; re_max = r["re_max"]; bp = r["bp"]; re_th = r["re_th"]
        avg = r["pnl"] / r["n"]
        print(f"  {i+1:>2d} │ {r['n']:>5d} │ {r['wins']:>5d} │ {r['wr']:>5.1f}% │ "
              f"{r['pnl']:>+8.2f} │ {avg:>+8.3f} │{cd:>2d}│{se:>2d}│{pe:>4.1f}│{nr:>4.1f}│"
              f"{fl:>2d}│{re_max:>4.1f}│{bp:>4.2f}│{re_th:>4.1f}")

    # Best by win rate
    valid_results.sort(key=lambda r: r["wr"], reverse=True)
    print(f"\n\nTop 30 by Win Rate (最少 30 个信号):")
    print(header)
    print("-" * len(header))
    for i, r in enumerate(valid_results[:30]):
        cd = r["cd"]; se = r["se"]; pe = r["pe"]; nr = r["nr"]
        fl = r["fl"]; re_max = r["re_max"]; bp = r["bp"]; re_th = r["re_th"]
        avg = r["pnl"] / r["n"]
        print(f"  {i+1:>2d} │ {r['n']:>5d} │ {r['wins']:>5d} │ {r['wr']:>5.1f}% │ "
              f"{r['pnl']:>+8.2f} │ {avg:>+8.3f} │{cd:>2d}│{se:>2d}│{pe:>4.1f}│{nr:>4.1f}│"
              f"{fl:>2d}│{re_max:>4.1f}│{bp:>4.2f}│{re_th:>4.1f}")

    # ═══════════════════════════════════════════════════════════════
    # Best Config DETAIL
    # ═══════════════════════════════════════════════════════════════
    valid_results.sort(key=lambda r: r["pnl"], reverse=True)
    best = valid_results[0]
    cfg = best["cfg"]

    print(f"\n\n{'#'*65}")
    print(f"# 🏆 最优参数配置 (最高 P&L)")
    print(f"{'#'*65}")
    print(f"""
    confirm_delay_ticks = {cfg.confirm_delay_ticks}
    score_entry = {cfg.score_entry}
    path_eff_oscillating = {cfg.path_eff_oscillating}
    noise_ratio_oscillating = {cfg.noise_ratio_oscillating}
    flips_oscillating = {cfg.flips_oscillating}
    range_exp_threshold = {cfg.range_exp_threshold}
    range_exp_max = {cfg.range_exp_max}
    btc_pos_min/max = {cfg.btc_pos_min}/{cfg.btc_pos_max}
    w_oscillating = {cfg.w_oscillating}
    trigger_threshold = {cfg.trigger_threshold}
    max_remaining_sec = {cfg.max_remaining_sec}
    entry_cheap_strong/weak = {cfg.entry_cheap_strong}/{cfg.entry_cheap_weak}
    """)

    signals, total, active = run_backtest(events, cfg)
    print_summary(signals, total, active)

    # Compare vs baseline
    base_signals, _, _ = run_backtest(events, base)
    base_n = len(base_signals)
    base_wins = sum(1 for s in base_signals if s.won)
    base_pnl = sum(s.pnl for s in base_signals)
    base_wr = base_wins / base_n * 100 if base_n > 0 else 0

    print(f"\n{'='*60}")
    print(f"  Baseline vs Optimized 对比")
    print(f"{'='*60}")
    print(f"  {'':>12s} │ {'n':>5s} │ {'Win%':>6s} │ {'P&L':>8s} │ {'Avg/sig':>8s}")
    print("-" * 60)
    print(f"  {'Baseline':>12s} │ {base_n:>5d} │ {base_wr:>5.1f}% │ {base_pnl:>+8.2f} │ {base_pnl/base_n:>+8.3f}")
    print(f"  {'Optimized':>12s} │ {best['n']:>5d} │ {best['wr']:>5.1f}% │ {best['pnl']:>+8.2f} │ {best['pnl']/best['n']:>+8.3f}")
    if best['pnl'] > base_pnl:
        pnl_improve = best['pnl'] - base_pnl
        print(f"  {'Improvement':>12s} │ {'--':>5s} │ {best['wr']-base_wr:>+5.1f}% │ {pnl_improve:>+8.2f} │ {'--':>8s}")
        print(f"\n  🔥 P&L 提升: {pnl_improve:+.2f} ({pnl_improve/base_pnl*100:+.1f}%)")

    # Also print the best by WR for comparison
    valid_results.sort(key=lambda r: r["wr"], reverse=True)
    best_wr = valid_results[0]
    print(f"\n  📊 最高胜率配置: n={best_wr['n']}, WR={best_wr['wr']:.1f}%, P&L={best_wr['pnl']:+.2f}")


if __name__ == "__main__":
    main()
