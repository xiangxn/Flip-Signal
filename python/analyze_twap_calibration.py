#!/usr/bin/env python3
"""
Phase 3 首轮分析：TWAP 结算口径下的基差量化与回测对照（2026-08-14/15 采集数据）。

三部分：
  1. 数据质量 —— 官方 TWAP 开/收盘覆盖率、采样完整性
  2. 基差量化 —— TWAP vs Binance 胜负分歧率（按振幅分桶）、振幅尺度比
  3. 回测对照 —— 四组合（特征口径 × 结算口径）Formula B 表现

用法:
    python analyze_twap_calibration.py --data ../data/btc
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_flip_config import FlipBacktestConfig
from backtest_flip_utils import (
    load_events_from_dir,
    compute_hist_avg_range,
    run_backtest,
    print_summary,
)


def data_quality(events):
    print("=" * 58)
    print("  1. 数据质量")
    print("=" * 58)
    n = len(events)
    t0, t1 = min(e["start_time"] for e in events), max(e["start_time"] for e in events)
    print(f"  事件数: {n}  |  跨度: {(t1 - t0) / 3600:.1f} 小时")

    with_official = sum(
        1 for e in events
        if e.get("twap_open_price") and e.get("twap_close_price")
    )
    with_binance_out = sum(1 for e in events if "binance_outcome" in e)
    print(f"  官方 TWAP 开+收盘: {with_official}/{n}  ({with_official / n * 100:.1f}%)")
    print(f"  binance_outcome 字段: {with_binance_out}/{n}")

    snaps = [len(e["snapshots"]) for e in events]
    print(f"  快照数/事件: min={min(snaps)} median={sorted(snaps)[n // 2]} max={max(snaps)}")

    # TWAP 快照缺失率（穿越时引擎会否决）
    missing = sum(
        1 for e in events for s in e["snapshots"]
        if not s.get("twap_price")
    )
    total_snaps = sum(len(e["snapshots"]) for e in events)
    print(f"  TWAP 快照缺失: {missing}/{total_snaps} ({missing / total_snaps * 100:.2f}%)")
    print()


def basis_analysis(events):
    print("=" * 58)
    print("  2. 基差量化：TWAP vs Binance")
    print("=" * 58)

    # ── 胜负分歧率 ──
    disagree = [
        e for e in events
        if e.get("twap_open_price") and e.get("twap_close_price")
        and "binance_outcome" in e
        and e["outcome"] != e["binance_outcome"]
    ]
    n_both = sum(
        1 for e in events
        if e.get("twap_open_price") and e.get("twap_close_price")
        and "binance_outcome" in e
    )
    print(f"  两口径 outcome 分歧: {len(disagree)}/{n_both} ({len(disagree) / n_both * 100:.2f}%)")

    # 按 Binance 振幅分桶看分歧分布（振幅小的窗口 = B2 信号区）
    if n_both:
        both = [e for e in events
                if e.get("twap_open_price") and e.get("twap_close_price")
                and "binance_outcome" in e]
        both.sort(key=lambda e: abs(e["close_price"] - e["open_price"]))
        amp = [abs(e["close_price"] - e["open_price"]) for e in both]
        qs = [amp[len(amp) * k // 4] for k in (1, 2, 3)]
        print(f"  Binance 振幅四分位: {qs[0]:.1f} / {qs[1]:.1f} / {qs[2]:.1f}")
        for lo, hi, label in [(0, qs[0], "Q1 最小振幅"),
                              (qs[0], qs[1], "Q2"),
                              (qs[1], qs[2], "Q3"),
                              (qs[2], 1e9, "Q4 最大振幅")]:
            bucket = [e for e in both if lo <= abs(e["close_price"] - e["open_price"]) < hi]
            if not bucket:
                continue
            d = sum(1 for e in bucket if e["outcome"] != e["binance_outcome"])
            print(f"    {label:12s} (n={len(bucket):>3d}): 分歧 {d} ({d / len(bucket) * 100:>5.1f}%)")

        # 分歧窗口示例
        for e in disagree[:3]:
            print(f"    例: binance {'UP' if e['binance_outcome'] == 0 else 'DOWN'} "
                  f"(o={e['open_price']:.0f} c={e['close_price']:.0f}) vs "
                  f"twap {'UP' if e['outcome'] == 0 else 'DOWN'} "
                  f"(o={e['twap_open_price']:.0f} c={e['twap_close_price']:.0f})")

    # ── 历史振幅尺度比 ──
    print()
    compute_hist_avg_range(events, 18, "twap")
    twap_hist = {e["start_time"]: e.get("hist_avg_range") for e in events}
    compute_hist_avg_range(events, 18, "binance")
    ratios = []
    for e in events:
        th = twap_hist.get(e["start_time"])
        bh = e.get("hist_avg_range")
        if th and bh:
            ratios.append(th / bh)
    if ratios:
        ratios.sort()
        print(f"  hist_avg_range(TWAP)/hist_avg_range(Binance): "
              f"median={ratios[len(ratios) // 2]:.3f}, "
              f"P10={ratios[len(ratios) // 10]:.3f}, "
              f"P90={ratios[len(ratios) * 9 // 10]:.3f}  (n={len(ratios)})")
        print("  → TWAP 振幅 < Binance 振幅 = 同一物理波动在 TWAP 单位下 range_expansion 更大")
        print("  → B2/F0 阈值需要按该比例放大/平移（Phase 3 重标定核心）")
    print()


def backtest_compare(events):
    print("=" * 58)
    print("  3. 回测对照（Formula B 现参数, 4 组合）")
    print("=" * 58)
    combos = [
        ("TWAP特征 + TWAP结算   (生产口径)", "twap", "twap"),
        ("TWAP特征 + Binance结算", "twap", "binance"),
        ("Binance特征 + TWAP结算 (错口径=实盘现状)", "binance", "twap"),
        ("Binance特征 + Binance结算 (旧标定基准)", "binance", "binance"),
    ]
    for label, ps, os_ in combos:
        print(f"\n── {label} ──")
        cfg = FlipBacktestConfig()
        cfg.price_source = ps
        cfg.outcome_source = os_
        signals, total, active = run_backtest(events, cfg)
        print_summary(signals, total, active, events)


def main():
    parser = argparse.ArgumentParser(description="TWAP 口径回测与基差分析")
    parser.add_argument("--data", default="../data/btc/", help="JSONL 数据目录")
    args = parser.parse_args()

    print(f"加载数据: {args.data}")
    events = load_events_from_dir(args.data)
    print(f"  → {len(events)} 个事件")
    if not events:
        print("无事件数据，退出。")
        return

    data_quality(events)
    basis_analysis(events)
    backtest_compare(events)


if __name__ == "__main__":
    main()
