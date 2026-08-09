#!/usr/bin/env python3
"""
复合评分版翻转信号回测

基于 docs/flip_backtest_plan.md — 纯实时（无未来信息）的翻转信号回测。

核心思路:
  - 当 Polymarket YES 或 NO 价格首次突破 0.7 时触发信号检测
  - 用穿越前的 BTC 价格路径判断是否为来回振荡（趋势衰竭）
  - 结合对面价格确认、振幅扩张、入场价等 7 个特征评分
  - score ≥ 5 开仓, ≥ 7 加仓

Usage:
    python backtest_flip_scoring.py --data ../data/
    python backtest_flip_scoring.py --data ../data/ --export signals.jsonl
    python backtest_flip_scoring.py --data ../data/ --verbose  # 打印每笔信号详情
"""

import argparse
import sys
from pathlib import Path

# 确保可以从 python/ 目录运行
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_flip_config import FlipBacktestConfig, DEFAULT_CONFIG, ETH_CONFIG
from backtest_flip_utils import (
    load_events_from_dir,
    run_backtest,
    print_summary,
    export_signals_jsonl,
    FlipSignal,
)


def main():
    parser = argparse.ArgumentParser(
        description="复合评分版翻转信号回测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--data", default="../data/btc/",
        help="JSONL 数据目录 (默认: ../data/btc/)"
    )
    parser.add_argument(
        "--export", default=None,
        help="导出信号到 JSONL 文件"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="打印每笔信号详情"
    )
    parser.add_argument(
        "--profile", choices=["btc", "eth"], default="btc",
        help="参数预设 (默认: btc)"
    )
    args = parser.parse_args()

    # ── 加载数据 ──
    print(f"加载数据: {args.data}")
    events = load_events_from_dir(args.data)
    print(f"  → {len(events)} 个事件")
    if not events:
        print("无事件数据，退出。")
        return

    # 统计
    n_up = sum(1 for e in events if e["outcome"] == 0)
    n_down = len(events) - n_up
    n_with_hist = sum(1 for e in events if e.get("hist_avg_range") is not None)
    print(f"  → UP: {n_up}, DOWN: {n_down}")
    print(f"  → 有历史振幅数据: {n_with_hist}/{len(events)}")

    # ── 运行回测 ──
    cfg = DEFAULT_CONFIG if args.profile == "btc" else ETH_CONFIG
    print(f"\n运行回测 [{args.profile.upper()}] (trigger>{cfg.trigger_threshold}, "
          f"confirm_delay={cfg.confirm_delay_ticks}tick, "
          f"score_entry≥{cfg.score_entry})...")
    signals, total_events, active_events = run_backtest(events, cfg)

    # ── 输出结果 ──
    print_summary(signals, total_events, active_events)

    if args.verbose and signals:
        print("\n逐笔信号:")
        print("-" * 100)
        for s in signals:
            direction = "买NO赌DOWN" if s.side == "yes" else "买YES赌UP"
            outcome_icon = "✅" if s.won else "❌"
            print(
                f"  {outcome_icon} {s.side.upper():>3s}>0.7 | "
                f"score={s.score} | entry={s.entry_price:.4f} | "
                f"shares={s.shares} | P&L={s.pnl:+.3f} | "
                f"rem={s.remaining_sec:>3d}s | "
                f"path_eff={s.path_eff:.2f} noise={s.noise_ratio:.1f} "
                f"flips={s.flips} | "
                f"osc={s.is_oscillating} | "
                f"range_exp={s.range_expansion:.1f} | "
                f"btc_pos={s.btc_position:.2f} {s.btc_extreme} | "
                f"other_d={s.other_delta:+.4f} | "
                f"{direction}"
            )
        print("-" * 100)

    # ── 导出 ──
    if args.export:
        export_signals_jsonl(signals, args.export)
        print(f"\n信号已导出: {args.export} ({len(signals)} 条)")


if __name__ == "__main__":
    main()
