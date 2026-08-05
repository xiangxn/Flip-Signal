#!/usr/bin/env python3
"""
F8 非对称做市策略回测 + Spread 分桶分析。

仅回测一种策略：卖侧 + F8 > 0.95 + 固定非对称 spread。
对 (high_spread, low_spread) 做网格搜索，按 spread 水平分桶，找出最优参数。

Usage:
    python backtest_f8_spread_bucket.py --data ../data/
    python backtest_f8_spread_bucket.py --data ../data/ --detail   # 打印每笔交易明细
"""

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── 策略固定参数（来自 docs/f8_asymmetric_strategy.md）──
ENTRY_SEC = 220          # 入场时间（距收盘秒数，即开盘后 80s）
WARMUP_SEC = 60          # 热身期
TOLERANCE = 10           # 快照查找容忍度
CAPITAL = 5.0            # Split 金额
F8_THRESHOLD = 0.95      # F8 入场阈值


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════
# F8 策略模拟
# ═══════════════════════════════════════════════════════════════

def simulate_f8(snapshots: list[dict], entry_snap: dict,
                outcome: int, high_sp: float, low_sp: float,
                capital: float = CAPITAL) -> dict:
    """F8 非对称卖侧策略：split 后挂限价卖单。

    高价侧（>0.5）加 high_sp，低价侧（<0.5）加 low_sp。
    返回详细交易结果字典。
    """
    yes_e = entry_snap["yes_price"]
    no_e = entry_snap["no_price"]
    rem = entry_snap["remaining_sec"]

    # 非对称定价：高价侧加小 spread，低价侧加大 spread
    if yes_e >= no_e:
        yes_ask = min(yes_e + high_sp, 0.99)
        no_ask = min(no_e + low_sp, 0.99)
        high_side = "YES"
    else:
        yes_ask = min(yes_e + low_sp, 0.99)
        no_ask = min(no_e + high_sp, 0.99)
        high_side = "NO"

    # 扫描成交（限价卖单按挂单价成交，用 ask 价格算收入）
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
        revenue = qty * (1.0 if outcome == 0 else 0.0) + qty * (1.0 if outcome == 1 else 0.0)
        pnl = revenue - capital  # = 0

    fill_type = ("BOTH" if (yf and nf) else
                 "YES only" if (yf and not nf) else
                 "NO only" if (not yf and nf) else
                 "NONE")

    return {
        "pnl": pnl,
        "yes_entry": yes_e, "no_entry": no_e,
        "yes_ask": yes_ask, "no_ask": no_ask,
        "yes_filled": yf, "no_filled": nf,
        "fill_type": fill_type,
        "outcome": outcome,
        "high_side": high_side,
    }


# ═══════════════════════════════════════════════════════════════
# 单个 spread 组合回测
# ═══════════════════════════════════════════════════════════════

@dataclass
class BucketResult:
    """单个 spread 组合的回测结果。"""
    high_sp: float
    low_sp: float
    asymmetry: float          # low_sp - high_sp
    avg_spread: float          # (high_sp + low_sp) / 2
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
    trades: list = field(default_factory=list)

    @property
    def both_pct(self) -> float:
        return self.both_fill / max(self.traded, 1)

    @property
    def one_pct(self) -> float:
        return self.one_fill / max(self.traded, 1)

    @property
    def both_avg_pnl(self) -> float:
        return self.both_pnl_total / max(self.both_fill, 1)

    @property
    def one_avg_pnl(self) -> float:
        return self.one_pnl_total / max(self.one_fill, 1)


def run_single_spread(events: list[dict], high_sp: float, low_sp: float,
                      f8_threshold: float = F8_THRESHOLD) -> BucketResult:
    """对给定 spread 组合运行 F8 策略回测。"""
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

        # F8 入场过滤器
        f8 = compute_f8(snaps, entry)
        if f8 <= f8_threshold:
            skipped += 1
            continue

        tr = simulate_f8(snaps, entry, e["outcome"], high_sp, low_sp)
        tr["event_id"] = e.get("condition_id", "")[:12]
        tr["f8"] = f8
        trades.append(tr)

    r = BucketResult(high_sp=high_sp, low_sp=low_sp,
                     asymmetry=round(low_sp - high_sp, 4),
                     avg_spread=round((high_sp + low_sp) / 2, 4),
                     total_events=total, traded=len(trades), skipped=skipped)

    if not trades:
        return r

    pnls = [t["pnl"] for t in trades]
    r.both_fill = sum(1 for t in trades if t["fill_type"] == "BOTH")
    r.one_fill = sum(1 for t in trades if "only" in t["fill_type"])
    r.none_fill = sum(1 for t in trades if t["fill_type"] == "NONE")

    both_pnls = [t["pnl"] for t in trades if t["fill_type"] == "BOTH"]
    one_pnls = [t["pnl"] for t in trades if "only" in t["fill_type"]]

    r.both_pnl_total = sum(both_pnls)
    r.one_pnl_total = sum(one_pnls)
    r.total_pnl = sum(pnls)
    r.avg_pnl = statistics.mean(pnls) if pnls else 0.0
    r.win_rate = sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0.0
    r.best = max(pnls)
    r.worst = min(pnls)
    r.trades = trades

    return r


# ═══════════════════════════════════════════════════════════════
# Spread 分桶定义
# ═══════════════════════════════════════════════════════════════

def define_spread_buckets() -> dict[str, list[tuple[float, float]]]:
    """定义 spread 分桶。

    Bucket 逻辑（按 low_sp 为主，high_sp 为辅）：
      - 保守型：low ≤ 0.10（薄利多销）
      - 温和型：low ∈ (0.10, 0.16]
      - 激进型：low ∈ (0.16, 0.22]
      - 极度激进：low > 0.22
    """
    return {
        "保守 (low≤0.10)":          [],
        "温和 (low 0.12-0.14)":     [],
        "中性 (low 0.16-0.18)":     [],
        "激进 (low 0.20-0.22)":     [],
        "极度激进 (low≥0.24)":      [],
    }


def bucket_name_for(low_sp: float) -> str:
    if low_sp <= 0.10:
        return "保守 (low≤0.10)"
    elif low_sp <= 0.14:
        return "温和 (low 0.12-0.14)"
    elif low_sp <= 0.18:
        return "中性 (low 0.16-0.18)"
    elif low_sp <= 0.22:
        return "激进 (low 0.20-0.22)"
    else:
        return "极度激进 (low≥0.24)"


# ═══════════════════════════════════════════════════════════════
# 报告输出
# ═══════════════════════════════════════════════════════════════

def print_header(title: str, width: int = 90) -> None:
    print(f"\n{'═' * width}")
    print(f"  {title}")
    print(f"{'═' * width}")


def print_separator(width: int = 90) -> None:
    print("  " + "─" * (width - 2))


def print_single_result(r: BucketResult) -> None:
    """打印单个 spread 组合结果。"""
    print(f"  Spread high={r.high_sp:.2f} low={r.low_sp:.2f} "
          f"(非对称差={r.asymmetry:.2f}, 均值={r.avg_spread:.2f})")
    print(f"    总Event: {r.total_events} | 入场: {r.traded} | 跳过: {r.skipped}")
    print(f"    两侧成交: {r.both_fill} ({r.both_pct:.1%}) "
          f"| 均P&L=${r.both_avg_pnl:+.3f} "
          f"| 合计=${r.both_pnl_total:+.2f}")
    print(f"    单侧成交: {r.one_fill} ({r.one_pct:.1%}) "
          f"| 均P&L=${r.one_avg_pnl:+.3f} "
          f"| 合计=${r.one_pnl_total:+.2f}")
    print(f"    不成交:   {r.none_fill}")
    print(f"    总P&L: ${r.total_pnl:+.2f} | 均P&L: ${r.avg_pnl:+.3f} "
          f"| 胜率: {r.win_rate:.1%} | 最佳: ${r.best:+.2f} | 最差: ${r.worst:+.2f}")


def print_bucket_summary(bucket_name: str, results: list[BucketResult]) -> None:
    """打印一个分桶的汇总。"""
    if not results:
        print(f"\n  [{bucket_name}] — 无数据")
        return

    print(f"\n  ┌─ [{bucket_name}] ({len(results)} 个 spread 组合) ─┐")

    # 按总P&L排序
    sorted_results = sorted(results, key=lambda r: -r.total_pnl)

    print(f"  │ {'high/low':>10} {'非对称差':>8} {'笔数':>5} {'两侧%':>7} "
          f"{'单侧%':>7} {'总P&L':>9} {'均P&L':>8} {'胜率':>7} │")

    for r in sorted_results:
        label = f"{r.high_sp:.2f}/{r.low_sp:.2f}"
        print(f"  │ {label:>10} {r.asymmetry:>8.2f} {r.traded:>5} "
              f"{r.both_pct:>6.1%} {r.one_pct:>6.1%} "
              f"${r.total_pnl:>+8.2f} ${r.avg_pnl:>+7.3f} {r.win_rate:>6.1%} │")

    # 桶内最佳
    best = sorted_results[0]
    print(f"  └─ 最佳: high={best.high_sp:.2f} low={best.low_sp:.2f} "
          f"总P&L=${best.total_pnl:+.2f} 均P&L=${best.avg_pnl:+.3f} ─┘")


def print_summary_table(all_results: list[BucketResult]) -> None:
    """打印全量对比表，按总 P&L 排序。"""
    print_header("全量 Spread 组合排名 (按总P&L)")
    print(f"  {'排名':>4} {'high_sp':>8} {'low_sp':>8} {'非对称差':>8} "
          f"{'笔数':>5} {'两侧%':>7} {'单侧%':>7} "
          f"{'总P&L':>9} {'均P&L':>8} {'胜率':>7} {'最佳':>8} {'最差':>8}")
    print_separator()

    sorted_results = sorted(all_results, key=lambda r: -r.total_pnl)

    for i, r in enumerate(sorted_results, 1):
        marker = " ←" if i == 1 else ""
        print(f"  {i:>4} {r.high_sp:>8.2f} {r.low_sp:>8.2f} {r.asymmetry:>8.2f} "
              f"{r.traded:>5} {r.both_pct:>6.1%} {r.one_pct:>6.1%} "
              f"${r.total_pnl:>+8.2f} ${r.avg_pnl:>+7.3f} {r.win_rate:>6.1%} "
              f"${r.best:>+7.2f} ${r.worst:>+7.2f}{marker}")


def print_heatmap(all_results: list[BucketResult]) -> None:
    """打印 P&L 热力图 (high_sp × low_sp)。"""
    high_vals = sorted(set(r.high_sp for r in all_results))
    low_vals = sorted(set(r.low_sp for r in all_results))

    # 建立查找表
    pnl_map = {}
    trade_map = {}
    for r in all_results:
        pnl_map[(r.high_sp, r.low_sp)] = r.total_pnl
        trade_map[(r.high_sp, r.low_sp)] = r.traded

    print_header("P&L 热力图 (high_sp × low_sp)")
    print(f"  每个单元格: 总P&L (笔数)")
    print(f"  空白 = 无效组合 (low_sp < high_sp)")

    # 表头
    header = f"  {'high↓ low→':>12}"
    for lv in low_vals:
        header += f" {lv:>10.2f}"
    print(header)
    print_separator()

    for hv in high_vals:
        row = f"  {hv:>12.2f}"
        for lv in low_vals:
            if lv < hv:
                row += f" {'—':>10}"
            elif (hv, lv) in pnl_map:
                p = pnl_map[(hv, lv)]
                n = trade_map[(hv, lv)]
                row += f" ${p:>+7.2f}({n:>2})"
            else:
                row += f" {'N/A':>10}"
        print(row)

    # 标识当前策略参数
    print(f"\n  ★ 当前策略: high=0.12 low=0.14")


def print_trade_details(r: BucketResult) -> None:
    """打印每笔交易明细。"""
    if not r.trades:
        print("  (无交易)")
        return

    print(f"\n  交易明细 (high={r.high_sp:.2f}, low={r.low_sp:.2f}):")
    print(f"  {'#':>3} {'F8':>6} {'YES入':>7} {'NO入':>7} "
          f"{'YES挂':>7} {'NO挂':>7} {'成交':>8} {'结果':>5} {'P&L':>8}")
    print_separator()

    for i, t in enumerate(r.trades, 1):
        outcome_str = "YES赢" if t["outcome"] == 0 else "NO赢"
        print(f"  {i:>3} {t['f8']:>6.3f} {t['yes_entry']:>7.3f} {t['no_entry']:>7.3f} "
              f"{t['yes_ask']:>7.3f} {t['no_ask']:>7.3f} {t['fill_type']:>8} "
              f"{outcome_str:>5} ${t['pnl']:>+7.2f}")


def compute_f8_buckets(events: list[dict], high_sp: float, low_sp: float,
                       f8_threshold: float = F8_THRESHOLD) -> list[dict]:
    """按 F8 区间分桶统计 P&L。

    返回列表，每项为 {label, total_events, trades, both, one, total_pnl, avg_pnl, win_rate}。
    低于 f8_threshold 的区间标注 ⚠️ 表示"若强行交易"的假设回测。
    """
    f8_ranges = [
        (0.80, 0.85), (0.85, 0.90), (0.90, 0.92), (0.92, 0.94),
        (0.94, 0.96), (0.96, 0.98), (0.98, 1.01),
    ]
    results = []
    for lo, hi in f8_ranges:
        bt, btotal = [], 0
        for e in events:
            snaps = e["snapshots"]
            entry = find_snapshot(snaps, ENTRY_SEC)
            if entry is None or entry["yes_price"] <= 0 or entry["no_price"] <= 0:
                continue
            if entry["remaining_sec"] > 300 - WARMUP_SEC:
                continue
            f8 = compute_f8(snaps, entry)
            if lo <= f8 < hi:
                btotal += 1
                tr = simulate_f8(snaps, entry, e["outcome"], high_sp, low_sp)
                tr["f8"] = f8
                bt.append(tr)

        is_below_threshold = hi <= f8_threshold
        suffix = " ⚠️假设" if is_below_threshold else ""

        if not bt:
            results.append({
                "label": f"[{lo:.2f}, {hi:.2f}){suffix}",
                "total_events": btotal, "trades": 0,
                "both": 0, "one": 0, "total_pnl": 0.0, "avg_pnl": float("nan"),
                "win_rate": float("nan"),
            })
            continue

        pnls = [t["pnl"] for t in bt]
        both_n = sum(1 for t in bt if t["fill_type"] == "BOTH")
        one_n = sum(1 for t in bt if "only" in t["fill_type"])
        total = sum(pnls)
        avg = statistics.mean(pnls)
        wr = sum(1 for p in pnls if p > 0) / len(pnls)

        results.append({
            "label": f"[{lo:.2f}, {hi:.2f}){suffix}",
            "total_events": btotal, "trades": len(bt),
            "both": both_n, "one": one_n,
            "total_pnl": total, "avg_pnl": avg, "win_rate": wr,
        })
    return results


def print_f8_bucket_table(buckets: list[dict], title: str) -> None:
    """打印 F8 分桶统计表。"""
    header = "⚠️ 标注 = 低于 F8 阈值，仅假设回测，实盘不会交易该区间"
    print(f"\n  {title}")
    print(f"  {header}")
    print(f"  {'F8区间':<24} {'Event':>6} {'笔数':>4} {'两侧%':>7} {'单侧%':>7} "
          f"{'总P&L':>9} {'均P&L':>8} {'胜率':>7}")
    print_separator()
    for b in buckets:
        if b["trades"] == 0:
            print(f"  {b['label']:<24} {b['total_events']:>6} {'—':>4} "
                  f"{'—':>7} {'—':>7} {'—':>9} {'—':>8} {'—':>7}")
        else:
            both_pct = b["both"] / b["trades"]
            one_pct = b["one"] / b["trades"]
            print(f"  {b['label']:<24} {b['total_events']:>6} {b['trades']:>4} "
                  f"{both_pct:>6.1%} {one_pct:>6.1%} "
                  f"${b['total_pnl']:>+8.2f} ${b['avg_pnl']:>+7.3f} {b['win_rate']:>6.1%}")


def print_bucket_aggregate(buckets: dict[str, list[BucketResult]]) -> None:
    """打印分桶聚合统计：每个桶内取最优 spread 组合。"""
    print_header("Spread 分桶聚合 (每桶最优组合)")

    print(f"  {'分桶':<28} {'最优high':>9} {'最优low':>8} {'笔数':>5} "
          f"{'两侧%':>7} {'总P&L':>9} {'均P&L':>8} {'胜率':>7}")
    print_separator()

    for bucket_name in [
        "保守 (low≤0.10)",
        "温和 (low 0.12-0.14)",
        "中性 (low 0.16-0.18)",
        "激进 (low 0.20-0.22)",
        "极度激进 (low≥0.24)",
    ]:
        results = buckets.get(bucket_name, [])
        if not results:
            print(f"  {bucket_name:<28} {'—':>9} {'—':>8} {'—':>5} "
                  f"{'—':>7} {'—':>9} {'—':>8} {'—':>7}")
            continue

        best = max(results, key=lambda r: r.total_pnl)
        print(f"  {bucket_name:<28} {best.high_sp:>9.2f} {best.low_sp:>8.2f} "
              f"{best.traded:>5} {best.both_pct:>6.1%} "
              f"${best.total_pnl:>+8.2f} ${best.avg_pnl:>+7.3f} {best.win_rate:>6.1%}")


def print_symmetric_spread_analysis(events: list[dict], f8_threshold: float,
                                     spread_range: list[float]) -> list[BucketResult]:
    """对称 Spread 分桶分析：仅测试 high=low 的对称 spread。

    二元期权 UP/DOWN 具有天然对称性，对称 spread 更简洁、不易过拟合。
    """
    print_header(f"⑥ 对称 Spread 分桶 (spread {spread_range[0]:.2f}–{spread_range[-1]:.2f}, step=0.01)")

    sym_results: list[BucketResult] = []
    for sp in spread_range:
        r = run_single_spread(events, sp, sp, f8_threshold)
        sym_results.append(r)

    # ── 汇总表 ──
    print(f"\n  {'spread':>8} {'笔数':>5} {'两侧':>5} {'单侧':>5} "
          f"{'两侧%':>7} {'单侧%':>7} "
          f"{'两侧均':>8} {'单侧均':>8} {'总P&L':>9} {'均P&L':>8} {'胜率':>7} "
          f"{'最佳':>8} {'最差':>8}")
    print_separator()

    for r in sym_results:
        print(f"  {r.high_sp:>8.2f} {r.traded:>5} {r.both_fill:>5} {r.one_fill:>5} "
              f"{r.both_pct:>6.1%} {r.one_pct:>6.1%} "
              f"${r.both_avg_pnl:>+7.3f} ${r.one_avg_pnl:>+7.3f} "
              f"${r.total_pnl:>+8.2f} ${r.avg_pnl:>+7.3f} {r.win_rate:>6.1%} "
              f"${r.best:>+7.2f} ${r.worst:>+7.2f}")

    # ── 最优 ──
    best_sym = max(sym_results, key=lambda r: r.total_pnl)
    worst_sym = min(sym_results, key=lambda r: r.total_pnl)
    print(f"\n  对称最优: sp={best_sym.high_sp:.2f} → 总P&L=${best_sym.total_pnl:+.2f} "
          f"均P&L=${best_sym.avg_pnl:+.3f} 两侧率={best_sym.both_pct:.1%}")
    print(f"  对称最差: sp={worst_sym.high_sp:.2f} → 总P&L=${worst_sym.total_pnl:+.2f}")

    # ── 关键发现 ──
    print(f"\n  ── 解读 ──")
    profitable = [r for r in sym_results if r.total_pnl > 0]
    if profitable:
        best_prof = max(profitable, key=lambda r: r.total_pnl)
        print(f"  • 对称 spread 在 [{spread_range[0]:.2f}, {spread_range[-1]:.2f}] 区间内"
              f"大部分为正期望")
        print(f"  • 峰值在 sp={best_prof.high_sp:.2f}，总P&L=${best_prof.total_pnl:+.2f}")

    # 观察 spread 增加时两侧率如何变化
    first, last = sym_results[0], sym_results[-1]
    print(f"  • spread {first.high_sp:.2f}→{last.high_sp:.2f}: "
          f"两侧率 {first.both_pct:.1%}→{last.both_pct:.1%}, "
          f"两侧均P&L ${first.both_avg_pnl:+.3f}→${last.both_avg_pnl:+.3f}, "
          f"单侧均P&L ${first.one_avg_pnl:+.3f}→${last.one_avg_pnl:+.3f}")

    # ── 交易明细：最优对称 spread ──
    print(f"\n  ── 最优对称 sp={best_sym.high_sp:.2f} 交易明细 ──")
    print_trade_details(best_sym)

    # ── F8 分桶（共享函数）──
    f8_buckets = compute_f8_buckets(events, best_sym.high_sp, best_sym.high_sp, f8_threshold)
    print_f8_bucket_table(f8_buckets, f"F8 分桶 (对称 sp={best_sym.high_sp:.2f})")

    return sym_results


def print_f8_bucket_analysis(events: list[dict], high_sp: float, low_sp: float) -> None:
    """F8 分桶分析：对给定 spread 参数，按 F8 区间分桶统计。"""
    print_header(f"F8 分桶分析 (spread high={high_sp:.2f} low={low_sp:.2f})")
    f8_buckets = compute_f8_buckets(events, high_sp, low_sp, F8_THRESHOLD)
    print_f8_bucket_table(f8_buckets, "")


# ═══════════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="F8 非对称做市策略回测 + Spread 分桶分析")
    parser.add_argument("--data", default="../data/", help="JSONL 数据目录")
    parser.add_argument("--detail", action="store_true",
                        help="打印最优组合的每笔交易明细")
    parser.add_argument("--f8-threshold", type=float, default=F8_THRESHOLD,
                        help=f"F8 入场阈值 (默认: {F8_THRESHOLD})")
    parser.add_argument("--high-min", type=float, default=0.06,
                        help="high_sp 搜索下限 (默认: 0.06)")
    parser.add_argument("--high-max", type=float, default=0.20,
                        help="high_sp 搜索上限 (默认: 0.20)")
    parser.add_argument("--low-min", type=float, default=0.08,
                        help="low_sp 搜索下限 (默认: 0.08)")
    parser.add_argument("--low-max", type=float, default=0.26,
                        help="low_sp 搜索上限 (默认: 0.26)")
    parser.add_argument("--step", type=float, default=0.02,
                        help="spread 搜索步长 (默认: 0.02)")
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

    # ── 生成 spread 网格 ──
    high_range = [round(args.high_min + i * args.step, 2)
                  for i in range(int((args.high_max - args.high_min) / args.step) + 1)]
    low_range = [round(args.low_min + i * args.step, 2)
                 for i in range(int((args.low_max - args.low_min) / args.step) + 1)]

    spread_pairs = [(h, l) for h in high_range for l in low_range if l >= h]
    print(f"\nSpread 网格: high ∈ {high_range}")
    print(f"             low  ∈ {low_range}")
    print(f"有效组合数: {len(spread_pairs)} (low ≥ high)")

    # ── 运行所有 spread 组合 ──
    print_header("F8 策略回测 — Spread 网格搜索")
    print(f"  参数: entry={ENTRY_SEC}s, warmup={WARMUP_SEC}s, "
          f"F8>{args.f8_threshold}, capital=${CAPITAL}")

    all_results: list[BucketResult] = []
    buckets = define_spread_buckets()

    for i, (h, l) in enumerate(spread_pairs):
        pct = (i + 1) / len(spread_pairs) * 100
        print(f"\r  回测中... {i+1}/{len(spread_pairs)} ({pct:.0f}%) "
              f"high={h:.2f} low={l:.2f}", end="", flush=True)

        r = run_single_spread(events, h, l, args.f8_threshold)
        all_results.append(r)

        # 分桶
        bn = bucket_name_for(l)
        buckets[bn].append(r)

    print("\n")

    # ═══════════════════════════════════════════════════════════
    # 报告输出
    # ═══════════════════════════════════════════════════════════

    # 1. 当前策略基准
    print_header("① 当前策略基准 (high=0.12, low=0.14)")
    baseline = next((r for r in all_results
                     if r.high_sp == 0.12 and r.low_sp == 0.14), None)
    if baseline:
        print_single_result(baseline)
        if args.detail:
            print_trade_details(baseline)
    else:
        print("  (0.12/0.14 不在搜索范围内)")

    # 2. 全量排名表
    print_summary_table(all_results)

    # 3. P&L 热力图
    print_heatmap(all_results)

    # 4. Spread 分桶聚合
    print_bucket_aggregate(buckets)

    # 5. 每个桶的详细展开
    print_header("② Spread 分桶详情")
    for bucket_name in [
        "保守 (low≤0.10)",
        "温和 (low 0.12-0.14)",
        "中性 (low 0.16-0.18)",
        "激进 (low 0.20-0.22)",
        "极度激进 (low≥0.24)",
    ]:
        print_bucket_summary(bucket_name, buckets.get(bucket_name, []))

    # 6. 最优组合详情
    best = max(all_results, key=lambda r: r.total_pnl)
    print_header("③ 最优 Spread 组合")
    print_single_result(best)
    if args.detail:
        print_trade_details(best)

    # 7. F8 分桶分析（使用最优 spread）
    print_f8_bucket_analysis(events, best.high_sp, best.low_sp)

    # 8. 与文档基线对比
    print_header("④ 与文档基线对比")
    print(f"  {'策略':<45} {'笔数':>5} {'两侧%':>7} {'均P&L':>9} {'总P&L':>9}")
    print_separator()

    doc_baseline_total = 9.07
    doc_baseline_trades = 24
    print(f"  {'文档基线 F8>0.95 固定sp=0.12/0.14':<45} "
          f"{doc_baseline_trades:>5} {'70.8%':>7} "
          f"${0.378:>+8.3f} ${doc_baseline_total:>+8.2f}")

    if baseline:
        delta = baseline.total_pnl - doc_baseline_total
        print(f"  {'本次回测 相同参数':<45} "
              f"{baseline.traded:>5} {baseline.both_pct:>6.1%} "
              f"${baseline.avg_pnl:>+8.3f} ${baseline.total_pnl:>+8.2f} "
              f"(Δ{delta:+.2f})")

    print(f"  {'本次搜索最优 high=' + str(best.high_sp) + ' low=' + str(best.low_sp):<45} "
          f"{best.traded:>5} {best.both_pct:>6.1%} "
          f"${best.avg_pnl:>+8.3f} ${best.total_pnl:>+8.2f}")

    # 9. 非对称差 vs 均值分析
    print_header("⑤ Spread 维度分析")

    # 按 asymmetry 分组
    asym_buckets: dict[float, list[BucketResult]] = {}
    for r in all_results:
        asym_buckets.setdefault(r.asymmetry, []).append(r)

    print(f"\n  非对称差 (low-high) 对 P&L 的影响:")
    print(f"  {'非对称差':>10} {'组合数':>7} {'平均总P&L':>11} {'最佳总P&L':>11} "
          f"{'平均笔数':>8} {'平均两侧%':>9}")
    print_separator()
    for asym in sorted(asym_buckets.keys()):
        group = asym_buckets[asym]
        avg_pnl = statistics.mean([r.total_pnl for r in group])
        best_pnl = max(r.total_pnl for r in group)
        avg_trades = statistics.mean([r.traded for r in group])
        avg_both = statistics.mean([r.both_pct for r in group])
        print(f"  {asym:>10.2f} {len(group):>7} ${avg_pnl:>+10.2f} ${best_pnl:>+10.2f} "
              f"{avg_trades:>8.1f} {avg_both:>8.1%}")

    # 10. 对称 Spread 分桶
    sym_range = [round(0.06 + i * 0.01, 2) for i in range(7)]  # 0.06, 0.07, ..., 0.12
    sym_results = print_symmetric_spread_analysis(events, args.f8_threshold, sym_range)

    # 11. 对称 vs 最优非对称 对比
    best_asym = max(all_results, key=lambda r: r.total_pnl)
    best_sym = max(sym_results, key=lambda r: r.total_pnl)
    print_header("⑦ 对称 vs 最优非对称 最终对比")
    print(f"  {'类型':<30} {'spread':>14} {'笔数':>5} {'两侧%':>7} "
          f"{'总P&L':>9} {'均P&L':>8} {'胜率':>7}")
    print_separator()
    for label, r in [("对称最优", best_sym), ("非对称最优", best_asym)]:
        sp_label = f"sp={r.high_sp:.2f}/{r.low_sp:.2f}"
        print(f"  {label:<30} {sp_label:>14} {r.traded:>5} {r.both_pct:>6.1%} "
              f"${r.total_pnl:>+8.2f} ${r.avg_pnl:>+7.3f} {r.win_rate:>6.1%}")
    delta = best_asym.total_pnl - best_sym.total_pnl
    print(f"\n  非对称相比对称多赚: ${delta:+.2f} "
          f"({'值得' if delta > 1 else '边际' if delta > 0 else '不值得'})")

    print()


if __name__ == "__main__":
    main()
