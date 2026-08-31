#!/usr/bin/env python3
"""
v3 策略回测（2 USDC/笔）——「自信崩溃」flip。

规则（docs/v3/strategy_plan_2026-08-31.md）:
  C1  穿越时刻触发侧 bid > 0.73（市场高度自信）
  C2  确认时刻（+10s）触发侧 bid ≤ post_end 阈值（默认 0.66）
  决策 +10s, 买入对侧（flip）, 成交价 = 对侧 ask@+10s = flip_fill10s
  stake = 2 USDC → 股数 = 2/fill; 赢 → 每股兑 1 USDC; 输 → 本金归零

输出: 全量/训练/测试窗摘要, 阈值族对比, 逐日 P&L, 累计收益与最大回撤,
      明细表 data/trades_v3.csv。
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from featlib import DATA  # noqa: E402

STAKE = 2.0
TRIGGER_BID_MIN = 0.73
POST_END_DEFAULT = 0.66


def load():
    df = pd.read_pickle(BASE / "data" / "featmat.pkl")
    return df[df["flip_fill0s"].notna()]


def backtest(df, post_end_max, tb_min=TRIGGER_BID_MIN, stake=STAKE):
    """应用规则, 逐笔计算 P&L。返回 (明细 df, 汇总 dict)。"""
    m = df[df["trigger_bid"].gt(tb_min) & df["post_end"].notna() &
           df["post_end"].le(post_end_max) & df["flip_fill10s"].notna()]
    if m.empty:
        return m, None
    fill = m["flip_fill10s"]
    # 防御: 越界/缺失样本显式剔除, 不静默改价（clip 会扭曲统计）
    fill_ok = fill.between(0.05, 0.95)
    if not fill_ok.all():
        m = m[fill_ok]
        fill = m["flip_fill10s"]
    shares = stake / fill
    pnl = np.where(m["flip_won"].astype(bool), shares - stake, -stake)
    m = m.copy()
    m["fill"] = fill
    m["shares"] = shares
    m["pnl"] = pnl
    wr = m["flip_won"].mean()
    s = dict(
        n=len(m),
        wr=wr,
        fill=fill.mean(),
        ev_share=wr - fill.mean(),          # EV/股
        ev_trade=(m["pnl"]).mean(),         # EV/笔（2U）
        total_pnl=m["pnl"].sum(),
        day_ev_pos=m.groupby("date")["pnl"].mean().gt(0).sum(),
        day_n=m["date"].nunique(),
        per_day=len(m) / m["date"].nunique(),
        cum=m["pnl"].cumsum(),
    )
    s["max_dd"] = float((s["cum"].cummax() - s["cum"]).max())  # 最大回撤（USDC）
    return m, s


def wilson(n, k, z=1.96):
    p = k / n
    denom = 1 + z * z / n
    c = (p + z * z / (2 * n)) / denom
    m = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return c - m, c + m


def show(title, s):
    if s is None:
        print(f"{title}: 无信号")
        return
    s = dict(s)
    s.setdefault("max_dd", float((s["cum"].cummax() - s["cum"]).max()))
    lo, hi = wilson(s["n"], int(round(s["n"] * s["wr"])))
    print(f"\n{'=' * 66}")
    print(f"  {title}")
    print(f"{'=' * 66}")
    print(f"  信号数        {s['n']:>4d}  （约 {s['per_day']:.1f} 笔/天）")
    print(f"  胜率          {s['wr'] * 100:>5.1f}%   Wilson 95% CI [{lo * 100:.1f}%, {hi * 100:.1f}%]")
    print(f"  平均 fill     {s['fill']:.3f}")
    print(f"  EV/股         {s['ev_share']:+.4f}")
    print(f"  EV/笔（2U）   {s['ev_trade']:+.4f} USDC")
    print(f"  总 P&L        {s['total_pnl']:+.2f} USDC")
    print(f"  逐日盈利      {s['day_ev_pos']}/{s['day_n']} 天")
    print(f"  最大回撤      {s['max_dd']:.2f} USDC")


def main():
    df = load()
    dates = sorted(df["date"].unique())
    test2d = ("2026-08-29", "2026-08-30")
    print(f"数据: {DATA}  |  穿越观测 {len(df)}  |  {dates[0]} ~ {dates[-1]}")
    print(f"规则: trigger_bid > {TRIGGER_BID_MIN} 且 穿越侧 bid@+10s ≤ post_end 阈值 | "
          f"每笔 {STAKE} USDC | 决策 +10s | 结算 = TWAP 官方")

    # ── 主配置: post_end ≤ 0.66 ──
    m, s = backtest(df, POST_END_DEFAULT)
    show(f"主配置: post_end ≤ {POST_END_DEFAULT}（全量 14 天）", s)
    m.to_csv(BASE / "data" / "trades_v3.csv", index=False,
             columns=["date", "event_start", "side", "trigger_bid", "post_end",
                      "fill", "shares", "flip_won", "pnl", "cls"])

    # ── 样本内/样本外分解 ──
    if s:
        mtr = m[m["date"].le("2026-08-28")]
        mte = m[m["date"].isin(test2d)]
        mte4 = m[m["date"].ge("2026-08-27") & m["date"].le("2026-08-30")]
        show("  其中: train（08-18~08-28）", dict(n=len(mtr), wr=mtr["flip_won"].mean(),
             fill=mtr["fill"].mean(), ev_share=mtr["flip_won"].mean() - mtr["fill"].mean(),
             ev_trade=mtr["pnl"].mean(), total_pnl=mtr["pnl"].sum(),
             day_ev_pos=mtr.groupby("date")["pnl"].mean().gt(0).sum(),
             day_n=mtr["date"].nunique(), per_day=len(mtr) / mtr["date"].nunique(),
             cum=mtr["pnl"].cumsum()))
        show("  样本外: test2d（08-29~08-30）", dict(n=len(mte), wr=mte["flip_won"].mean(),
             fill=mte["fill"].mean(), ev_share=mte["flip_won"].mean() - mte["fill"].mean(),
             ev_trade=mte["pnl"].mean(), total_pnl=mte["pnl"].sum(),
             day_ev_pos=mte.groupby("date")["pnl"].mean().gt(0).sum(),
             day_n=mte["date"].nunique(), per_day=len(mte) / mte["date"].nunique(),
             cum=mte["pnl"].cumsum()))
        show("  样本外: 长窗 4d（08-27~08-30）", dict(n=len(mte4), wr=mte4["flip_won"].mean(),
             fill=mte4["fill"].mean(), ev_share=mte4["flip_won"].mean() - mte4["fill"].mean(),
             ev_trade=mte4["pnl"].mean(), total_pnl=mte4["pnl"].sum(),
             day_ev_pos=mte4.groupby("date")["pnl"].mean().gt(0).sum(),
             day_n=mte4["date"].nunique(), per_day=len(mte4) / mte4["date"].nunique(),
             cum=mte4["pnl"].cumsum()))

    # ── 阈值族对比（全量）──
    print(f"\n{'─' * 66}\n阈值族对比（全量 14 天, trigger_bid > {TRIGGER_BID_MIN}）\n{'─' * 66}")
    print(f"  {'post_end≤':<9s} {'n':>4s} {'WR':>6s} {'fill':>6s} {'EV/笔':>8s} "
          f"{'总P&L':>8s} {'逐日正':>6s} {'回撤':>6s}")
    for pe in (0.76, 0.74, 0.72, 0.70, 0.68, 0.66, 0.64, 0.62):
        _, s2 = backtest(df, pe)
        if s2 is None:
            continue
        print(f"  ≤{pe:<8.2f} {s2['n']:>4d} {s2['wr']*100:>5.1f}% {s2['fill']:.3f} "
              f"{s2['ev_trade']:>+8.4f} {s2['total_pnl']:>+8.2f} "
              f"{s2['day_ev_pos']:>4d}/{s2['day_n']:<2d} {s2['max_dd']:>6.2f}")

    # ── 逐日明细（主配置）──
    if s is not None:
        print(f"\n{'─' * 66}\n逐日明细（post_end ≤ {POST_END_DEFAULT}）\n{'─' * 66}")
        print(f"  {'日期':<12s} {'信号':>4s} {'WR':>6s} {'fill':>6s} {'P&L':>8s} {'累计':>8s}")
        cum = 0.0
        for d in sorted(m["date"].unique()):
            g = m[m["date"] == d]
            cum += g["pnl"].sum()
            print(f"  {d:<12s} {len(g):>4d} {g['flip_won'].mean()*100:>5.1f}% "
                  f"{g['fill'].mean():.3f} {g['pnl'].sum():>+8.2f} {cum:>+8.2f}")


if __name__ == "__main__":
    main()
