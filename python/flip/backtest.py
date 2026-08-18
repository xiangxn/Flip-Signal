#!/usr/bin/env python3
"""
翻转（Flip）策略回测（独立模块）—— TWAP 结算下预判"震荡市"并买对侧。

与 follow 线完全独立（各自目录/信号/评估，P&L 分开）:
  * 翻转线只做 flip: 买穿越侧的对侧，赢 = 穿越侧最终输掉
  * follow 线只做 follow（见 python/follow/）

背景（2026-08-16 重推导, docs/twap_flip_rederivation_2026-08-15.md）:
  整窗只一侧穿越 0.7 → 该侧 ~97%+ 赢（follow 的领域）；
  两侧都穿越（both）→ 首个穿越侧只赢 ~20%（flip 的领域, EV 上界 +0.51/股）。
  类别标签是未来信息，可交易版本 = 决策时刻用实时特征预判类别。

本脚本三个口径:
  模式 A: 事件内时间顺序首个穿越 → flip（无过滤基线, 预期 EV≈0 附近）
  模式 B: 两侧都穿越过 0.7 后，flip 最新穿越侧（实时可知的弱化版;
          市场见过双向穿越已 reprice，预期边际很薄）
  类别上界: 整窗类别 × flip 首个穿越（⚠️ 未来信息，不可交易，仅看结构）

铁律（无未来数据）:
  * 入场只用 ≤ 决策 tick 的信息
  * 结算只用 outcome / market_outcome 字段（真实结算口径）
  * 每事件最多一注

用法:
    python backtest.py --data ../data_0/lab_resolved --outcome-field market_outcome
    python backtest.py --data ../data/btc --outcome-field outcome
"""

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class FlipConfig:
    """Flip 策略参数（独立于 follow 线）。"""
    trigger_threshold: float = 0.7
    min_pre_snaps: int = 5
    max_remaining_sec: int = 260
    min_remaining_sec: int = 15
    confirm_delay_ticks: int = 2
    max_flip_price: float = 1.0     # flip 成交价 gate（> 此值不入场）
    side: str = "both"              # both / yes / no（穿越侧过滤）


@dataclass
class FlipSignal:
    """单笔 flip 交易。"""
    event_start: int
    side: str            # 被 flip 的穿越侧（买其对面）
    remaining_sec: int
    trigger_bid: float   # 穿越侧 bid（决策时刻）
    fill: float          # 对侧 ask = 1 - 触发侧 bid（确认时刻）
    won: bool
    pnl: float


def load_events(data_dir: str) -> list[dict]:
    events = []
    corrections = {}
    for path in sorted(Path(data_dir).glob("events_*.jsonl")):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("event_type") == "settlement_correction":
                    corrections[rec["start_time"]] = rec
                else:
                    events.append(rec)
    # 结算修正行按 start_time 覆盖合并（官方 open/close/outcome 延迟到达后追加，
    # 见 internal/collect/settle.go；文件内行序与事件序无关，按 start_time 对齐）
    if corrections:
        for e in events:
            corr = corrections.get(e["start_time"])
            if corr:
                e.update({k: v for k, v in corr.items() if k != "event_type"})
    events.sort(key=lambda e: e["start_time"])
    return events


def _flip_fill(trigger_bid_conf: float) -> float:
    """对侧 ask = 1 - 触发侧 bid（双 token 互补，实盘 FAK 口径）。"""
    return 1.0 - trigger_bid_conf


def detect_first(events: list[dict], cfg: FlipConfig,
                 outcome_field: str) -> list[FlipSignal]:
    """模式 A: 时间顺序首个穿越 → 确认后 flip（买对侧）。"""
    signals: list[FlipSignal] = []
    for event in events:
        outcome = event.get(outcome_field)
        if outcome is None:
            continue
        snaps = event["snapshots"]
        sides = [cfg.side] if cfg.side in ("yes", "no") else ["yes", "no"]
        state = {"yes": False, "no": False}
        for i, s in enumerate(snaps):
            if i < cfg.min_pre_snaps:
                continue
            if not (s["remaining_sec"] < cfg.max_remaining_sec
                    and s["remaining_sec"] > cfg.min_remaining_sec):
                continue
            for side in sides:
                this_key = "yes_price" if side == "yes" else "no_price"
                is_above = s[this_key] > cfg.trigger_threshold
                if is_above and not state[side]:
                    conf_idx = i + cfg.confirm_delay_ticks
                    if conf_idx < len(snaps):
                        fill = _flip_fill(snaps[conf_idx][this_key])
                        if fill <= cfg.max_flip_price:
                            won = (outcome == 1) if side == "yes" else (outcome == 0)
                            signals.append(FlipSignal(
                                event_start=event["start_time"], side=side,
                                remaining_sec=s["remaining_sec"],
                                trigger_bid=s[this_key], fill=fill,
                                won=won, pnl=(1.0 - fill) if won else -fill))
                    state[side] = True
                    break  # 首个穿越消耗事件
            else:
                continue  # 本 snapshot 无上升沿
            break
    return signals


def detect_both(events: list[dict], cfg: FlipConfig,
                outcome_field: str) -> list[FlipSignal]:
    """模式 B: 两侧都穿越过 0.7 后，flip 最新穿越侧（实时可知）。

    第二侧穿越 = both 已实现（决策时刻已知双向穿越），在其确认 tick
    flip 该侧。市场见过双向穿越后已 reprice，此处验证残余边际。
    """
    signals: list[FlipSignal] = []
    for event in events:
        outcome = event.get(outcome_field)
        if outcome is None:
            continue
        snaps = event["snapshots"]
        sides = [cfg.side] if cfg.side in ("yes", "no") else ["yes", "no"]
        state = {"yes": False, "no": False}
        crossed = set()
        done = False
        for i, s in enumerate(snaps):
            if done:
                break
            if i < cfg.min_pre_snaps:
                continue
            if not (s["remaining_sec"] < cfg.max_remaining_sec
                    and s["remaining_sec"] > cfg.min_remaining_sec):
                continue
            for side in sides:
                this_key = "yes_price" if side == "yes" else "no_price"
                is_above = s[this_key] > cfg.trigger_threshold
                if is_above and not state[side]:
                    crossed.add(side)
                    if len(crossed) == 2:
                        # both 已实现 → flip 最新穿越侧（= 当前 side）
                        done = True
                        conf_idx = i + cfg.confirm_delay_ticks
                        if conf_idx < len(snaps):
                            fill = _flip_fill(snaps[conf_idx][this_key])
                            if fill <= cfg.max_flip_price:
                                won = (outcome == 1) if side == "yes" else (outcome == 0)
                                signals.append(FlipSignal(
                                    event_start=event["start_time"], side=side,
                                    remaining_sec=s["remaining_sec"],
                                    trigger_bid=s[this_key], fill=fill,
                                    won=won, pnl=(1.0 - fill) if won else -fill))
                        break
                state[side] = is_above
    return signals


# ═══════════════════════════════════════════════════════════════
# 报告
# ═══════════════════════════════════════════════════════════════

def _ci(rate: float, n: int) -> str:
    if n < 5:
        return "—"
    se = (rate * (1 - rate) / n) ** 0.5
    return f"±{1.96 * se * 100:.1f}pp"


def summarize(signals: list[FlipSignal], title: str = "") -> None:
    if not signals:
        print(f"  {title}: 无信号")
        return
    n = len(signals)
    wr = sum(1 for s in signals if s.won) / n
    mf = sum(s.fill for s in signals) / n
    ev = sum(s.pnl for s in signals) / n
    gross_win = sum(s.pnl for s in signals if s.pnl > 0)
    gross_loss = -sum(s.pnl for s in signals if s.pnl < 0)
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    head = f"  {title + ' ' if title else ''}"
    print(f"{head}n={n:>4d}  胜率={wr * 100:>5.1f}% ({_ci(wr, n):>10s})  "
          f"均价={mf:.3f}  EV={ev:+.4f}/股  P&L={sum(s.pnl for s in signals):+.2f}  "
          f"PF={pf:.2f}")


def per_day(signals: list[FlipSignal]) -> None:
    days = sorted({datetime.fromtimestamp(s.event_start, tz=timezone.utc).date()
                   for s in signals})
    print("\n  ── 按天稳定性 ──")
    print(f"  {'日期':<12s} {'n':>4s} {'胜率':>7s} {'95%CI':>12s} "
          f"{'均价':>7s} {'EV/股':>8s}")
    for d in days:
        xs = [s for s in signals
              if datetime.fromtimestamp(s.event_start, tz=timezone.utc).date() == d]
        n = len(xs)
        wr = sum(1 for s in xs if s.won) / n
        mf = sum(s.fill for s in xs) / n
        ev = sum(s.pnl for s in xs) / n
        print(f"  {str(d):<12s} {n:>4d} {wr * 100:>6.1f}% {_ci(wr, n):>12s} "
              f"{mf:>7.3f} {ev:>+8.4f}")


def class_bound_report(events: list[dict], outcome_field: str) -> None:
    """⚠️ 未来信息上界（不可交易）: 翻转视角的整窗类别结构。"""
    stats = {}
    for event in events:
        outcome = event.get(outcome_field)
        if outcome is None:
            continue
        snaps = event["snapshots"]
        crossed = set()
        state = {"yes": False, "no": False}
        first = None
        for i, s in enumerate(snaps):
            if i < 5:
                continue
            if not (s["remaining_sec"] < 260 and s["remaining_sec"] > 15):
                continue
            for side in ("yes", "no"):
                this_key = "yes_price" if side == "yes" else "no_price"
                is_above = s[this_key] > 0.7
                if is_above and not state[side]:
                    crossed.add(side)
                    if first is None:
                        conf = snaps[i + 2] if i + 2 < len(snaps) else None
                        first = (side, conf[this_key] if conf else None)
                state[side] = is_above
        cls = ("only_yes" if crossed == {"yes"} else
               "only_no" if crossed == {"no"} else
               "both" if len(crossed) == 2 else "none")
        if first is not None and first[1] is not None:
            side, trigger_bid_conf = first
            won = (outcome == 1) if side == "yes" else (outcome == 0)
            stats.setdefault(cls, []).append((1.0 - trigger_bid_conf, won))

    print("\n  ── ⚠️ 未来信息上界（整窗类别 × flip 首个穿越, 不可交易）──")
    print(f"  {'类别':<10s} {'n':>5s} {'胜率':>7s} {'均价':>7s} {'EV/股':>8s}")
    for cls in ("only_yes", "only_no", "both"):
        xs = stats.get(cls, [])
        if not xs:
            continue
        wr = sum(1 for _, w in xs if w) / len(xs)
        mf = sum(f for f, _ in xs) / len(xs)
        print(f"  {cls:<10s} {len(xs):>5d} {wr * 100:>6.1f}% {mf:>7.3f} {wr - mf:>+8.4f}")
    print("  → flip 的领域是 both 类（EV 上界 +0.51/股）；only 类 flip 必亏。")
    print("    可交易版本 = 决策时刻用实时特征预判 both 类（下一步: analyze.py）。")


def main():
    parser = argparse.ArgumentParser(description="Fade 策略回测（独立）")
    parser.add_argument("--data", default="../data_0/lab_resolved/", help="JSONL 数据目录")
    parser.add_argument("--outcome-field", default="market_outcome",
                        help="结算字段: market_outcome(data_0) / outcome(data/btc)")
    parser.add_argument("--mode", default="both", choices=("first", "both", "all"),
                        help="first=首个穿越 flip; both=双向穿越后 flip 最新侧; all=都跑")
    parser.add_argument("--side", default="both", choices=("both", "yes", "no"),
                        help="穿越侧过滤")
    parser.add_argument("--max-flip-price", type=float, default=1.0,
                        help="flip 成交价 gate（默认 1.0 = 不设限）")
    args = parser.parse_args()

    events = load_events(args.data)
    if not events:
        print("无事件数据。")
        sys.exit(1)
    t0, t1 = min(e["start_time"] for e in events), max(e["start_time"] for e in events)
    print(f"数据: {args.data}  |  事件 {len(events)}  |  跨度 {(t1 - t0) / 86400:.1f} 天  |  "
          f"结算字段 {args.outcome_field}")

    cfg = FlipConfig(side=args.side, max_flip_price=args.max_flip_price)

    if args.mode in ("first", "all"):
        sigs = detect_first(events, cfg, args.outcome_field)
        print()
        print("=" * 62)
        print("  Flip 模式 A: flip 事件内首个 0.7 穿越侧（无过滤基线）")
        print("=" * 62)
        summarize(sigs)
        if sigs:
            per_day(sigs)

    if args.mode in ("both", "all"):
        sigs = detect_both(events, cfg, args.outcome_field)
        print()
        print("=" * 62)
        print("  Flip 模式 B: 两侧都穿越后 flip 最新穿越侧（实时可知）")
        print("=" * 62)
        summarize(sigs)
        if sigs:
            per_day(sigs)

    class_bound_report(events, args.outcome_field)
    print()


if __name__ == "__main__":
    main()
