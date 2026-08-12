#!/usr/bin/env python3
"""
入场价上限 gate 与确认窗口的推演分析

背景问题:
  添加 max_entry_price gate 后, 胜率/P&L/盈亏比/信号量反而全面变差 — 为什么?

推演线索:
  1. 引擎的 entry_price 是穿越时刻(T=0)对侧价, 确认在 T+delay_ticks×5s 后。
  2. 旧回测默认按穿越价成交 — 但实盘要等确认期结束才能行动, 只能按确认时刻价成交。
  3. gate 砍掉的信号是"强确认(other_delta 大)但价格已跑掉"的:
     它们赢面大(翻转真实发生), 但盈亏比按确认价算可能已无利可图。
  4. 幸存者(确认价≤0.35) other_delta 弱 → 确认力度弱 → 胜率低。
  5. 关键变量: confirm_delay_ticks 越长 → 对侧反弹越多 → 入场价恶化越多。

输出:
  1. delay=5 无 gate 信号按"确认价是否可成交(≤0.35)"分组对比
  2. 各 delay(1..6) 下两种成交口径的对比: 穿越价(旧假设) vs 确认价(真实)
  3. other_delta 分桶: 胜率 / 确认价 / 真实 P&L 的关系
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_flip_config import FlipBacktestConfig
from backtest_flip_utils import load_events_from_dir, run_backtest

DATA_DIR = str(Path(__file__).resolve().parent.parent / "data_0" / "lab")
MAX_ENTRY = 0.35


def pnl_at(fill_price: float, won: bool, shares: int = 1) -> float:
    """按成交价 fill_price 计算单笔盈亏。"""
    return (1.0 - fill_price) * shares if won else -fill_price * shares


def stats(signals, fill_mode: str):
    """按指定成交价口径统计一组信号。

    fill_mode:
      'cross'   = 穿越价成交（旧回测假设, 实盘不可达）
      'confirm' = 确认价成交（实盘真实可成交价, = entry_price + other_delta）
    """
    if not signals:
        return None
    n = len(signals)
    wins = sum(1 for s in signals if s.won)
    pnls = []
    for s in signals:
        fp = s.entry_price if fill_mode == "cross" else (s.entry_price + s.other_delta)
        pnls.append(pnl_at(fp, s.won, s.shares))
    total = sum(pnls)
    losers = sum(p for p in pnls if p < 0)
    wr = wins / n * 100
    rr = total / abs(losers) if losers < 0 and total > 0 else 0.0
    avg_fill = sum(s.entry_price + s.other_delta for s in signals) / n
    return {"n": n, "wins": wins, "wr": wr, "total": total, "rr": rr, "avg_fill": avg_fill}


def fmt(s):
    """统一格式化 stats 为一行。"""
    if s is None:
        return "  (无)"
    return (f"n={s['n']:>3d}  WR={s['wr']:>5.1f}%  "
            f"P&L={s['total']:>+7.2f}  盈亏比={s['rr']:>4.1f}  "
            f"avg确认价={s['avg_fill']:.3f}")


def main():
    events = load_events_from_dir(DATA_DIR)
    print(f"数据: {len(events)} 事件")

    # ═══════════════════════════════════════════════════════════
    # 1. delay=5 无 gate: 按确认价分组 — 解释 gate 后全面变差的悖论
    # ═══════════════════════════════════════════════════════════
    print("=" * 100)
    print("  1) delay=5 (25s) 无 gate 信号, 按确认价是否可成交(≤0.35)分组")
    print("=" * 100)
    cfg5 = FlipBacktestConfig()
    cfg5.confirm_delay_ticks = 5  # 本部分固定分析旧默认 25s 窗口
    cfg5.max_entry_price = 0      # 关闭 gate, 看全部信号
    sigs5, _, _ = run_backtest(events, cfg5)

    cheap = [s for s in sigs5 if s.entry_price + s.other_delta <= MAX_ENTRY]
    expensive = [s for s in sigs5 if s.entry_price + s.other_delta > MAX_ENTRY]

    print(f"\n  总信号: {len(sigs5)}")
    print(f"\n  A) 确认价 ≤ {MAX_ENTRY} (gate 幸存, 实盘可成交):   {fmt(stats(cheap, 'confirm'))}")
    print(f"     同一组按穿越价成交(旧回测口径):                 {fmt(stats(cheap, 'cross'))}")
    print(f"\n  B) 确认价 > {MAX_ENTRY} (gate 砍掉, 价格已跑):      {fmt(stats(expensive, 'confirm'))}")
    print(f"     同一组按穿越价成交(旧回测口径):                 {fmt(stats(expensive, 'cross'))}")
    print(f"\n  推演: B 组是强确认信号(翻转真实发生, 胜率高), 但旧回测的利润来自")
    print(f"        穿越价成交 — 实盘等 25s 后价格已跑, 按真实确认价成交利润大幅缩水。")
    print(f"        gate 把 B 组全砍掉, 只剩确认力度弱的 A 组 → 胜率/盈亏比全面变差。")

    # ═══════════════════════════════════════════════════════════
    # 2. 各 delay 下两种成交口径对比 — 验证"确认 tick 不能太长"
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("  2) confirm_delay_ticks 扫描: 穿越价成交(旧) vs 确认价成交(真实)")
    print("=" * 100)
    print(f"\n  {'delay':>5} | {'信号':>4} | {'WR':>6} | {'P&L@穿越':>8} | {'P&L@确认':>8} "
          f"| {'盈亏比@确认':>8} | {'avg确认价':>8} | {'可成交≤0.35':>9} | {'gate后P&L@确认':>11}")
    print("  " + "-" * 96)

    for delay in [1, 2, 3, 4, 5, 6]:
        # 无 gate: 全部信号
        cfg = FlipBacktestConfig()
        cfg.confirm_delay_ticks = delay
        cfg.max_entry_price = 0
        sigs, _, _ = run_backtest(events, cfg)

        # 有 gate: 确认价 > 0.35 直接无效
        cfg_g = FlipBacktestConfig()
        cfg_g.confirm_delay_ticks = delay
        cfg_g.max_entry_price = MAX_ENTRY
        sigs_g, _, _ = run_backtest(events, cfg_g)

        s_cross = stats(sigs, "cross")
        s_conf = stats(sigs, "confirm")
        s_gate = stats(sigs_g, "confirm")
        fillable = sum(1 for s in sigs if s.entry_price + s.other_delta <= MAX_ENTRY)

        n = s_cross["n"] if s_cross else 0
        wr = s_cross["wr"] if s_cross else 0
        p_cross = s_cross["total"] if s_cross else 0
        p_conf = s_conf["total"] if s_conf else 0
        rr = s_conf["rr"] if s_conf else 0
        avg_f = s_conf["avg_fill"] if s_conf else 0
        pct_fill = fillable / n * 100 if n else 0
        p_gate = s_gate["total"] if s_gate else 0
        n_gate = s_gate["n"] if s_gate else 0

        print(f"  {delay:>5} | {n:>4} | {wr:>5.1f}% | {p_cross:>+8.2f} | {p_conf:>+8.2f} "
              f"| {rr:>8.2f} | {avg_f:>8.3f} | {fillable:>4}/{n:<3}({pct_fill:>4.1f}%) "
              f"| {p_gate:>+8.2f}(n={n_gate})")

    # ═══════════════════════════════════════════════════════════
    # 3. other_delta 分桶: 确认力度 vs 胜率 vs 价格恶化
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("  3) delay=5 无 gate: other_delta 分桶 (确认力度 vs 价格恶化 vs 真实盈亏)")
    print("=" * 100)
    print(f"\n  {'other_delta':>14} | {'n':>4} | {'WR':>6} | {'avg穿越价':>8} | {'avg确认价':>8} "
          f"| {'P&L@确认':>8} | {'可成交率':>7}")
    print("  " + "-" * 78)

    bins = [
        ("<= 0.01", lambda s: s.other_delta <= 0.01),
        ("(0.01, 0.02]", lambda s: 0.01 < s.other_delta <= 0.02),
        ("(0.02, 0.05]", lambda s: 0.02 < s.other_delta <= 0.05),
        ("(0.05, 0.15]", lambda s: 0.05 < s.other_delta <= 0.15),
        ("> 0.15", lambda s: s.other_delta > 0.15),
    ]
    for label, cond in bins:
        group = [s for s in sigs5 if cond(s)]
        if not group:
            continue
        st = stats(group, "confirm")
        avg_cross = sum(s.entry_price for s in group) / len(group)
        fillable = sum(1 for s in group if s.entry_price + s.other_delta <= MAX_ENTRY)
        print(f"  {label:>14} | {st['n']:>4} | {st['wr']:>5.1f}% | {avg_cross:>8.3f} "
              f"| {st['avg_fill']:>8.3f} | {st['total']:>+8.2f} | "
              f"{fillable}/{st['n']} ({fillable / st['n'] * 100:.0f}%)")

    print(f"\n  推演: other_delta 越大 → 确认越强 → 胜率越高, 但确认价也跑得越远, 可成交率骤降。")
    print(f"        25s 确认把「强确认」与「廉价入场」两个本可兼得的条件变成了互斥。")


if __name__ == "__main__":
    main()
