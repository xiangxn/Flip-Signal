#!/usr/bin/env python3
"""
分析 confirm_delay_ticks 对信号数的影响。

直接复用现有回测框架 (backtest_flip_utils.py)，
分别在 [1..5] 下运行完整回测，输出对比结果。

同时诊断:
  1. 不同 delay 下的信号数、胜率、P&L
  2. other_delta 随 delay 的分布变化
  3. 窗口末尾穿越的丢失情况 (Python vs Go 的差异)
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_flip_config import FlipBacktestConfig, DEFAULT_CONFIG
from backtest_flip_utils import (
    load_events_from_dir,
    run_backtest,
    print_summary,
    compute_other_delta,
    _score_crossing,
)

# ── 数据目录 ──
DATA_DIR = str(Path(__file__).resolve().parent.parent / "data_0" / "lab")


def analyze_confirm_delay():
    print("=" * 80)
    print("  confirm_delay_ticks 影响分析")
    print("=" * 80)

    events = load_events_from_dir(DATA_DIR)
    print(f"\n数据: {len(events)} 事件")

    # ── 1. 各 delay 值回测对比 ──
    print("\n" + "-" * 80)
    print("  各 confirm_delay_ticks 下的完整回测对比")
    print("-" * 80)
    print(f"  {'delay':>6} {'signals':>8} {'wins':>6} {'wr%':>7} {'P&L':>8} {'avg_entry':>10} {'avg_score':>10} {'daily':>6}")
    print("  " + "-" * 76)

    best_pnl = -999
    best_delay = 1
    for delay in [1, 2, 3, 4, 5]:
        cfg = FlipBacktestConfig()  # copy defaults
        cfg.confirm_delay_ticks = delay
        signals, total, active = run_backtest(events, cfg)

        n = len(signals)
        wins = sum(1 for s in signals if s.won)
        wr = wins / n * 100 if n > 0 else 0
        total_pnl = sum(s.pnl for s in signals)
        avg_entry = sum(s.entry_price for s in signals) / n if n > 0 else 0
        avg_score = sum(s.score for s in signals) / n if n > 0 else 0

        if signals:
            t_min = min(s.event_time for s in signals)
            t_max = max(s.event_time for s in signals)
            days = max((t_max - t_min) / 86400, 1)
            daily = n / days
        else:
            daily = 0

        marker = ""
        if total_pnl > best_pnl:
            best_pnl = total_pnl
            best_delay = delay

        print(f"  {delay:>6} {n:>8} {wins:>6} {wr:>6.1f}% {total_pnl:>+8.2f} {avg_entry:>10.3f} {avg_score:>10.1f} {daily:>5.0f}")

    print(f"\n  最高 P&L: delay={best_delay} (+{best_pnl:.2f})")

    # ── 2. delay=1 vs delay=5 丢失信号详情 ──
    print("\n" + "-" * 80)
    print("  delay=1 有信号但 delay=5 丢失的事件 — 丢失原因分析")
    print("-" * 80)

    cfg1 = FlipBacktestConfig()
    cfg1.confirm_delay_ticks = 1
    signals1, _, _ = run_backtest(events, cfg1)
    sig1_ids = {(s.event_time, s.side) for s in signals1}

    cfg5 = FlipBacktestConfig()
    cfg5.confirm_delay_ticks = 5
    signals5, _, _ = run_backtest(events, cfg5)
    sig5_ids = {(s.event_time, s.side) for s in signals5}

    lost_ids = sig1_ids - sig5_ids

    print(f"  delay=1 信号数: {len(signals1)}")
    print(f"  delay=5 信号数: {len(signals5)}")
    print(f"  丢失: {len(lost_ids)}")

    # 分析丢失原因
    lost_reasons = {"od_regressed": 0, "od_insufficient": 0, "window_end_go": 0, "other": 0}
    lost_details = []

    for event in events:
        if event.get("hist_avg_range") is None:
            continue
        for side in ("yes", "no"):
            if (event["start_time"], side) not in lost_ids:
                continue

            # 找到这个穿越点
            this_key = "yes_price" if side == "yes" else "no_price"
            snaps = event["snapshots"]
            cross_idx = None
            was_above = False
            for i, s in enumerate(snaps):
                is_above = (s[this_key] > cfg1.trigger_threshold
                           and s["remaining_sec"] < cfg1.max_remaining_sec)
                if is_above and not was_above and i >= cfg1.min_pre_snaps:
                    # check if this crossing produced signal at delay=1
                    sig1 = _score_crossing(event, side, i, cfg1)
                    if sig1 is not None:
                        cross_idx = i
                        break
                was_above = is_above

            if cross_idx is None:
                continue

            # 检查 delay=5 时的情况
            other_key = "no_price" if side == "yes" else "yes_price"
            od1 = compute_other_delta(snaps, cross_idx, other_key, cfg1)
            od5 = compute_other_delta(snaps, cross_idx, other_key, cfg5)

            sig5 = _score_crossing(event, side, cross_idx, cfg5)

            # 判断原因
            conf_idx_needed = cross_idx + 5
            if conf_idx_needed >= len(snaps):
                reason = "window_end_go"
            elif sig5 is None:
                # 看具体是因为什么被过滤
                if od5 <= od1:
                    reason = f"od_regressed"
                else:
                    reason = f"od_insufficient"
            else:
                reason = f"other"

            if reason in lost_reasons:
                lost_reasons[reason] += 1
            else:
                lost_reasons["other"] += 1

            lost_details.append({
                "event_time": event["start_time"],
                "side": side,
                "remaining_sec": snaps[cross_idx]["remaining_sec"],
                "od1": od1,
                "od5": od5,
                "sig5_score": sig5.score if sig5 else "N/A",
                "cross_idx": cross_idx,
                "total_snaps": len(snaps),
                "reason": reason,
            })

    total_lost = sum(lost_reasons.values())
    print(f"\n  丢失原因分布:")
    for reason, count in sorted(lost_reasons.items(), key=lambda x: -x[1]):
        pct = count / total_lost * 100 if total_lost > 0 else 0
        label = {
            "od_regressed": "od 缩减 (5s比25s更有利于对面反弹)",
            "od_insufficient": "od 不够大 (得分不足5)",
            "window_end_go": "窗口末尾 (Go引擎会丢失, Python用最后snap兜底)",
            "other": "其他",
        }.get(reason, reason)
        print(f"    {label}: {count} ({pct:.1f}%)")

    # ── 3. other_delta 随 delay 的分布 ──
    print("\n" + "-" * 80)
    print("  所有穿越点的 other_delta 随 delay 分布")
    print("-" * 80)
    print(f"  {'delay':>6} {'n':>6} {'mean':>8} {'median':>8} {'p25':>8} {'p75':>8} {'>0_pct':>7} {'>0.01':>7} {'>0.02':>7} {'>0.05':>7}")
    print("  " + "-" * 76)

    # Collect all crossings
    all_crossings = []
    for event in events:
        snaps = event["snapshots"]
        yes_was, no_was = False, False
        for i, s in enumerate(snaps):
            for side, key, was in [("yes", "yes_price", yes_was), ("no", "no_price", no_was)]:
                is_above = (s[key] > 0.7 and s["remaining_sec"] < 260)
                if is_above and not was and i >= 5:
                    all_crossings.append((event, snaps, i, side))
                    break
            yes_was = (s["yes_price"] > 0.7 and s["remaining_sec"] < 260)
            no_was = (s["no_price"] > 0.7 and s["remaining_sec"] < 260)

    for delay in [1, 2, 3, 4, 5]:
        cfg_d = FlipBacktestConfig()
        cfg_d.confirm_delay_ticks = delay
        ods = []
        for event, snaps, idx, side in all_crossings:
            other_key = "no_price" if side == "yes" else "yes_price"
            od = compute_other_delta(snaps, idx, other_key, cfg_d)
            ods.append(od)

        if ods:
            ods.sort()
            n = len(ods)
            mean = sum(ods) / n
            median = ods[n // 2]
            p25 = ods[n // 4]
            p75 = ods[3 * n // 4]
            gt0 = sum(1 for o in ods if o > 0) / n * 100
            gt001 = sum(1 for o in ods if o > 0.01) / n * 100
            gt002 = sum(1 for o in ods if o > 0.02) / n * 100
            gt005 = sum(1 for o in ods if o > 0.05) / n * 100
            print(f"  {delay:>6} {n:>6} {mean:>+8.4f} {median:>+8.4f} {p25:>+8.4f} {p75:>+8.4f} {gt0:>6.1f}% {gt001:>6.1f}% {gt002:>6.1f}% {gt005:>6.1f}%")

    # ── 4. 窗口末尾穿越：Go 引擎会丢失多少 ──
    print("\n" + "-" * 80)
    print("  窗口末尾穿越: Python vs Go 引擎差异")
    print("-" * 80)
    print("  Python 回测用 min(idx+delay, len-1) — 总是用最后一个 snapshot 兜底")
    print("  Go 引擎确认期不够则永远无法进入 onConfirmed — 穿越被丢弃")
    print()

    for delay in [3, 4, 5]:
        lost = 0
        lost_with_signal = 0
        for event, snaps, idx, side in all_crossings:
            if idx + delay >= len(snaps):
                lost += 1
                # 在 Python 回测中这个穿越能产生信号吗?
                sig = _score_crossing(event, side, idx, cfg5)
                if sig is not None:
                    lost_with_signal += 1

        print(f"  delay={delay}: Go 引擎会丢失 {lost} 个窗口末尾穿越 "
              f"(其中 {lost_with_signal} 个在 Python 回测中能过评分)")

    # ── 5. 案例详情 ──
    print("\n" + "-" * 80)
    print("  delay=1 通过但 delay=5 丢失的案例 (前 15 个)")
    print("-" * 80)
    for d in sorted(lost_details, key=lambda x: x["event_time"])[:15]:
        print(f"  ts={d['event_time']} side={d['side']:>3} rem={d['remaining_sec']:>3}s "
              f"od1={d['od1']:+.3f} od5={d['od5']:+.3f} "
              f"score5={d['sig5_score']} cross@={d['cross_idx']}/{d['total_snaps']} "
              f"reason={d['reason']}")


if __name__ == "__main__":
    analyze_confirm_delay()
