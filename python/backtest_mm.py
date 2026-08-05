#!/usr/bin/env python3
"""
多策略回测 — 非对称 Spread 买侧 + 卖侧 F8。

正确 P&L 算法：
  - 两侧成交：merge/卖出利润
  - 单侧成交：持仓方与 outcome 一致则盈，否则亏损全部持仓成本
  - 都不成交：P&L=0

Usage:
    python backtest_mm.py --data ../data/
"""

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── 通用参数 ──────────────────────────────────────────────
ENTRY_SEC = 220
WARMUP_SEC = 60
TOLERANCE = 10
QTY = 5


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def asym_formula(total_spread: float, yes_price: float, no_price: float,
                 k: float) -> tuple[float, float]:
    """非对称 Spread 分配公式。返回 (high_spread, low_spread)。"""
    base = total_spread / 2
    imbalance = abs(yes_price - no_price)
    tilt = min(k * imbalance, 0.95)
    high_sp = max(base * (1 - tilt), 0.01)
    low_sp = min(base * (1 + tilt), total_spread - 0.01)
    return high_sp, low_sp


def find_snapshot(snapshots: list[dict], target_sec: int,
                  tolerance: int = TOLERANCE) -> dict | None:
    """在 snapshots 中找到最接近 target_sec 的快照。"""
    best, best_dist = None, float("inf")
    for s in snapshots:
        dist = abs(s["remaining_sec"] - target_sec)
        if dist < best_dist:
            best_dist = dist
            best = s
    return best if (best is not None and best_dist <= tolerance) else None


def compute_f8(snapshots: list[dict], entry_snap: dict) -> float:
    """计算 F8（价格稳定性）：开盘到入场时刻 yes+no 累计变动的补数。"""
    # 找到第一个有 PM 数据的快照作为开盘价
    first_yes = first_no = None
    for s in snapshots:
        if s["yes_price"] > 0 and s["no_price"] > 0:
            first_yes = s["yes_price"]
            first_no = s["no_price"]
            break
    if first_yes is None:
        return 1.0

    yes_change = abs(entry_snap["yes_price"] - first_yes)
    no_change = abs(entry_snap["no_price"] - first_no)
    return 1.0 - min(yes_change + no_change, 1.0)


def load_events(data_dir: str) -> list[dict]:
    """加载所有 JSONL 事件文件。"""
    events = []
    data_path = Path(data_dir)
    jsonl_files = sorted(data_path.glob("events_*.jsonl"))
    if not jsonl_files:
        raise FileNotFoundError(f"No events_*.jsonl files found in {data_dir}")
    for fpath in jsonl_files:
        for line in open(fpath):
            events.append(json.loads(line))
    return events


# ═══════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════

@dataclass
class TradeResult:
    event_id: str = ""
    yes_entry: float = 0.0
    no_entry: float = 0.0
    yes_filled: bool = False
    no_filled: bool = False
    pnl: float = 0.0
    outcome: int = 0


@dataclass
class StrategyResult:
    name: str = ""
    total_events: int = 0
    traded: int = 0
    skipped: int = 0
    both_fill: int = 0
    one_fill: int = 0
    none_fill: int = 0
    both_pnl_total: float = 0.0
    one_pnl_total: float = 0.0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    win_rate: float = 0.0
    best: float = 0.0
    worst: float = 0.0
    # 单侧成交分侧 P&L
    yes_only_total: int = 0
    yes_only_pnl: float = 0.0
    no_only_total: int = 0
    no_only_pnl: float = 0.0


# ═══════════════════════════════════════════════════════════
# 策略实现
# ═══════════════════════════════════════════════════════════

def simulate_buy(snapshots: list[dict], entry_snap: dict,
                 outcome: int, total_spread: float, k: float) -> TradeResult:
    """买侧策略：挂限价买单（乘法折扣），成交后 merge。

    bid = price × (1 - spread)
    P&L: 两侧成交 → merge 1 USDC/对；单侧 → 按结算价。
    """
    yes_e = entry_snap["yes_price"]
    no_e = entry_snap["no_price"]
    rem = entry_snap["remaining_sec"]

    bid_h, bid_l = asym_formula(total_spread, yes_e, no_e, k)

    if yes_e >= no_e:
        yes_bid = yes_e * (1 - bid_h)
        no_bid = no_e * (1 - bid_l)
    else:
        yes_bid = yes_e * (1 - bid_l)
        no_bid = no_e * (1 - bid_h)

    yf, nf = False, False
    for s in snapshots:
        if s["remaining_sec"] >= rem:
            continue
        if not yf and s["yes_price"] > 0 and s["yes_price"] <= yes_bid:
            yf = True
        if not nf and s["no_price"] > 0 and s["no_price"] <= no_bid:
            nf = True
        if yf and nf:
            break

    if yf and nf:
        cost = QTY * yes_bid + QTY * no_bid
        pnl = QTY - cost
    elif yf and not nf:
        cost = QTY * yes_bid
        pnl = QTY * (1.0 if outcome == 0 else 0.0) - cost
    elif not yf and nf:
        cost = QTY * no_bid
        pnl = QTY * (1.0 if outcome == 1 else 0.0) - cost
    else:
        pnl = 0.0

    return TradeResult(
        event_id="", yes_entry=yes_e, no_entry=no_e,
        yes_filled=yf, no_filled=nf, pnl=pnl, outcome=outcome,
    )


def simulate_sell(snapshots: list[dict], entry_snap: dict,
                  outcome: int, total_spread: float, k: float,
                  capital: float = 5.0) -> TradeResult:
    """卖侧策略：Split 后挂限价卖单（加法溢价）。

    ask = price + spread
    P&L: revenue - capital（split 成本）。
    """
    yes_e = entry_snap["yes_price"]
    no_e = entry_snap["no_price"]
    rem = entry_snap["remaining_sec"]

    sp_h, sp_l = asym_formula(total_spread, yes_e, no_e, k)

    if yes_e >= no_e:
        yes_ask = min(yes_e + sp_h, 0.99)
        no_ask = min(no_e + sp_l, 0.99)
    else:
        yes_ask = min(yes_e + sp_l, 0.99)
        no_ask = min(no_e + sp_h, 0.99)

    yf, nf = False, False
    for s in snapshots:
        if s["remaining_sec"] >= rem:
            continue
        if not yf and s["yes_price"] > 0 and s["yes_price"] >= yes_ask:
            yf = True
        if not nf and s["no_price"] > 0 and s["no_price"] >= no_ask:
            nf = True
        if yf and nf:
            break

    qty = int(capital / 0.5) // 2  # =5
    if yf and nf:
        revenue = qty * yes_ask + qty * no_ask
        pnl = revenue - capital
    elif yf and not nf:
        revenue = qty * yes_ask + qty * (1.0 if outcome == 1 else 0.0)
        pnl = revenue - capital
    elif not yf and nf:
        revenue = qty * (1.0 if outcome == 0 else 0.0) + qty * no_ask
        pnl = revenue - capital
    else:
        # 都不成交：未卖出 token 按结算价 → 一对一赢 → revenue=5
        revenue = qty * (1.0 if outcome == 0 else 0.0) + qty * (1.0 if outcome == 1 else 0.0)
        pnl = revenue - capital  # = 0

    return TradeResult(
        event_id="", yes_entry=yes_e, no_entry=no_e,
        yes_filled=yf, no_filled=nf, pnl=pnl, outcome=outcome,
    )


def simulate_sell_fixed(snapshots: list[dict], entry_snap: dict,
                        outcome: int, high_sp: float, low_sp: float,
                        capital: float = 5.0) -> TradeResult:
    """卖侧策略：固定非对称 spread（非公式，直接用固定值）。"""
    yes_e = entry_snap["yes_price"]
    no_e = entry_snap["no_price"]
    rem = entry_snap["remaining_sec"]

    if yes_e >= no_e:
        yes_ask = min(yes_e + high_sp, 0.99)
        no_ask = min(no_e + low_sp, 0.99)
    else:
        yes_ask = min(yes_e + low_sp, 0.99)
        no_ask = min(no_e + high_sp, 0.99)

    yf, nf = False, False
    for s in snapshots:
        if s["remaining_sec"] >= rem:
            continue
        if not yf and s["yes_price"] > 0 and s["yes_price"] >= yes_ask:
            yf = True
        if not nf and s["no_price"] > 0 and s["no_price"] >= no_ask:
            nf = True
        if yf and nf:
            break

    qty = int(capital / 0.5) // 2
    if yf and nf:
        pnl = qty * yes_ask + qty * no_ask - capital
    elif yf and not nf:
        pnl = qty * yes_ask + qty * (1.0 if outcome == 1 else 0.0) - capital
    elif not yf and nf:
        pnl = qty * (1.0 if outcome == 0 else 0.0) + qty * no_ask - capital
    else:
        pnl = 0.0

    return TradeResult(
        event_id="", yes_entry=yes_e, no_entry=no_e,
        yes_filled=yf, no_filled=nf, pnl=pnl, outcome=outcome,
    )


# ═══════════════════════════════════════════════════════════
# 回测 & 统计
# ═══════════════════════════════════════════════════════════

def make_stats(trades: list[TradeResult], name: str, total_events: int,
               skipped: int = 0) -> StrategyResult:
    """从交易列表生成统计结果。"""
    r = StrategyResult(name=name, total_events=total_events,
                       traded=len(trades), skipped=skipped)
    if not trades:
        return r

    pnls = [t.pnl for t in trades]
    r.both_fill = sum(1 for t in trades if t.yes_filled and t.no_filled)
    r.one_fill = sum(1 for t in trades
                     if (t.yes_filled or t.no_filled)
                     and not (t.yes_filled and t.no_filled))
    r.none_fill = sum(1 for t in trades
                      if not t.yes_filled and not t.no_filled)

    both_pnls = [t.pnl for t in trades if t.yes_filled and t.no_filled]
    one_pnls = [t.pnl for t in trades
                if (t.yes_filled or t.no_filled)
                and not (t.yes_filled and t.no_filled)]

    r.both_pnl_total = sum(both_pnls)
    r.one_pnl_total = sum(one_pnls)
    r.total_pnl = sum(pnls)
    r.avg_pnl = statistics.mean(pnls) if pnls else 0.0
    r.win_rate = sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0.0
    r.best = max(pnls)
    r.worst = min(pnls)

    r.yes_only_total = sum(1 for t in trades
                           if t.yes_filled and not t.no_filled)
    r.yes_only_pnl = sum(t.pnl for t in trades
                         if t.yes_filled and not t.no_filled)
    r.no_only_total = sum(1 for t in trades
                          if t.no_filled and not t.yes_filled)
    r.no_only_pnl = sum(t.pnl for t in trades
                        if t.no_filled and not t.yes_filled)

    return r


def run_variant(events: list[dict], name: str, sim_fn,
                entry_filter_fn=None) -> StrategyResult:
    """运行一个策略变体。

    sim_fn(snapshots, entry_snap, outcome) -> TradeResult
    entry_filter_fn(entry_snap, snapshots) -> bool | None
    """
    trades = []
    total = 0
    skipped = 0

    for e in events:
        snaps = e["snapshots"]
        entry = find_snapshot(snaps, ENTRY_SEC)
        if entry is None:
            continue
        if entry["yes_price"] <= 0 or entry["no_price"] <= 0:
            continue
        if entry["remaining_sec"] > 300 - WARMUP_SEC:
            continue

        total += 1

        # 入场过滤器
        if entry_filter_fn and not entry_filter_fn(entry, snaps):
            skipped += 1
            continue

        tr = sim_fn(snaps, entry, e["outcome"])
        tr.event_id = e.get("condition_id", "")[:12]
        trades.append(tr)

    return make_stats(trades, name, total, skipped)


# ═══════════════════════════════════════════════════════════
# 报告
# ═══════════════════════════════════════════════════════════

def print_result(r: StrategyResult) -> None:
    """打印单个策略结果。"""
    print(f"\n  {'─' * 60}")
    print(f"  {r.name}")
    print(f"  {'─' * 60}")
    print(f"    Event: {r.total_events} | 交易: {r.traded} | 跳过: {r.skipped}")
    print(f"    两侧: {r.both_fill} ({r.both_fill/max(r.traded,1):.1%}) "
          f"| 均=${r.both_pnl_total/max(r.both_fill,1):+.3f} "
          f"| 合计=${r.both_pnl_total:+.2f}")
    print(f"    单侧: {r.one_fill} ({r.one_fill/max(r.traded,1):.1%}) "
          f"| 均=${r.one_pnl_total/max(r.one_fill,1):+.3f} "
          f"| 合计=${r.one_pnl_total:+.2f}")
    if r.one_fill > 0:
        y_avg = r.yes_only_pnl / max(r.yes_only_total, 1)
        n_avg = r.no_only_pnl / max(r.no_only_total, 1)
        print(f"    仅YES成交: {r.yes_only_total}笔 均=${y_avg:+.3f}  |  "
              f"仅NO成交: {r.no_only_total}笔 均=${n_avg:+.3f}")
    print(f"    总P&L: ${r.total_pnl:+.2f} | 均P&L: ${r.avg_pnl:+.3f} "
          f"| 胜率: {r.win_rate:.1%} | 最佳: ${r.best:+.2f} | 最差: ${r.worst:+.2f}")


def print_summary(results: list[StrategyResult]) -> None:
    """打印对比总结。"""
    print()
    print("=" * 80)
    print("策略对比总结")
    print("=" * 80)
    print(f"  {'策略':<40} {'笔数':>5} {'两侧%':>7} {'单侧%':>7} "
          f"{'总P&L':>10} {'均P&L':>9} {'胜率':>7}")
    print("  " + "-" * 75)
    for r in results:
        both_pct = f"{r.both_fill/max(r.traded,1):.1%}"
        one_pct = f"{r.one_fill/max(r.traded,1):.1%}"
        print(f"  {r.name:<40} {r.traded:>5} {both_pct:>7} {one_pct:>7} "
              f"${r.total_pnl:>+9.2f} ${r.avg_pnl:>+8.3f} {r.win_rate:>6.1%}")


# ═══════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="多策略回测")
    parser.add_argument("--data", default="../data/", help="JSONL 数据目录")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    data_dir = (script_dir / args.data).resolve()

    print(f"数据目录: {data_dir}")
    print("加载数据...")
    try:
        events = load_events(str(data_dir))
    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  Event 数: {len(events)}")

    results = []

    # ── 策略 1: 买侧 NoFilter (S=0.18, k=1.8) ──
    print("\n回测: 买侧 NoFilter ...")
    r = run_variant(events, "买侧 NoFilter S=0.18 k=1.8",
                    lambda snaps, entry, out: simulate_buy(snaps, entry, out, 0.18, 1.8))
    results.append(r)
    print_result(r)

    # ── 策略 2: 买侧 F8避坑 (避开 F8 ∈ [0.88, 0.95)) ──
    print("\n回测: 买侧 F8避坑 ...")
    def f8_avoid_filter(entry, snaps):
        f8 = compute_f8(snaps, entry)
        return not (0.88 <= f8 < 0.95)
    r = run_variant(events, "买侧 避开F8[0.88,0.95) S=0.18 k=1.8",
                    lambda snaps, entry, out: simulate_buy(snaps, entry, out, 0.18, 1.8),
                    entry_filter_fn=f8_avoid_filter)
    results.append(r)
    print_result(r)

    # ── 策略 3: 卖侧 NoFilter (S=0.26, k=0.1) ──
    print("\n回测: 卖侧 NoFilter ...")
    r = run_variant(events, "卖侧 NoFilter S=0.26 k=0.1",
                    lambda snaps, entry, out: simulate_sell(snaps, entry, out, 0.26, 0.1))
    results.append(r)
    print_result(r)

    # ── 策略 4: 卖侧 F8>0.95 + 公式 (S=0.26, k=0.1) ──
    print("\n回测: 卖侧 F8>0.95 + 公式 ...")
    def f8_filter(entry, snaps):
        return compute_f8(snaps, entry) > 0.95
    r = run_variant(events, "卖侧 F8>0.95 S=0.26 k=0.1",
                    lambda snaps, entry, out: simulate_sell(snaps, entry, out, 0.26, 0.1),
                    entry_filter_fn=f8_filter)
    results.append(r)
    print_result(r)

    # ── 策略 5: 卖侧 F8>0.95 + 固定 spread (0.12/0.14) ──
    print("\n回测: 卖侧 F8>0.95 + 固定spread ...")
    r = run_variant(events, "卖侧 F8>0.95 固定sp=0.12/0.14",
                    lambda snaps, entry, out: simulate_sell_fixed(snaps, entry, out, 0.12, 0.14),
                    entry_filter_fn=f8_filter)
    results.append(r)
    print_result(r)

    # ── 汇总 ──
    print_summary(results)


if __name__ == "__main__":
    main()
