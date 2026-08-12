#!/usr/bin/env python3
"""
确认窗口 × 入场价 gate × other_delta 阈值 联合扫描

目标口径 (实盘真实):
  - 成交价 = 确认时刻对侧价 (entry_price + other_delta), 不再使用穿越价
  - max_entry_price gate: 确认价 > gate → 信号无效 (实盘 FAK 无法成交)
  - P&L 按确认价成交计算 (每股 1 share)

扫描维度:
  - confirm_delay_ticks: [1, 2, 3]  (更长的窗口入场价恶化严重, 不纳入)
  - od 三档阈值按 1:2:5 比例整体缩放, 弱档 base ∈ [0.005, 0.01, 0.02, 0.03]
  - max_entry_price gate ∈ [0.30, 0.35, 0.40, 0.45]

输出: 按真实 P&L 排序的 top 组合 + 各 delay 下的最优组合。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_flip_config import FlipBacktestConfig
from backtest_flip_utils import load_events_from_dir, run_backtest

DATA_DIR = str(Path(__file__).resolve().parent.parent / "data_0" / "lab")

DELAYS = [1, 2, 3]
OD_BASES = [0.005, 0.01, 0.02, 0.03]   # 弱档阈值, 中档=2x, 强档=5x
GATES = [0.30, 0.35, 0.40, 0.45]


def realistic_stats(signals):
    """按确认价成交的实盘口径统计 (gate 已过滤不可成交信号)。

    三种盈亏比口径:
      rr_net  = 总净盈利 / 总亏损        (代码库 print_summary 旧口径, 依赖胜率)
      rr_per  = 平均单笔盈利 / 平均单笔亏损 (标准盈亏比, 高盈亏比低胜率策略看这个)
      pf      = 总盈利 / 总亏损          (利润因子, gross/gross)
    """
    if not signals:
        return None
    n = len(signals)
    win_pnls, loss_pnls = [], []
    for s in signals:
        fill = s.entry_price + s.other_delta
        p = (1.0 - fill) if s.won else -fill
        (win_pnls if p > 0 else loss_pnls).append(p)
    total = sum(win_pnls) + sum(loss_pnls)
    gross_win = sum(win_pnls)
    gross_loss = abs(sum(loss_pnls))
    avg_fill = sum(s.entry_price + s.other_delta for s in signals) / n
    return {
        "n": n, "wr": len(win_pnls) / n * 100,
        "total": total,
        "rr_net": total / gross_loss if gross_loss > 0 and total > 0 else 0.0,
        "rr_per": (gross_win / len(win_pnls)) / (gross_loss / len(loss_pnls))
                  if win_pnls and loss_pnls else 0.0,
        "pf": gross_win / gross_loss if gross_loss > 0 else 0.0,
        "avg_fill": avg_fill,
    }


def main():
    events = load_events_from_dir(DATA_DIR)
    print(f"数据: {len(events)} 事件")
    print(f"口径: 确认价成交 + gate 过滤 + 1 share\n")

    results = []
    for delay in DELAYS:
        for base in OD_BASES:
            for gate in GATES:
                cfg = FlipBacktestConfig()
                cfg.confirm_delay_ticks = delay
                cfg.other_delta_weak = base
                cfg.other_delta_strong = base * 2
                cfg.other_delta_vstrong = base * 5
                cfg.max_entry_price = gate
                sigs, _, _ = run_backtest(events, cfg)
                st = realistic_stats(sigs)
                if st:
                    results.append({
                        "delay": delay, "od_weak": base, "gate": gate, **st,
                    })

    # ── 1. 按单笔盈亏比 (标准盈亏比) 排序的 top 15 ──
    results.sort(key=lambda r: (-r["rr_per"], -r["total"]))
    print("=" * 112)
    print("  Top 15 组合 (按单笔盈亏比 = 平均盈利/平均亏损 排序, 实盘口径)")
    print("=" * 112)
    print(f"  {'delay':>5} {'od弱档':>7} {'gate':>6} | {'n':>4} {'WR':>7} {'单笔盈亏比':>9} "
          f"{'利润因子':>7} {'P&L':>8} {'avg成交价':>9}")
    print("  " + "-" * 100)
    for r in results[:15]:
        print(f"  {r['delay']:>5} {r['od_weak']:>7.3f} {r['gate']:>6.2f} | "
              f"{r['n']:>4} {r['wr']:>6.1f}% {r['rr_per']:>9.2f} {r['pf']:>7.2f} "
              f"{r['total']:>+8.2f} {r['avg_fill']:>9.3f}")

    # ── 2. 按真实 P&L 排序的 top 15 ──
    results.sort(key=lambda r: -r["total"])
    print("\n" + "=" * 112)
    print("  Top 15 组合 (按实盘口径 P&L 排序)")
    print("=" * 112)
    print(f"  {'delay':>5} {'od弱档':>7} {'gate':>6} | {'n':>4} {'WR':>7} {'单笔盈亏比':>9} "
          f"{'P&L':>8} {'净盈亏比(旧)':>10} {'avg成交价':>9}")
    print("  " + "-" * 100)
    for r in results[:15]:
        print(f"  {r['delay']:>5} {r['od_weak']:>7.3f} {r['gate']:>6.2f} | "
              f"{r['n']:>4} {r['wr']:>6.1f}% {r['rr_per']:>9.2f} {r['total']:>+8.2f} "
              f"{r['rr_net']:>10.2f} {r['avg_fill']:>9.3f}")

    # ── 3. 当前默认参数 (delay=2, od=0.01/0.02/0.05, gate=0.35) 的参考行 ──
    print("\n" + "-" * 112)
    for r in results:
        if (r["delay"] == 2 and r["od_weak"] == 0.01 and r["gate"] == 0.35):
            print(f"  当前默认 (delay=2, od弱=0.01, gate=0.35): n={r['n']} WR={r['wr']:.1f}% "
                  f"单笔盈亏比={r['rr_per']:.2f} 利润因子={r['pf']:.2f} "
                  f"P&L={r['total']:+.2f} avg成交价={r['avg_fill']:.3f}")
    print("  (对比旧参数 delay=5, od弱=0.01, gate=0.35 — 见 analyze_price_gate.py)")


if __name__ == "__main__":
    main()
