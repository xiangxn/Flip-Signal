#!/usr/bin/env python3
"""
尾盘扫尾策略回测 — 基于 7 个特征的组合规则。

决策逻辑：
  下注方向：price > open → 赌 YES，price < open → 赌 NO

  必须条件：
    A. distance_to_strike > DIST_THRESHOLD     （趋势已走出来）

  否决条件（任一触发则不下注）：
    V_vol. volatility_expansion >= 1.0          （波动达到/超过历史水平）
    F.     distance_to_strike < DIST_LOW AND direction_persistence < PERSIST_LOW
           （距离小 + 方向模糊）

  加分条件（至少满足 MIN_BONUS 个，3选N）：
    C. safety_ratio > SAFETY_THRESHOLD         （趋势不是噪声）
    D. reversal_capacity < REVERSAL_THRESHOLD  （逆转风险可控）
    G. imbalance_trend > 0                     （盘口深度支撑方向）

Usage:
    python backtest.py --data ../data/lab/
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from features import FEATURES
from loader import load_events, summary

# ── 策略阈值 ──────────────────────────────────────────────
DIST_THRESHOLD = 0.00016      # distance_to_strike 必须 > 此值（≈Q3）
SAFETY_THRESHOLD = 1.8        # safety_ratio 加分（≈Q3）
REVERSAL_THRESHOLD = 0.87     # reversal_capacity 加分（≈Q3）
DIST_LOW = 0.00010            # "距离还很小" 的阈值（≈Q1-Q2 边界）
PERSIST_LOW = 0.15            # "持续性很低" 的阈值（≈Q1-Q2 边界）
MIN_BONUS = 2                 # 加分条件至少满足几个（3 个中选 2 个）
CHECKPOINTS = [60, 55, 50, 45, 40, 35, 30, 25, 20, 15, 10, 5]  # 尾盘决策时间点 (每5秒)
TOLERANCE = 3                 # 时间匹配容差（秒）


def compute_features(df: pd.DataFrame) -> dict[str, pd.Series]:
    """计算全部 7 个特征，返回 {name: series} 字典。"""
    cache = {}
    for f in FEATURES:
        cache[f.name] = f.compute(df)
    return cache


def backtest(df: pd.DataFrame, fv: dict[str, pd.Series]) -> pd.DataFrame:
    """在尾盘时间点应用策略规则，返回每条决策记录。

    Returns
    -------
    pd.DataFrame with columns:
      condition_id, remaining_sec, bet_direction, entry_price,
      won, pnl, passed (bool), reject_reason, plus each feature value.
    """
    # 基础数据
    distance = (df["price"] - df["open"]) / df["open"]
    bet_yes = distance > 0
    bet_no = distance < 0
    valid_dir = bet_yes | bet_no

    won = (bet_yes & (df["outcome"] == 0)) | (bet_no & (df["outcome"] == 1))

    # 入场价：赌 YES 用 yes_price，赌 NO 用 no_price
    entry_price = np.where(bet_yes, df["yes_price"], df["no_price"])

    # 盈亏
    pnl = np.where(won, 1.0 - entry_price, -entry_price)

    # ── 策略条件 ──
    # 必须条件
    A = fv["distance_to_strike"] > DIST_THRESHOLD        # 必须：趋势已走出来

    # 加分条件 (3个)
    C = fv["safety_ratio"] > SAFETY_THRESHOLD            # 加分：趋势显著
    D = fv["reversal_capacity"] < REVERSAL_THRESHOLD     # 加分：逆转可控
    G = fv["imbalance_trend"] > 0                        # 加分：盘口支撑

    # 否决条件 (2个)
    V_vol = fv["volatility_expansion"] >= 1.0            # 否决：波动达到/超过历史水平
    F = (fv["distance_to_strike"] < DIST_LOW) & \
        (fv["direction_persistence"] < PERSIST_LOW)      # 否决：方向模糊

    must_pass = A
    bonus_count = C.astype(int) + D.astype(int) + G.astype(int)
    bonus_pass = bonus_count >= MIN_BONUS
    veto = F | V_vol

    passed = must_pass & bonus_pass & ~veto & valid_dir

    # ── 收集尾盘时间点 ──
    rows = []
    for cp in CHECKPOINTS:
        near = (
            (df["remaining_sec"] >= cp - TOLERANCE)
            & (df["remaining_sec"] <= cp + TOLERANCE)
            & valid_dir
        )
        subset = df.loc[near]
        if len(subset) == 0:
            continue

        idx = subset.index
        for i in idx:
            reason = ""
            if not must_pass.loc[i]:
                reason = "必须条件未满足"
            elif V_vol.loc[i]:
                reason = "否决:波动异常"
            elif F.loc[i]:
                reason = "否决:方向模糊"
            elif not bonus_pass.loc[i]:
                reason = "加分条件不足"
            elif not valid_dir.loc[i]:
                reason = "价格未偏离开盘价"

            rows.append({
                "condition_id": df.loc[i, "condition_id"],
                "remaining_sec": cp,
                "bet_direction": "YES" if distance.loc[i] > 0 else "NO",
                "entry_price": round(float(entry_price[i]), 4),
                "won": int(won.loc[i]),
                "pnl": round(float(pnl[i]), 4),
                "passed": bool(passed.loc[i]),
                "reject_reason": reason if not passed.loc[i] else "",
                # 特征值
                "distance_to_strike": round(float(fv["distance_to_strike"].loc[i]), 8),
                "safety_ratio": round(float(fv["safety_ratio"].loc[i]), 4),
                "reversal_capacity": round(float(fv["reversal_capacity"].loc[i]), 4),
                "volatility_expansion": round(float(fv["volatility_expansion"].loc[i]), 4),
                "direction_persistence": round(float(fv["direction_persistence"].loc[i]), 4),
                "cumulative_buy_pct": round(float(fv["cumulative_buy_pct"].loc[i]), 4),
                "imbalance_trend": round(float(fv["imbalance_trend"].loc[i]), 6),
                # 单独的条件标记
                "cond_A_dist": bool(A.loc[i]),
                "cond_C_safety": bool(C.loc[i]),
                "cond_D_reversal": bool(D.loc[i]),
                "cond_G_imb": bool(G.loc[i]),
                "cond_V_vol": bool(V_vol.loc[i]),
                "cond_F_veto": bool(F.loc[i]),
            })

    return pd.DataFrame(rows)


def analyze_results(trades: pd.DataFrame) -> str:
    """生成回测分析报告。"""
    total_snaps = len(trades)
    passed = trades[trades["passed"]]
    rejected = trades[~trades["passed"]]

    n_pass = len(passed)
    n_reject = len(rejected)

    lines = [
        "# 尾盘扫尾策略回测报告",
        "",
        f"**尾盘决策点总数**: {total_snaps}（每个事件 {len(CHECKPOINTS)} 个时间点）",
        "",
        "---",
        "",
        "## 策略参数",
        "",
        f"| 参数 | 值 | 说明 |",
        f"|------|-----|------|",
        f"| distance_to_strike > | {DIST_THRESHOLD} | 趋势已走出来（必须） |",
        f"| safety_ratio > | {SAFETY_THRESHOLD} | 趋势不是噪声（加分） |",
        f"| reversal_capacity < | {REVERSAL_THRESHOLD} | 逆转风险可控（加分） |",
        f"| volatility_expansion >= | 1.0 | 波动达到历史水平（否决） |",
        f"| imbalance_trend > | 0 | 盘口深度支撑方向（加分） |",
        f"| 否决: distance < {DIST_LOW} AND persistence < {PERSIST_LOW} | — | 方向模糊（否决） |",
        f"| 加分条件最低满足 | {MIN_BONUS}/3 | — |",
        "",
        "---",
        "",
        "## 整体结果",
        "",
    ]

    # Overall stats
    baseline_wr = trades["won"].mean()
    pass_wr = passed["won"].mean() if n_pass > 0 else 0
    reject_wr = rejected["won"].mean() if n_reject > 0 else 0
    pass_avg_price = passed["entry_price"].mean() if n_pass > 0 else 0
    pass_avg_pnl = passed["pnl"].mean() if n_pass > 0 else 0

    lines.extend([
        f"| 指标 | 全部快照 | 策略通过 | 策略拒绝 |",
        f"|------|---------|---------|---------|",
        f"| 决策点数 | {total_snaps} | {n_pass} | {n_reject} |",
        f"| 胜率 | {baseline_wr:.2%} | {pass_wr:.2%} | {reject_wr:.2%} |",
        f"| 平均入场价 | {trades['entry_price'].mean():.4f} | {pass_avg_price:.4f} | {rejected['entry_price'].mean():.4f} |",
        f"| 平均盈亏 | {trades['pnl'].mean():+.4f} | {pass_avg_pnl:+.4f} | {rejected['pnl'].mean():+.4f} |",
        "",
    ])

    # By checkpoint
    lines.extend([
        "## 各时间点结果",
        "",
        "| 剩余时间 | 全部胜率 | 通过数 | 通过胜率 | 拒绝胜率 | 通过均价 | 通过平均盈亏 |",
        "|---------|---------|--------|---------|---------|---------|------------|",
    ])
    for cp in CHECKPOINTS:
        cp_trades = trades[trades["remaining_sec"] == cp]
        cp_pass = cp_trades[cp_trades["passed"]]
        cp_reject = cp_trades[~cp_trades["passed"]]
        pw = f"{cp_pass['won'].mean():.2%}" if len(cp_pass) > 0 else "—"
        rw = f"{cp_reject['won'].mean():.2%}" if len(cp_reject) > 0 else "—"
        pp = f"{cp_pass['entry_price'].mean():.4f}" if len(cp_pass) > 0 else "—"
        pnl = f"{cp_pass['pnl'].mean():+.4f}" if len(cp_pass) > 0 else "—"
        lines.append(
            f"| {cp}s | {cp_trades['won'].mean():.2%} | {len(cp_pass)} | "
            f"{pw} | {rw} | {pp} | {pnl} |"
        )
    lines.append("")

    # ── 特征有效性分析 ──
    lines.extend([
        "---",
        "",
        "## 7 个特征有效性分析",
        "",
        "_每个特征对策略的贡献：对比「特征条件满足」vs「不满足」时的胜率差异。_",
        "",
        "| # | 特征 | 条件 | 满足时胜率 | 不满足时胜率 | 胜率差 | 满足占比 | 有效性 |",
        "|---|------|------|----------|------------|--------|---------|--------|",
    ])

    feature_checks = [
        ("distance_to_strike", f" > {DIST_THRESHOLD}（必须）", "cond_A_dist"),
        ("safety_ratio", f" > {SAFETY_THRESHOLD}（加分）", "cond_C_safety"),
        ("reversal_capacity", f" < {REVERSAL_THRESHOLD}（加分）", "cond_D_reversal"),
        ("imbalance_trend", " > 0（加分）", "cond_G_imb"),
        ("volatility_expansion", " >= 1.0（否决）", "cond_V_vol"),
        ("direction_persistence",
         f"否决(dist<{DIST_LOW} & pers<{PERSIST_LOW})", "cond_F_veto"),
    ]

    for i, (name, cond_desc, col) in enumerate(feature_checks, 1):
        met = trades[trades[col]]
        not_met = trades[~trades[col]]
        wr_met = met["won"].mean() if len(met) > 0 else 0
        wr_not = not_met["won"].mean() if len(not_met) > 0 else 0
        diff = wr_met - wr_not
        pct = len(met) / max(len(trades), 1)

        if abs(diff) > 0.03:
            verdict = "✅ 有效" if diff > 0 else "⚠️ 反向有效"
        elif abs(diff) > 0.01:
            verdict = "🟡 弱有效"
        else:
            verdict = "❌ 无效"

        lines.append(
            f"| {i} | {name} | {cond_desc} | {wr_met:.2%} | {wr_not:.2%} | "
            f"{diff:+.2%} | {pct:.0%} | {verdict} |"
        )
    lines.append("")

    # ── 拒绝原因分布 ──
    lines.extend([
        "---",
        "",
        "## 拒绝原因分布",
        "",
        "| 原因 | 数量 | 占比 | 这些被拒绝的胜率 |",
        "|------|------|------|----------------|",
    ])
    for reason in rejected["reject_reason"].unique():
        subset = rejected[rejected["reject_reason"] == reason]
        lines.append(
            f"| {reason} | {len(subset)} | {len(subset)/max(n_reject,1):.0%} | "
            f"{subset['won'].mean():.2%} |"
        )
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="尾盘扫尾策略回测")
    parser.add_argument("--data", default="../data/",
                        help="JSONL 数据目录")
    parser.add_argument("--output", default="../reports/",
                        help="报告输出目录")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    data_dir = (script_dir / args.data).resolve()
    output_dir = (script_dir / args.output).resolve()

    print(f"数据目录: {data_dir}")
    print("加载数据...")
    try:
        df = load_events(str(data_dir))
    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)

    s = summary(df)
    print(f"  事件: {s['events']}, 快照: {s['snapshots']:,}")

    print("计算 7 个特征...")
    fv = compute_features(df)
    for name in fv:
        print(f"  {name}: {fv[name].notna().sum():,} 有效值")

    print("运行策略回测...")
    trades = backtest(df, fv)
    print(f"  尾盘决策点: {len(trades)}")
    print(f"  策略通过: {trades['passed'].sum()}")

    print("生成报告...")
    report = analyze_results(trades)

    output_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = output_dir / f"backtest_report_{ts}.md"
    report_path.write_text(report, encoding="utf-8")

    # Also save trades CSV for detailed inspection
    csv_path = output_dir / f"backtest_trades_{ts}.csv"
    trades.to_csv(str(csv_path), index=False)

    print(f"\n报告: {report_path}")
    print(f"交易明细: {csv_path}")


if __name__ == "__main__":
    main()
