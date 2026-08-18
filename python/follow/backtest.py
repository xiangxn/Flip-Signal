#!/usr/bin/env python3
"""
Follow 策略回测（独立模块）—— TWAP 结算下跟随首个 0.7 穿越侧。

背景（2026-08-15 重推导, 见 docs/twap_flip_rederivation_2026-08-15.md）:
  TWAP-60 是滞后均线 —— 现货推动的 0.7 穿越在 ~83%（事件级）的窗口被
  平滑均线确认；fade（Formula B 方向）基线 EV -0.012/股已死。
  本回测验证镜像策略: 事件内首个上升沿穿越 0.7 → 确认 T+2 ticks →
  买入穿越侧（follow），成交 = 穿越侧 ask = 1 - 对侧 bid（双 token 互补
  口径，与实盘 FAK 最优卖价校验一致）。

铁律（无未来数据）:
  * 入场只用 ≤ 确认 tick 的信息
  * 结算只用 outcome / market_outcome 字段（真实结算口径），
    禁止使用 close_price / twap_close_price 等窗口结束数据
  * 每事件最多一注，仅事件内时间顺序上首个穿越（后续穿越是反抽）

用法:
    python backtest.py --data ../data_0/lab_resolved --outcome-field market_outcome
    python backtest.py --data ../data/btc
"""

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


# ═══════════════════════════════════════════════════════════════
# 配置与结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class FollowConfig:
    """Follow 策略参数。"""
    trigger_threshold: float = 0.7   # 穿越阈值（与引擎同口径）
    min_pre_snaps: int = 5           # 穿越前最少 snapshot 数
    max_remaining_sec: int = 260     # 有效窗口上限
    min_remaining_sec: int = 15      # 有效窗口下限（确认 2 ticks + FAK 缓冲）
    confirm_delay_ticks: int = 2     # 确认等待 tick 数（10s）
    max_follow_price: float = 1.0    # follow 成交价 gate（> 此值不入场）
    side: str = "both"               # both / yes / no
    first_only: bool = True          # 仅事件内时间顺序上首个穿越


@dataclass
class FollowSignal:
    """单笔 follow 交易。"""
    event_start: int
    side: str            # 穿越侧（买入方向）
    remaining_sec: int   # 穿越时刻剩余秒数
    trigger_bid: float   # 穿越时刻穿越侧 bid
    fill: float          # 确认时刻穿越侧 ask = 1 - 对侧 bid
    won: bool            # 结算是否命中穿越侧
    pnl: float           # 每股盈亏 (1-fill) 或 -fill
    other_delta: float | None  # 确认期对侧 bid 变化（过滤特征）
    path_eff: float | None     # 穿越前现货路径效率（过滤特征）


def load_events(data_dir: str) -> list[dict]:
    """加载 JSONL 事件，按 start_time 排序（自包含，不依赖其他模块）。"""
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


# ═══════════════════════════════════════════════════════════════
# 信号检测（纯函数）
# ═══════════════════════════════════════════════════════════════

def detect_signals(events: list[dict], cfg: FollowConfig,
                   outcome_field: str) -> list[FollowSignal]:
    """按时间顺序扫描每个事件，取事件内首个上升沿穿越并记录 follow 交易。

    两侧在同一 snapshot 上不会同时 >0.7（bid 互补），无平局问题。
    首个穿越出现后事件即消耗（first_only 语义），无论 gate 是否通过。
    """
    signals: list[FollowSignal] = []
    for event in events:
        outcome = event.get(outcome_field)
        if outcome is None:
            continue
        snaps = event["snapshots"]
        sides = [cfg.side] if cfg.side in ("yes", "no") else ["yes", "no"]
        state = {"yes": False, "no": False}
        consumed = False

        for i, s in enumerate(snaps):
            if consumed:
                break
            if i < cfg.min_pre_snaps:
                continue
            if not (s["remaining_sec"] < cfg.max_remaining_sec
                    and s["remaining_sec"] > cfg.min_remaining_sec):
                continue
            for side in sides:
                this_key = "yes_price" if side == "yes" else "no_price"
                other_key = "no_price" if side == "yes" else "yes_price"
                is_above = s[this_key] > cfg.trigger_threshold
                if is_above and not state[side]:
                    # ── 上升沿穿越 ──
                    if cfg.first_only:
                        consumed = True  # 首个穿越即消耗事件（无论 gate）
                    conf_idx = i + cfg.confirm_delay_ticks
                    if conf_idx < len(snaps):
                        conf = snaps[conf_idx]
                        fill = 1.0 - conf[other_key]  # 穿越侧 ask = 1 - 对侧 bid
                        # 过滤特征（全部 ≤ 决策时刻已知）
                        other_delta = conf[other_key] - s[other_key]
                        spot = s.get("price") or 0
                        open_price = s.get("open") or event.get("open_price") or 0
                        prices = [x.get("price") or 0 for x in snaps[:i + 1]]
                        net = abs(spot - open_price) if open_price else 0
                        amp = max(prices) - min(prices) if prices else 0
                        path_eff = net / amp if amp > 1e-9 else None
                        if fill <= cfg.max_follow_price:
                            won = (outcome == 0) if side == "yes" else (outcome == 1)
                            signals.append(FollowSignal(
                                event_start=event["start_time"],
                                side=side,
                                remaining_sec=s["remaining_sec"],
                                trigger_bid=s[this_key],
                                fill=fill,
                                won=won,
                                pnl=(1.0 - fill) if won else -fill,
                                other_delta=other_delta,
                                path_eff=path_eff,
                            ))
                    if cfg.first_only:
                        break
                state[side] = is_above
    return signals


def detect_wait(events: list[dict], cfg: FollowConfig,
                outcome_field: str, wait_rem: int) -> list[FollowSignal]:
    """实时可知的 wait 变体: 窗口内「截止当前仅一侧穿越过 0.7」，
    在剩余秒数 ≤ wait_rem 时跟进该侧（决策时刻特征全部可知，无未来数据）。

    成交 = 1 - 对侧 bid（决策 tick）。该变体把"整窗单侧穿越"的类别信息
    退化为决策时刻可知的版本 —— 代价是市场已 reprice（fill 更高）。
    """
    signals: list[FollowSignal] = []
    for event in events:
        outcome = event.get(outcome_field)
        if outcome is None:
            continue
        snaps = event["snapshots"]
        crossed = set()
        state = {"yes": False, "no": False}
        for i, s in enumerate(snaps):
            if i < cfg.min_pre_snaps:
                continue
            for side in ("yes", "no"):
                this_key = "yes_price" if side == "yes" else "no_price"
                is_above = s[this_key] > cfg.trigger_threshold
                if is_above and not state[side]:
                    crossed.add(side)
                state[side] = is_above
            if s["remaining_sec"] <= wait_rem and len(crossed) == 1:
                side = list(crossed)[0]
                other_key = "no_price" if side == "yes" else "yes_price"
                fill = 1.0 - s[other_key]
                if fill > cfg.max_follow_price:
                    break  # 单侧确认但成交价超 gate，放弃本事件
                won = (outcome == 0) if side == "yes" else (outcome == 1)
                signals.append(FollowSignal(
                    event_start=event["start_time"],
                    side=side,
                    remaining_sec=s["remaining_sec"],
                    trigger_bid=s["yes_price" if side == "yes" else "no_price"],
                    fill=fill,
                    won=won,
                    pnl=(1.0 - fill) if won else -fill,
                ))
                break
    return signals


def class_bound_report(events: list[dict], outcome_field: str) -> None:
    """⚠️ 未来信息上界（不可交易，仅说明结构）:
    按整窗穿越集合分类 {only_yes / only_no / both}，报告各类别中
    follow 首个穿越的表现。类别标签用了整窗信息 —— 决策时刻未知，
    仅供理解 EV 结构的上界：若未来能实时预判类别，收益可达该水平。"""
    from collections import Counter
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
                other_key = "no_price" if side == "yes" else "yes_price"
                is_above = s[this_key] > 0.7
                if is_above and not state[side]:
                    crossed.add(side)
                    if first is None:
                        conf = snaps[i + 2] if i + 2 < len(snaps) else None
                        first = (side, conf[other_key] if conf else None)
                state[side] = is_above
        cls = ("only_yes" if crossed == {"yes"} else
               "only_no" if crossed == {"no"} else
               "both" if len(crossed) == 2 else "none")
        if first is not None and first[1] is not None:
            side, other_bid_conf = first
            won = (outcome == 0) if side == "yes" else (outcome == 1)
            stats.setdefault(cls, []).append((1.0 - other_bid_conf, won))

    print("\n  ── ⚠️ 未来信息上界（整窗穿越类别 × follow 首个穿越, 不可交易）──")
    print(f"  {'类别':<10s} {'n':>5s} {'胜率':>7s} {'均价':>7s} {'EV/股':>8s}")
    for cls in ("only_yes", "only_no", "both"):
        xs = stats.get(cls, [])
        if not xs:
            continue
        wr = sum(1 for _, w in xs if w) / len(xs)
        mf = sum(f for f, _ in xs) / len(xs)
        print(f"  {cls:<10s} {len(xs):>5d} {wr * 100:>6.1f}% {mf:>7.3f} {wr - mf:>+8.4f}")
    print("  → 若决策时刻能预判 only/both 类别: 跟进 only 类 EV ≈ +0.19/股；")
    print("    both 类首个穿越 follow 胜率仅 ~20%（≈fade 的温床）。")
    print("    可交易版本 = 用实时特征在决策时刻预判类别（下一步: 特征筛选）。")


# ═══════════════════════════════════════════════════════════════
# 候选过滤（2026-08-16 特征筛选结论, 两数据集交叉验证）
# ═══════════════════════════════════════════════════════════════

FILTERS = {
    "od0_pe4": ("od≤0 & path_eff≤0.4",
                lambda s: s.other_delta is not None and s.other_delta <= 0
                and s.path_eff is not None and s.path_eff <= 0.4),
    "od0_pe4_fill85": ("od≤0 & path_eff≤0.4 & fill≤0.85（保盈亏比）",
                       lambda s: s.other_delta is not None and s.other_delta <= 0
                       and s.path_eff is not None and s.path_eff <= 0.4
                       and s.fill <= 0.85),
    "od0_rem120": ("od≤0 & rem≤120（盈亏比差, 仅对照）",
                   lambda s: s.other_delta is not None and s.other_delta <= 0
                   and s.remaining_sec <= 120),
    "fill85_pe4": ("fill>0.85 & path_eff≤0.4",
                   lambda s: s.fill > 0.85
                   and s.path_eff is not None and s.path_eff <= 0.4),
}


# ═══════════════════════════════════════════════════════════════
# 报告
# ═══════════════════════════════════════════════════════════════

def _ci(rate: float, n: int) -> str:
    if n < 5:
        return "—"
    se = (rate * (1 - rate) / n) ** 0.5
    return f"±{1.96 * se * 100:.1f}pp"


def summarize(signals: list[FollowSignal], title: str = "") -> None:
    """汇总: n / 胜率 / 均价 / EV / P&L / PF / 均盈均亏（盈亏比）。"""
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
    n_win = sum(1 for s in signals if s.pnl > 0)
    n_loss = sum(1 for s in signals if s.pnl < 0)
    avg_win = gross_win / n_win if n_win else 0.0
    avg_loss = gross_loss / n_loss if n_loss else 0.0
    head = f"  {title + ' ' if title else ''}"
    print(f"{head}n={n:>4d}  胜率={wr * 100:>5.1f}% ({_ci(wr, n):>10s})  "
          f"均价={mf:.3f}  EV={ev:+.4f}/股  P&L={sum(s.pnl for s in signals):+.2f}  "
          f"PF={pf:.2f}")
    print(f"{'':>{len(head)}}  均盈 {avg_win:+.3f} / 均亏 {avg_loss:+.3f}  "
          f"盈亏比 1:{avg_loss / avg_win:.1f}（每赢 1 亏 {avg_loss / avg_win:.1f}）")


def per_side(signals: list[FollowSignal]) -> None:
    print("\n  ── 分侧 ──")
    for side in ("yes", "no"):
        summarize([s for s in signals if s.side == side], f"{side.upper()}:")


def per_day(signals: list[FollowSignal]) -> None:
    days = sorted({datetime.fromtimestamp(s.event_start, tz=timezone.utc).date()
                   for s in signals})
    print("\n  ── 按天稳定性 ──")
    print(f"  {'日期':<12s} {'n':>4s} {'胜率':>7s} {'95%CI':>12s} "
          f"{'均价':>7s} {'EV/股':>8s} {'日P&L':>8s}")
    for d in days:
        xs = [s for s in signals
              if datetime.fromtimestamp(s.event_start, tz=timezone.utc).date() == d]
        n = len(xs)
        wr = sum(1 for s in xs if s.won) / n
        mf = sum(s.fill for s in xs) / n
        ev = sum(s.pnl for s in xs) / n
        pnl = sum(s.pnl for s in xs)
        print(f"  {str(d):<12s} {n:>4d} {wr * 100:>6.1f}% {_ci(wr, n):>12s} "
              f"{mf:>7.3f} {ev:>+8.4f} {pnl:>+8.2f}")


def fill_buckets(signals: list[FollowSignal]) -> None:
    print("\n  ── 成交价分桶 ──")
    for lo, hi, label in [(0.0, 0.75, "≤0.75"), (0.75, 0.80, "0.75-0.80"),
                          (0.80, 0.85, "0.80-0.85"), (0.85, 1.01, ">0.85")]:
        xs = [s for s in signals if lo <= s.fill < hi]
        if xs:
            summarize(xs, f"fill {label}:")


def variants(events: list[dict], outcome_field: str) -> None:
    """参数矩阵: side × max_follow_price，快速看 gate 与方向过滤的敏感性。"""
    print("\n  ── 变体矩阵（side × max_follow_price, 格式: EV/胜率(n)）──")
    print(f"  {'side':<6s} " + "".join(f"{f'{g:.2f}':>14s}" for g in (0.75, 0.80, 0.85, 0.90, 1.00)))
    for side in ("both", "yes", "no"):
        row = f"  {side:<6s}"
        for gate in (0.75, 0.80, 0.85, 0.90, 1.00):
            cfg = FollowConfig(side=side, max_follow_price=gate)
            xs = detect_signals(events, cfg, outcome_field)
            if not xs:
                row += f" {'—':>14s}"
                continue
            wr = sum(1 for s in xs if s.won) / len(xs)
            ev = sum(s.pnl for s in xs) / len(xs)
            row += f" {ev:>+8.3f}/{wr * 100:>3.0f}%({len(xs)})"
        print(row)


def main():
    parser = argparse.ArgumentParser(description="Follow 策略回测（独立）")
    parser.add_argument("--data", default="../data_0/lab_resolved/", help="JSONL 数据目录")
    parser.add_argument("--outcome-field", default="market_outcome",
                        help="结算字段: market_outcome(data_0) / outcome(data/btc)")
    parser.add_argument("--mode", default="first", choices=("first", "wait", "both"),
                        help="first=首个穿越跟进; wait=仅一侧穿越确认后跟进(实时可知); both=都跑")
    parser.add_argument("--wait-rem", type=int, default=60,
                        help="wait 模式: 剩余秒数 ≤ 此值时下单（默认 60）")
    parser.add_argument("--side", default="both", choices=("both", "yes", "no"))
    parser.add_argument("--max-follow-price", type=float, default=1.0,
                        help="follow 成交价 gate（默认 1.0 = 不设限）")
    parser.add_argument("--filter", default="all",
                        choices=("none", "all", *FILTERS.keys()),
                        help="候选过滤: none/all/od0_pe4/od0_rem120/fill85_pe4")
    args = parser.parse_args()

    events = load_events(args.data)
    if not events:
        print("无事件数据。")
        sys.exit(1)
    t0, t1 = min(e["start_time"] for e in events), max(e["start_time"] for e in events)
    days = (t1 - t0) / 86400
    print(f"数据: {args.data}  |  事件 {len(events)}  |  跨度 {days:.1f} 天  |  "
          f"结算字段 {args.outcome_field}")

    cfg = FollowConfig(side=args.side, max_follow_price=args.max_follow_price)

    def report(signals, title):
        print()
        print("=" * 62)
        print(f"  {title}")
        print("=" * 62)
        summarize(signals)
        per_side(signals)
        per_day(signals)
        fill_buckets(signals)

    if args.mode in ("first", "both"):
        signals = detect_signals(events, cfg, args.outcome_field)
        report(signals, "Follow 模式 A: 跟随事件内首个 0.7 穿越侧（时间顺序）")
        # 候选过滤
        if args.filter != "none":
            keys = FILTERS.keys() if args.filter == "all" else [args.filter]
            print("\n  ── 候选过滤（首个穿越子集）──")
            for key in keys:
                label, pred = FILTERS[key]
                xs = [s for s in signals if pred(s)]
                print(f"\n  【{key}: {label}】")
                summarize(xs)
                if xs:
                    per_day(xs)
        # 对照: 全部穿越（仅参考，实盘口径为首个穿越）
        cfg_all = FollowConfig(side=args.side, max_follow_price=args.max_follow_price,
                               first_only=False)
        all_sigs = detect_signals(events, cfg_all, args.outcome_field)
        print("\n  ── 对照 ──")
        summarize(all_sigs, "全部穿越（参考）:")

    if args.mode in ("wait", "both"):
        wait_sigs = detect_wait(events, cfg, args.outcome_field, args.wait_rem)
        report(wait_sigs, f"Follow 模式 B: 单侧穿越确认后跟进（rem ≤ {args.wait_rem}s，实时可知）")

    variants(events, args.outcome_field)
    class_bound_report(events, args.outcome_field)
    print()


if __name__ == "__main__":
    main()
