#!/usr/bin/env python3
"""
Final: Detailed analysis of top 3 configs with verbose output.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_flip_config import FlipBacktestConfig
from backtest_flip_utils import (
    load_events_from_dir,
    run_backtest,
    print_summary,
    export_signals_jsonl,
)


def main():
    events = load_events_from_dir("../data_0/lab/")

    base = FlipBacktestConfig()

    # Config A: Highest P&L (aggressive)
    cfg_a = FlipBacktestConfig(**{
        **base.__dict__,
        "confirm_delay_ticks": 5,
        "score_entry": 4,
        "path_eff_oscillating": 0.8,
        "noise_ratio_oscillating": 1.5,
        "flips_oscillating": 0,
        "range_exp_max": 2.5,
        "range_exp_threshold": 0.5,
        "btc_pos_min": -0.1,
        "btc_pos_max": 0.1,
        "w_oscillating": 1,
    })

    # Config B: Balanced P&L / WR (moderate)
    cfg_b = FlipBacktestConfig(**{
        **base.__dict__,
        "confirm_delay_ticks": 5,
        "score_entry": 5,
        "path_eff_oscillating": 0.6,
        "noise_ratio_oscillating": 2.5,
        "flips_oscillating": 2,
        "range_exp_max": 1.5,
        "range_exp_threshold": 0.5,
        "btc_pos_min": -0.1,
        "btc_pos_max": 0.1,
        "w_oscillating": 1,
    })

    # Config C: Highest WR (conservative)
    cfg_c = FlipBacktestConfig(**{
        **base.__dict__,
        "confirm_delay_ticks": 5,
        "score_entry": 5,
        "path_eff_oscillating": 0.7,
        "noise_ratio_oscillating": 1.5,
        "flips_oscillating": 2,
        "range_exp_max": 1.5,
        "range_exp_threshold": 0.5,
        "btc_pos_min": -0.1,
        "btc_pos_max": 0.1,
        "w_oscillating": 1,
    })

    # Config D: With entry threshold tightening (fewer but better)
    cfg_d = FlipBacktestConfig(**{
        **base.__dict__,
        "confirm_delay_ticks": 5,
        "score_entry": 5,
        "path_eff_oscillating": 0.6,
        "noise_ratio_oscillating": 1.5,
        "flips_oscillating": 2,
        "range_exp_max": 1.5,
        "range_exp_threshold": 0.5,
        "btc_pos_min": -0.15,
        "btc_pos_max": 0.15,
        "w_oscillating": 1,
    })

    configs = [
        ("Baseline (当前)", base),
        ("A: 最高P&L (激进)", cfg_a),
        ("B: P&L/胜率均衡", cfg_b),
        ("C: 高胜率 (50.9%!)", cfg_c),
        ("D: 精选信号", cfg_d),
    ]

    all_signals = {}
    print(f"{'='*70}")
    print(f"  多种配置对比分析")
    print(f"{'='*70}")

    for name, cfg in configs:
        print(f"\n{'─'*70}")
        print(f"  [{name}]")
        print(f"{'─'*70}")
        print(f"  confirm_delay={cfg.confirm_delay_ticks}, score_entry≥{cfg.score_entry}")
        print(f"  path_eff≤{cfg.path_eff_oscillating}, noise>{cfg.noise_ratio_oscillating}, "
              f"flips>{cfg.flips_oscillating}")
        print(f"  range_exp<{cfg.range_exp_threshold}(+{cfg.w_range_expansion}), "
              f"range_exp≥{cfg.range_exp_max}(veto)")
        print(f"  btc_pos=±{abs(cfg.btc_pos_min)}, w_osc={cfg.w_oscillating}")

        signals, total, active = run_backtest(events, cfg)
        all_signals[name] = signals
        print_summary(signals, total, active)

    # Summary comparison table
    print(f"\n\n{'='*70}")
    print(f"  最终对比表")
    print(f"{'='*70}")
    print(f"  {'配置':>20s} │ {'n':>5s} │ {'Win%':>6s} │ {'P&L':>8s} │ {'Avg/sig':>8s} │ {'P&L提升':>8s}")
    print(f"  {'-'*20}─┼{'─'*7}─┼{'─'*8}─┼{'─'*10}─┼{'─'*10}─┼{'─'*10}")
    base_pnl = sum(s.pnl for s in all_signals["Baseline (当前)"])
    for name, _ in configs:
        s = all_signals[name]
        n = len(s)
        wins = sum(1 for x in s if x.won)
        pnl = sum(x.pnl for x in s)
        wr = wins / n * 100 if n > 0 else 0
        avg_sig = pnl / n if n > 0 else 0
        improvement = pnl - base_pnl
        print(f"  {name:>20s} │ {n:>5d} │ {wr:>5.1f}% │ {pnl:>+8.2f} │ {avg_sig:>+8.3f} │ {improvement:>+8.2f}")

    # Export best config signals
    print(f"\n\n导出 Config C 信号到 sweep_best_config_c.jsonl ...")
    export_signals_jsonl(all_signals["C: 高胜率 (50.9%!)"], "sweep_best_config_c.jsonl")
    print("完成。")


if __name__ == "__main__":
    main()
