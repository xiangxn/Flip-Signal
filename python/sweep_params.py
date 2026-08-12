#!/usr/bin/env python3
"""
参数网格搜索 — 寻找最优 FlipBacktestConfig 组合。
优化目标: 胜率 + P&L 综合最优。

用法:
  python sweep_params.py --data ../data_0/lab/
"""

import argparse
import itertools
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_flip_config import FlipBacktestConfig
from backtest_flip_utils import (
    load_events_from_dir,
    run_backtest,
    print_summary,
)


def sweep_1d(events, cfg_template, param_name, values):
    """对单个参数进行 1D sweep，返回所有结果。"""
    results = []
    for val in values:
        cfg = FlipBacktestConfig(**{**cfg_template.__dict__, param_name: val})
        signals, total, active = run_backtest(events, cfg)
        n = len(signals)
        wins = sum(1 for s in signals if s.won)
        total_pnl = sum(s.pnl for s in signals)
        win_rate = wins / n * 100 if n > 0 else 0
        results.append((val, n, wins, total_pnl, win_rate))
    return results


def print_sweep_table(title, results, val_label="Value"):
    """格式化输出 sweep 结果表。"""
    print(f"\n{'='*75}")
    print(f"  {title}")
    print(f"{'='*75}")
    print(f"  {val_label:>20s} │ {'n':>5s} │ {'Win':>5s} │ {'Win%':>6s} │ {'P&L':>8s} │ {'Avg P&L/sig':>12s}")
    print("-" * 75)
    for val, n, wins, total_pnl, win_rate in results:
        avg_pl = total_pnl / n if n > 0 else 0
        best_val = max(results, key=lambda x: x[3] if x[1] > 0 else -999)[0]
        marker = " 👈 BEST" if val == best_val else ""
        print(f"  {str(val):>20} │ {n:>5d} │ {wins:>5d} │ {win_rate:>5.1f}% │ {total_pnl:>+8.2f} │ {avg_pl:>+12.3f}{marker}")
    print("-" * 75)


def get_best_by_pnl(results):
    """按 P&L 排序返回最佳结果 (需至少有 10 个信号)。"""
    valid = [r for r in results if r[1] >= 10]
    if not valid:
        valid = [r for r in results if r[1] > 0]
    return max(valid, key=lambda x: x[3]) if valid else results[0]


def main():
    parser = argparse.ArgumentParser(description="参数网格搜索")
    parser.add_argument("--data", default="../data_0/lab/", help="JSONL 数据目录")
    args = parser.parse_args()

    print(f"加载数据: {args.data}")
    events = load_events_from_dir(args.data)
    print(f"  → {len(events)} 个事件")

    base = FlipBacktestConfig()

    # ═══════════════════════════════════════════════════════════════
    # Round 1: 单维度 sweep
    # ═══════════════════════════════════════════════════════════════

    # 1. Score entry threshold
    results = sweep_1d(events, base, "score_entry", [3, 4, 5, 6, 7, 8])
    print_sweep_table("1. Score Entry Threshold", results)

    # 2. Confirm delay ticks
    results = sweep_1d(events, base, "confirm_delay_ticks", [1, 2, 3, 4, 5])
    print_sweep_table("2. Confirm Delay Ticks (T+N)", results)

    # 3. path_eff oscillating threshold
    results = sweep_1d(events, base, "path_eff_oscillating", [0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
    print_sweep_table("3. Path Eff Oscillating Threshold (≤)", results)

    # 4. noise_ratio oscillating threshold
    results = sweep_1d(events, base, "noise_ratio_oscillating", [0.5, 1.0, 1.5, 2.0, 2.5, 3.0])
    print_sweep_table("4. Noise Ratio Oscillating Threshold (>)", results)

    # 5. flips oscillating
    results = sweep_1d(events, base, "flips_oscillating", [0, 1, 2, 3, 4])
    print_sweep_table("5. Flips Oscillating Threshold (>)", results)

    # 6. range_exp_threshold
    results = sweep_1d(events, base, "range_exp_threshold", [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8])
    print_sweep_table("6. Range Expansion Threshold (<)", results)

    # 7. range_exp_max (F0 veto)
    results = sweep_1d(events, base, "range_exp_max", [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0])
    print_sweep_table("7. Range Expansion Max (F0 Veto ≥)", results)

    # 8. btc_pos thresholds (symmetric)
    for pos_val in [0.0, 0.05, 0.1, 0.15, 0.2, 0.25]:
        cfg = FlipBacktestConfig(**{**base.__dict__,
            "btc_pos_min": -pos_val, "btc_pos_max": pos_val})
        signals, total, active = run_backtest(events, cfg)
        n = len(signals)
        wins = sum(1 for s in signals if s.won)
        total_pnl = sum(s.pnl for s in signals)
        win_rate = wins / n * 100 if n > 0 else 0
        if pos_val == 0:
            results = []
        results.append((pos_val, n, wins, total_pnl, win_rate))
    print_sweep_table("8. BTC Position Threshold (±)", results)

    # 9. entry_cheap thresholds
    for cheap_strong, cheap_weak in [(0.10, 0.15), (0.15, 0.20), (0.15, 0.25),
                                      (0.20, 0.25), (0.20, 0.30), (0.25, 0.30)]:
        cfg = FlipBacktestConfig(**{**base.__dict__,
            "entry_cheap_strong": cheap_strong, "entry_cheap_weak": cheap_weak})
        signals, total, active = run_backtest(events, cfg)
        n = len(signals)
        wins = sum(1 for s in signals if s.won)
        total_pnl = sum(s.pnl for s in signals)
        win_rate = wins / n * 100 if n > 0 else 0
        if cheap_strong == 0.10:
            results = []
        results.append((f"{cheap_strong}/{cheap_weak}", n, wins, total_pnl, win_rate))
    print_sweep_table("9. Entry Cheap Thresholds (strong/weak)", results, "Thresholds")

    # 10. Other delta thresholds
    for vstrong, strong, weak in [(0.03, 0.02, 0.01), (0.05, 0.03, 0.01),
                                    (0.05, 0.02, 0.01), (0.07, 0.03, 0.015),
                                    (0.10, 0.05, 0.02), (0.05, 0.02, 0.005)]:
        cfg = FlipBacktestConfig(**{**base.__dict__,
            "other_delta_vstrong": vstrong,
            "other_delta_strong": strong,
            "other_delta_weak": weak})
        signals, total, active = run_backtest(events, cfg)
        n = len(signals)
        wins = sum(1 for s in signals if s.won)
        total_pnl = sum(s.pnl for s in signals)
        win_rate = wins / n * 100 if n > 0 else 0
        if vstrong == 0.03:
            results = []
        results.append((f"v{vstrong}/s{strong}/w{weak}", n, wins, total_pnl, win_rate))
    print_sweep_table("10. Other Delta Thresholds", results, "Thresholds")

    # 11. trigger_threshold
    results = sweep_1d(events, base, "trigger_threshold", [0.60, 0.65, 0.70, 0.75, 0.80])
    print_sweep_table("11. Trigger Threshold", results)

    # 12. max_remaining_sec
    results = sweep_1d(events, base, "max_remaining_sec", [120, 180, 200, 240, 260, 280])
    print_sweep_table("12. Max Remaining Seconds", results)

    # 13. Oscillating weight
    results = sweep_1d(events, base, "w_oscillating", [0, 1, 2, 3])
    print_sweep_table("13. Oscillating Weight", results)

    # ═══════════════════════════════════════════════════════════════
    # Round 2: Combined sweep — best parameters together
    # ═══════════════════════════════════════════════════════════════
    print(f"\n\n{'#'*75}")
    print(f"# Round 2: Combined Parameter Sweep")
    print(f"{'#'*75}")

    # Based on round 1, combine promising values
    score_entries = [5, 6]
    path_eff_vals = [0.6, 0.7]
    noise_ratio_vals = [1.5, 2.0]
    flips_vals = [1, 2]
    range_exp_thresholds = [0.4, 0.5]
    range_exp_max_vals = [1.5, 2.0]
    btc_pos_vals = [0.1, 0.15]
    w_osc_vals = [1, 2]

    combinations = list(itertools.product(
        score_entries, path_eff_vals, noise_ratio_vals, flips_vals,
        range_exp_thresholds, range_exp_max_vals, btc_pos_vals, w_osc_vals
    ))

    print(f"共 {len(combinations)} 种组合，正在搜索...")

    all_results = []
    for score_e, pe, nr, fl, re_th, re_max, bp, wo in combinations:
        cfg = FlipBacktestConfig(**{
            **base.__dict__,
            "score_entry": score_e,
            "path_eff_oscillating": pe,
            "noise_ratio_oscillating": nr,
            "flips_oscillating": fl,
            "range_exp_threshold": re_th,
            "range_exp_max": re_max,
            "btc_pos_min": -bp,
            "btc_pos_max": bp,
            "w_oscillating": wo,
        })
        signals, total, active = run_backtest(events, cfg)
        n = len(signals)
        wins = sum(1 for s in signals if s.won)
        total_pnl = sum(s.pnl for s in signals)
        win_rate = wins / n * 100 if n > 0 else 0
        all_results.append({
            "cfg": cfg, "n": n, "wins": wins, "pnl": total_pnl, "wr": win_rate
        })

    # Sort: by P&L desc, but only if n >= 10
    valid_results = [r for r in all_results if r["n"] >= 10]
    valid_results.sort(key=lambda r: r["pnl"], reverse=True)

    print(f"\nTop 20 by P&L (最少 10 个信号):")
    print(f"{'Rank':>4s} │ {'n':>5s} │ {'Win':>5s} │ {'Win%':>6s} │ {'P&L':>8s} │ "
          f"{'Avg/sig':>8s} │ score│ path_eff│ noise│flips│range_th│range_max│btc_pos│w_osc")
    print("-" * 110)
    for i, r in enumerate(valid_results[:20]):
        cfg = r["cfg"]
        avg = r["pnl"] / r["n"]
        print(f"  {i+1:>2d} │ {r['n']:>5d} │ {r['wins']:>5d} │ {r['wr']:>5.1f}% │ "
              f"{r['pnl']:>+8.2f} │ {avg:>+8.3f} │ "
              f" {cfg.score_entry:>3d} │  {cfg.path_eff_oscillating:>5.1f}  │ "
              f" {cfg.noise_ratio_oscillating:>3.1f} │ {cfg.flips_oscillating:>3d} │ "
              f"  {cfg.range_exp_threshold:>5.1f}  │   {cfg.range_exp_max:>5.1f}   │ "
              f"  {abs(cfg.btc_pos_min):>5.2f} │  {cfg.w_oscillating:>3d}")

    # Also sort by win rate
    valid_results.sort(key=lambda r: r["wr"], reverse=True)
    print(f"\n\nTop 20 by Win Rate (最少 10 个信号):")
    print(f"{'Rank':>4s} │ {'n':>5s} │ {'Win':>5s} │ {'Win%':>6s} │ {'P&L':>8s} │ "
          f"{'Avg/sig':>8s} │ score│ path_eff│ noise│flips│range_th│range_max│btc_pos│w_osc")
    print("-" * 110)
    for i, r in enumerate(valid_results[:20]):
        cfg = r["cfg"]
        avg = r["pnl"] / r["n"]
        print(f"  {i+1:>2d} │ {r['n']:>5d} │ {r['wins']:>5d} │ {r['wr']:>5.1f}% │ "
              f"{r['pnl']:>+8.2f} │ {avg:>+8.3f} │ "
              f" {cfg.score_entry:>3d} │  {cfg.path_eff_oscillating:>5.1f}  │ "
              f" {cfg.noise_ratio_oscillating:>3.1f} │ {cfg.flips_oscillating:>3d} │ "
              f"  {cfg.range_exp_threshold:>5.1f}  │   {cfg.range_exp_max:>5.1f}   │ "
              f"  {abs(cfg.btc_pos_min):>5.2f} │  {cfg.w_oscillating:>3d}")


    # ═══════════════════════════════════════════════════════════════
    # Round 3: Detailed analysis of BEST config
    # ═══════════════════════════════════════════════════════════════
    valid_results.sort(key=lambda r: r["pnl"], reverse=True)
    best = valid_results[0]

    print(f"\n\n{'#'*75}")
    print(f"# 最佳参数配置详情")
    print(f"{'#'*75}")
    cfg = best["cfg"]
    print(f"""
    # 配置参数:
    score_entry = {cfg.score_entry}
    path_eff_oscillating = {cfg.path_eff_oscillating}
    noise_ratio_oscillating = {cfg.noise_ratio_oscillating}
    flips_oscillating = {cfg.flips_oscillating}
    range_exp_threshold = {cfg.range_exp_threshold}
    range_exp_max = {cfg.range_exp_max}
    btc_pos_min/max = {cfg.btc_pos_min}/{cfg.btc_pos_max}
    w_oscillating = {cfg.w_oscillating}
    """)

    signals, total, active = run_backtest(events, cfg)
    print_summary(signals, total, active, events)

    # Compare with baseline
    base_signals, _, _ = run_backtest(events, base)
    base_n = len(base_signals)
    base_wins = sum(1 for s in base_signals if s.won)
    base_pnl = sum(s.pnl for s in base_signals)
    base_wr = base_wins / base_n * 100 if base_n > 0 else 0

    print(f"\n{'='*60}")
    print(f"  Baseline vs Optimized 对比")
    print(f"{'='*60}")
    print(f"  {'':>12s} │ {'n':>5s} │ {'Win%':>6s} │ {'P&L':>8s}")
    print(f"  {'Baseline':>12s} │ {base_n:>5d} │ {base_wr:>5.1f}% │ {base_pnl:>+8.2f}")
    print(f"  {'Optimized':>12s} │ {best['n']:>5d} │ {best['wr']:>5.1f}% │ {best['pnl']:>+8.2f}")
    print(f"  {'Improvement':>12s} │ {best['n']-base_n:>+5d} │ {best['wr']-base_wr:>+5.1f}% │ {best['pnl']-base_pnl:>+8.2f}")


if __name__ == "__main__":
    main()
