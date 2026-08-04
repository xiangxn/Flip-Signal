#!/usr/bin/env python3
"""
尾盘扫尾策略回测 — 每个 Event 最多一笔交易。

核心原则：每个 5 分钟 event 只下一注（或不下）。在尾盘窗口内扫描
不同入场时间，找到胜率/盈亏最优的入场时机。

决策逻辑：
  下注方向：price > open → 赌 YES，price < open → 赌 NO

  必须条件：
    A. distance_to_strike > DIST_THRESHOLD     （趋势已走出来）

  否决条件（任一触发则不下注）：
    V_vol. volatility_expansion >= 1.0          （波动达到/超过历史水平）
    V_rev. reversal_capacity >= 2.0             （逆转风险过大，市场能轻易翻转当前gap）
    V_dir. direction_persistence < 0.3          （tick方向不一致，趋势不流畅）
    F.     distance_to_strike < DIST_LOW AND direction_persistence < PERSIST_LOW
           （距离小 + 方向模糊）

  加分条件（至少满足 MIN_BONUS 个，3选N）：
    C. safety_ratio > SAFETY_THRESHOLD         （趋势不是噪声）
    D. reversal_capacity < REVERSAL_THRESHOLD  （逆转风险可控）
    G. imbalance_trend > 0                     （盘口深度支撑方向）

Usage:
    python backtest.py --data ../data/ --output ../reports/
"""

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from features import FEATURES
from loader import load_events, summary

# ── 策略阈值 ──────────────────────────────────────────────
DIST_THRESHOLD = 0.00030      # distance_to_strike 必须 > 此值（≈中位数，排除弱趋势）
SAFETY_THRESHOLD = 1.8        # safety_ratio 加分
REVERSAL_THRESHOLD = 0.87     # reversal_capacity 加分
REVERSAL_VETO = 2.0           # reversal_capacity >= 此值 → 否决（逆转风险过大）
PERSIST_VETO = 0.25            # direction_persistence < 此值 → 否决（tick方向不一致）
DIST_LOW = 0.00010            # "距离还很小" 的阈值
PERSIST_LOW = 0.15            # "持续性很低" 的阈值
MIN_BONUS = 2                 # 加分条件至少满足几个（3 选 N）
ENTRY_TIMES = [120, 90, 60, 55, 50, 45, 40, 35, 30, 25, 20, 15, 10, 5]  # 入场时间点（前移到120s）
TOLERANCE = 3                 # 时间匹配容差（秒）
MIN_DEPTH = 5.0               # 下注方向最小盘口深度（USDC）
DIST_RATIO = 2.0              # distance / typical_5min_range > 此值 → 方向锁定信号


def compute_features(df: pd.DataFrame) -> dict[str, pd.Series]:
    """计算全部特征，返回 {name: series} 字典。"""
    cache = {}
    for f in FEATURES:
        cache[f.name] = f.compute(df)
    return cache


@dataclass
class EntryStats:
    """单个入场时间的回测统计。"""
    entry_sec: int
    events_total: int = 0
    events_traded: int = 0
    events_passed: int = 0       # 策略通过
    depth_rejected: int = 0      # 策略通过但盘口不足
    win_rate: float = 0.0
    avg_price: float = 0.0
    avg_pnl: float = 0.0
    total_pnl: float = 0.0
    reject_reasons: dict[str, int] = field(default_factory=dict)


def backtest_at_entry(
    df: pd.DataFrame,
    fv: dict[str, pd.Series],
    entry_sec: int,
    *,
    dist_threshold: float = DIST_THRESHOLD,
    safety_threshold: float = SAFETY_THRESHOLD,
    reversal_threshold: float = REVERSAL_THRESHOLD,
    reversal_veto: float = REVERSAL_VETO,
    persist_veto: float = PERSIST_VETO,
    dist_low: float = DIST_LOW,
    persist_low: float = PERSIST_LOW,
    min_bonus: int = MIN_BONUS,
    min_depth: float = MIN_DEPTH,
    tolerance: int = TOLERANCE,
) -> tuple[pd.DataFrame, EntryStats]:
    """对单一入场时间做逐 event 回测。

    每个 event 只取最接近 entry_sec 的一个快照，评估策略条件。

    Returns
    -------
    (trades_df, stats)
        trades_df: 每个 event 一行（通过或拒绝）
        stats: 汇总统计
    """
    events = df["condition_id"].unique()
    stats = EntryStats(entry_sec=entry_sec, events_total=len(events))

    # 预计算全局数组
    distance = (df["price"] - df["open"]) / df["open"]
    bet_yes = distance > 0
    bet_no = distance < 0
    valid_dir = bet_yes | bet_no
    won = (bet_yes & (df["outcome"] == 0)) | (bet_no & (df["outcome"] == 1))
    entry_price = np.where(bet_yes, df["yes_price"], df["no_price"])
    pnl = np.where(won, 1.0 - entry_price, -entry_price)

    # 策略条件
    # 必须条件
    A = fv["distance_to_strike"] > dist_threshold        # 必须：趋势已走出来

    # 加分条件 (3个)
    C = fv["safety_ratio"] > safety_threshold            # 加分：趋势显著
    D = fv["reversal_capacity"] < reversal_threshold     # 加分：逆转可控
    G = fv["imbalance_trend"] > 0                        # 加分：盘口支撑

    # 否决条件 (4个)
    V_vol = fv["volatility_expansion"] >= 1.0            # 否决：波动达到历史水平
    V_rev = fv["reversal_capacity"] >= reversal_veto     # 否决：逆转风险过大
    V_dir = fv["direction_persistence"] < persist_veto   # 否决：tick方向不一致
    F = (fv["distance_to_strike"] < dist_low) & \
        (fv["direction_persistence"] < persist_low)      # 否决：方向模糊

    must_pass = A
    bonus_count = C.astype(int) + D.astype(int) + G.astype(int)
    bonus_pass = bonus_count >= min_bonus
    veto = F | V_vol | V_rev | V_dir

    passed = must_pass & bonus_pass & ~veto & valid_dir

    rows = []
    for cid in events:
        event_mask = df["condition_id"] == cid

        # 找最接近 entry_sec 的快照（在容差范围内）
        remaining = df.loc[event_mask, "remaining_sec"]
        dist_to_target = (remaining - entry_sec).abs()
        in_range = dist_to_target <= tolerance

        if not in_range.any():
            continue  # 该 event 在目标时间附近没有快照

        best_local_idx = dist_to_target[in_range].idxmin()
        i = best_local_idx  # df 的全局 index

        # 判定拒绝原因
        reason = ""
        strategy_pass = bool(passed.loc[i])
        if not valid_dir.loc[i]:
            reason = "价格未偏离"
        elif not must_pass.loc[i]:
            reason = "必须条件未满足"
        elif V_vol.loc[i]:
            reason = "否决:波动异常"
        elif V_rev.loc[i]:
            reason = "否决:逆转风险"
        elif V_dir.loc[i]:
            reason = "否决:方向不一致"
        elif F.loc[i]:
            reason = "否决:方向模糊"
        elif not bonus_pass.loc[i]:
            reason = "加分条件不足"

        # 策略通过后，检查盘口深度
        trade_executed = strategy_pass
        if strategy_pass:
            need_depth = df.loc[i, "ask_depth"] if distance.loc[i] > 0 else df.loc[i, "bid_depth"]
            if need_depth < min_depth:
                reason = "盘口不足"
                trade_executed = False

        rows.append({
            "condition_id": cid,
            "entry_sec": entry_sec,
            "remaining_sec": int(remaining.loc[best_local_idx]),
            "bet_direction": "YES" if distance.loc[i] > 0 else "NO",
            "entry_price": round(float(entry_price[i]), 4),
            "won": int(won.loc[i]),
            "pnl": round(float(pnl[i]), 4),
            "passed": trade_executed,
            "reject_reason": reason,
            "distance_to_strike": round(float(fv["distance_to_strike"].loc[i]), 8),
            "safety_ratio": round(float(fv["safety_ratio"].loc[i]), 4),
            "reversal_capacity": round(float(fv["reversal_capacity"].loc[i]), 4),
            "volatility_expansion": round(float(fv["volatility_expansion"].loc[i]), 4),
            "direction_persistence": round(float(fv["direction_persistence"].loc[i]), 4),
            "imbalance_trend": round(float(fv["imbalance_trend"].loc[i]), 6),
        })

    trades_df = pd.DataFrame(rows)
    if len(trades_df) == 0:
        return trades_df, stats

    passed_df = trades_df[trades_df["passed"]]
    n_pass = len(passed_df)
    stats.events_passed = (trades_df["reject_reason"] == "").sum()  # 策略通过（不含盘口）
    stats.depth_rejected = (trades_df["reject_reason"] == "盘口不足").sum()
    stats.events_traded = n_pass

    if n_pass > 0:
        stats.win_rate = float(passed_df["won"].mean())
        stats.avg_price = float(passed_df["entry_price"].mean())
        stats.avg_pnl = float(passed_df["pnl"].mean())
        stats.total_pnl = float(passed_df["pnl"].sum())

    # 拒绝原因统计
    rejected = trades_df[~trades_df["passed"]]
    for reason in rejected["reject_reason"]:
        if reason:
            stats.reject_reasons[reason] = stats.reject_reasons.get(reason, 0) + 1

    return trades_df, stats


def scan_entry_times(
    df: pd.DataFrame,
    fv: dict[str, pd.Series],
    **params,
) -> list[EntryStats]:
    """扫描所有入场时间，返回每个时间的统计。"""
    results = []
    for entry_sec in ENTRY_TIMES:
        _, stats = backtest_at_entry(df, fv, entry_sec, **params)
        results.append(stats)
    return results


def backtest_earliest(
    df: pd.DataFrame,
    fv: dict[str, pd.Series],
    **params,
) -> tuple[pd.DataFrame, dict]:
    """每个 event 从开盘起扫描，在条件首次满足时入场。

    扫描顺序：从 remaining_sec 最大（开盘）到最小（收盘），
    找到第一个策略通过 + 盘口足够的快照。

    Returns
    -------
    (trades_df, summary_dict)
    """
    events = df["condition_id"].unique()
    min_depth = params.get("min_depth", MIN_DEPTH)
    dist_th = params.get("dist_threshold", DIST_THRESHOLD)
    safety_th = params.get("safety_threshold", SAFETY_THRESHOLD)
    reversal_th = params.get("reversal_threshold", REVERSAL_THRESHOLD)
    reversal_veto = params.get("reversal_veto", REVERSAL_VETO)
    persist_veto = params.get("persist_veto", PERSIST_VETO)
    min_bonus = params.get("min_bonus", MIN_BONUS)
    dist_ratio = params.get("dist_ratio", DIST_RATIO)

    # 从所有 event 计算典型 5 分钟波幅（中位数）
    all_ranges = []
    for cid in events:
        ev = df[df["condition_id"] == cid]
        rng = abs(ev.iloc[-1]["close_price"] - ev.iloc[0]["open"]) / ev.iloc[0]["open"]
        all_ranges.append(rng)
    typical_range = np.median(all_ranges)

    rows = []
    for cid in events:
        ev = df[df["condition_id"] == cid].sort_values("remaining_sec", ascending=False)
        outcome = ev.iloc[0]["outcome"]
        actual_dir = 1 if outcome == 0 else -1

        for idx, row in ev.iterrows():
            price, opn = row["price"], row["open"]
            if price > opn:
                d = 1
            elif price < opn:
                d = -1
            else:
                continue

            # 方向锁定信号: 当前涨跌幅 ≥ N倍 典型5分钟波幅
            dist_v = abs(price - opn) / opn
            lock_signal = dist_v >= dist_ratio * typical_range

            # 入场决策: 两套规则
            # 规则1: 方向锁定信号（涨跌幅远超典型5分钟波幅）→ 不限时间，立即入场
            # 规则2: 标准尾盘策略 → 仅最后 15s 使用
            remaining = row["remaining_sec"]

            if lock_signal:
                enter = True
            elif remaining <= 15:
                A = fv["distance_to_strike"].loc[idx] > dist_th
                C = fv["safety_ratio"].loc[idx] > safety_th
                D_ok = fv["reversal_capacity"].loc[idx] < reversal_th
                G = fv["imbalance_trend"].loc[idx] > 0
                V_vol = fv["volatility_expansion"].loc[idx] >= 1.0
                V_rev = fv["reversal_capacity"].loc[idx] >= reversal_veto
                V_dir = fv["direction_persistence"].loc[idx] < persist_veto
                V_f = ((fv["distance_to_strike"].loc[idx] < 0.00010) and
                       (fv["direction_persistence"].loc[idx] < 0.15))
                bonus_ok = (int(C) + int(D_ok) + int(G)) >= min_bonus
                enter = (A and bonus_ok and not V_vol and not V_rev and not V_dir and not V_f)
            else:
                enter = False

            if not enter:
                continue

            # 盘口检查
            need_d = row["ask_depth"] if d == 1 else row["bid_depth"]
            if need_d < min_depth:
                continue

            # 入场！
            entry_p = row["yes_price"] if d == 1 else row["no_price"]
            won = (d == actual_dir)
            pnl = (1.0 - entry_p) if won else -entry_p

            rows.append({
                "condition_id": cid,
                "entry_sec": int(row["remaining_sec"]),
                "bet_direction": "YES" if d == 1 else "NO",
                "entry_price": round(float(entry_p), 4),
                "won": int(won),
                "pnl": round(float(pnl), 4),
                "distance_to_strike": round(float(fv["distance_to_strike"].loc[idx]), 8),
                "safety_ratio": round(float(fv["safety_ratio"].loc[idx]), 4),
                "reversal_capacity": round(float(fv["reversal_capacity"].loc[idx]), 4),
                "volatility_expansion": round(float(fv["volatility_expansion"].loc[idx]), 4),
                "direction_persistence": round(float(fv["direction_persistence"].loc[idx]), 4),
                "imbalance_trend": round(float(fv["imbalance_trend"].loc[idx]), 6),
            })
            break  # 每个 event 只取最早的一次

    trades_df = pd.DataFrame(rows)
    if len(trades_df) == 0:
        return trades_df, {"trades": 0, "total_pnl": 0}

    won_trades = trades_df[trades_df["won"] == 1]
    summary = {
        "trades": len(trades_df),
        "win_rate": float(trades_df["won"].mean()),
        "avg_price": float(trades_df["entry_price"].mean()),
        "avg_pnl": float(trades_df["pnl"].mean()),
        "total_pnl": float(trades_df["pnl"].sum()),
        "won_count": len(won_trades),
        "lost_count": len(trades_df) - len(won_trades),
        # 入场时间分布
        "entry_dist": {
            "≥120s": int((trades_df["entry_sec"] >= 120).sum()),
            "90-119s": int(((trades_df["entry_sec"] >= 90) & (trades_df["entry_sec"] < 120)).sum()),
            "60-89s": int(((trades_df["entry_sec"] >= 60) & (trades_df["entry_sec"] < 90)).sum()),
            "30-59s": int(((trades_df["entry_sec"] >= 30) & (trades_df["entry_sec"] < 60)).sum()),
            "10-29s": int(((trades_df["entry_sec"] >= 10) & (trades_df["entry_sec"] < 30)).sum()),
            "5-9s": int(((trades_df["entry_sec"] >= 5) & (trades_df["entry_sec"] < 10)).sum()),
        },
    }
    return trades_df, summary


def compute_reversal_rates(df: pd.DataFrame) -> dict[int, dict]:
    """计算各时间点的市场价格翻转率。

    翻转 = 该时刻的 price vs open 方向 ≠ 最终 outcome 方向。
    这是尾盘策略的天然风险天花板——即使完美预测，也会被翻转吃掉。

    Returns
    -------
    dict: {entry_sec: {total, flat, reversed, reversal_pct}}
    """
    results = {}
    events = df["condition_id"].unique()

    for sec in ENTRY_TIMES:
        total = 0
        flat = 0
        reversed_cnt = 0

        for cid in events:
            ev = df[df["condition_id"] == cid]
            dist = (ev["remaining_sec"] - sec).abs()
            best = ev.loc[dist.idxmin()]

            price = best["price"]
            opn = best["open"]
            outcome = best["outcome"]

            if price > opn:
                direction = "UP"
            elif price < opn:
                direction = "DOWN"
            else:
                flat += 1
                total += 1
                continue

            actual = "UP" if outcome == 0 else "DOWN"
            if direction != actual:
                reversed_cnt += 1
            total += 1

        valid = total - flat
        results[sec] = {
            "total": total,
            "flat": flat,
            "reversed": reversed_cnt,
            "reversal_pct": reversed_cnt / max(valid, 1),
        }

    return results


@dataclass
class LockStats:
    """事件方向锁定统计。"""
    never_flipped: int = 0       # 从未翻转
    locked_by_180s: int = 0      # 180s前锁定
    locked_by_120s: int = 0      # 120s前锁定
    locked_by_60s: int = 0       # 60s前锁定
    locked_by_15s: int = 0       # 15s前锁定
    total: int = 0


def compute_lock_stats(df: pd.DataFrame) -> LockStats:
    """分析每个 event 价格方向最后一次翻转的时间。"""
    events = df["condition_id"].unique()
    stats = LockStats(total=len(events))

    for cid in events:
        ev = df[df["condition_id"] == cid].sort_values("remaining_sec", ascending=False)
        outcome = ev.iloc[0]["outcome"]
        actual_dir = 1 if outcome == 0 else -1

        last_flip = None
        for _, row in ev.iterrows():
            price, opn = row["price"], row["open"]
            if price > opn:
                d = 1
            elif price < opn:
                d = -1
            else:
                continue
            if d != actual_dir:
                last_flip = row["remaining_sec"]

        if last_flip is None:
            stats.never_flipped += 1
        elif last_flip <= 180:
            stats.locked_by_180s += 1
        if last_flip is not None and last_flip <= 120:
            stats.locked_by_120s += 1
        if last_flip is not None and last_flip <= 60:
            stats.locked_by_60s += 1
        if last_flip is not None and last_flip <= 15:
            stats.locked_by_15s += 1

    return stats


def generate_report(all_stats: list[EntryStats], reversals: dict[int, dict],
                    lock_stats: LockStats = None,
                    earliest_summary: dict = None) -> str:
    """生成中文回测报告。"""
    lines = [
        "# 尾盘扫尾策略回测报告",
        "",
        f"**生成时间**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "> 每个 5 分钟 Event 最多一笔交易（在目标入场时间点评估条件）。",
        "",
        "---",
        "",
        "## 策略规则",
        "",
        "| 类型 | 条件 | 阈值 | 说明 |",
        "|------|------|------|------|",
        "| 必须 | distance_to_strike > | "
        f"{DIST_THRESHOLD} | 趋势已走出来 |",
        "| 否决 | volatility_expansion >= | 1.0 | 波动达到/超过历史水平 |",
        "| 否决 | reversal_capacity >= | "
        f"{REVERSAL_VETO} | 逆转风险过大（市场振幅超当前gap） |",
        "| 否决 | direction_persistence < | "
        f"{PERSIST_VETO} | tick方向不一致（趋势不流畅） |",
        "| 否决 | distance_to_strike < "
        f"{DIST_LOW} AND direction_persistence < {PERSIST_LOW} | — | 方向模糊 |",
        f"| 加分 | safety_ratio > | {SAFETY_THRESHOLD} | 趋势不是噪声 |",
        f"| 加分 | reversal_capacity < | {REVERSAL_THRESHOLD} | 逆转风险可控 |",
        "| 加分 | imbalance_trend > | 0 | 盘口支撑方向 |",
        f"| — | 加分最低满足 | {MIN_BONUS}/3 | — |",
        f"| 执行 | 方向侧盘口深度 ≥ | {MIN_DEPTH} USDC | 确保可成交 |",
        "",
        "---",
        "",
        "## 入场时间扫描",
        "",
        "_在尾盘各时间点入场，每个 Event 最多一笔交易。_",
        "",
        "| 入场时间 | 总Event | 策略通过 | 盘口不足 | 实际成交 | 胜率 | 均价 | 平均盈亏 | 总盈亏 |",
        "|---------|--------|---------|---------|---------|------|------|---------|--------|",
    ]

    # Find best entry time by win_rate (primary) and total_pnl (secondary)
    best_wr = max(all_stats, key=lambda s: (s.win_rate, s.total_pnl))
    best_pnl = max(all_stats, key=lambda s: (s.total_pnl, s.win_rate))

    for s in all_stats:
        marker_wr = " ⭐" if s is best_wr else ""
        marker_pnl = " 💰" if s is best_pnl else ""
        lines.append(
            f"| {s.entry_sec}s | {s.events_total} | {s.events_passed} | "
            f"{s.depth_rejected} | {s.events_traded} | "
            f"{s.win_rate:.1%}{marker_wr} | "
            f"{s.avg_price:.4f} | {s.avg_pnl:+.4f}{marker_pnl} | "
            f"{s.total_pnl:+.2f} |"
        )

    lines.append("")
    lines.append("⭐ = 最佳胜率  💰 = 最佳总盈亏")
    lines.append("")

    # ── 价格翻转分析 ──
    lines.extend([
        "",
        "---",
        "",
        "## 市场价格翻转率",
        "",
        "_各时间点 price vs open 方向与最终 outcome 不一致的比例。"
        "这是尾盘策略的天然风险天花板。_",
        "",
        "| 剩余时间 | 总Event | 持平 | 翻转数 | 翻转率 | 策略胜率 | 过滤提升 |",
        "|---------|--------|------|--------|--------|---------|---------|",
    ])
    for s in all_stats:
        rev = reversals.get(s.entry_sec, {})
        rev_pct = rev.get("reversal_pct", 0)
        rev_n = rev.get("reversed", 0)
        rev_flat = rev.get("flat", 0)
        # 理论最高胜率 = 1 - 翻转率
        max_possible = 1 - rev_pct
        # 策略实际胜率 vs 理论最高
        boost = s.win_rate - max_possible if s.events_traded > 0 else 0
        lines.append(
            f"| {s.entry_sec}s | {rev.get('total', 0)} | {rev_flat} | "
            f"{rev_n} | {rev_pct:.1%} | "
            f"{s.win_rate:.1%} | {boost:+.1%} |"
        )
    lines.append("")

    # 解释
    best_rev = reversals.get(best_wr.entry_sec, {})
    lines.extend([
        "",
        f"**关键发现**: 即使在 5s 剩余，仍有 {best_rev.get('reversal_pct', 0):.1%} 的 Event 会翻转。",
        f"策略通过过滤条件（必须+否决+加分），将胜率从理论最高 {1 - best_rev.get('reversal_pct', 0):.1%} "
        f"提升到了 {best_wr.win_rate:.1%}。",
        "",
    ])

    # ── 方向锁定分析 ──
    if lock_stats:
        ls = lock_stats
        total = ls.total
        lines.extend([
            "",
            "---",
            "",
            "## 事件方向锁定分析",
            "",
            "_统计每个 Event 的价格方向最后一次翻转的时间。"
            "锁定越早，越可以提前入场拿更好的价格。_",
            "",
            "| 锁定时间 | 事件数 | 占比 | 说明 |",
            "|---------|--------|------|------|",
            f"| 从未翻转 | {ls.never_flipped} | {ls.never_flipped/total:.1%} | 从第一秒方向就确定了 |",
            f"| ≤180s 锁定 | {ls.locked_by_180s + ls.never_flipped} | "
            f"{(ls.locked_by_180s + ls.never_flipped)/total:.1%} | "
            f"3分钟内方向锁定 |",
            f"| ≤120s 锁定 | {ls.locked_by_120s + ls.never_flipped} | "
            f"{(ls.locked_by_120s + ls.never_flipped)/total:.1%} | "
            f"2分钟内方向锁定 |",
            f"| ≤60s 锁定 | {ls.locked_by_60s + ls.never_flipped} | "
            f"{(ls.locked_by_60s + ls.never_flipped)/total:.1%} | "
            f"尾盘窗口前已锁定 |",
            f"| ≤15s 锁定 | {ls.locked_by_15s + ls.never_flipped} | "
            f"{(ls.locked_by_15s + ls.never_flipped)/total:.1%} | "
            f"最后15秒才锁定 |",
            "",
            f"**关键发现**: {ls.never_flipped + ls.locked_by_180s} 个 Event "
            f"({(ls.never_flipped + ls.locked_by_180s)/total:.1%}) "
            f"在 3 分钟内方向已锁定，之后再无翻转。",
            f"这些事件如果能在锁定后尽早入场，均价远低于尾盘的 0.97，"
            f"盈亏比大幅提升。",
            "",
        ])

    # ── 最早入场策略（从开盘扫描）──
    if earliest_summary and earliest_summary.get("trades", 0) > 0:
        es = earliest_summary
        lines.extend([
            "---",
            "",
            "## 最早入场策略（从开盘起监控）",
            "",
            "_从 300s 开盘起逐秒扫描，条件首次满足+盘口足够即入场。_",
            "",
            "| 指标 | 数值 |",
            "|------|------|",
            f"| 总交易 | {es['trades']} / {lock_stats.total if lock_stats else '?'} Event |",
            f"| 胜率 | {es['win_rate']:.1%} |",
            f"| 平均入场价 | {es['avg_price']:.4f} |",
            f"| 平均盈亏 | {es['avg_pnl']:+.4f} |",
            f"| 总盈亏 | {es['total_pnl']:+.2f} |",
            f"| 盈利/亏损 | {es['won_count']} / {es['lost_count']} |",
            "",
            "**入场时间分布:**",
            "",
            "| 入场时段 | 交易数 |",
            "|---------|--------|",
        ])
        for label, cnt in es["entry_dist"].items():
            lines.append(f"| {label} | {cnt} |")
        lines.append("")

    # ── 最佳入场时间详情 ──
    lines.extend([
        "---",
        "",
        "## 最佳入场时间详情",
        "",
        f"### 按胜率最优: {best_wr.entry_sec}s",
        "",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| 总 Event | {best_wr.events_total} |",
        f"| 策略通过 | {best_wr.events_passed} |",
        f"| 胜率 | {best_wr.win_rate:.1%} |",
        f"| 均价 | {best_wr.avg_price:.4f} |",
        f"| 平均盈亏 | {best_wr.avg_pnl:+.4f} |",
        f"| 总盈亏 | {best_wr.total_pnl:+.2f} |",
        "",
    ])

    if best_wr.reject_reasons:
        lines.extend([
            "**拒绝原因分布:**",
            "",
            "| 原因 | 数量 |",
            "|------|------|",
        ])
        for reason, count in sorted(best_wr.reject_reasons.items(), key=lambda x: -x[1]):
            lines.append(f"| {reason} | {count} |")
        lines.append("")

    if best_pnl is not best_wr:
        lines.extend([
            f"### 按总盈亏最优: {best_pnl.entry_sec}s",
            "",
            f"| 指标 | 数值 |",
            f"|------|------|",
            f"| 总 Event | {best_pnl.events_total} |",
            f"| 策略通过 | {best_pnl.events_passed} |",
            f"| 胜率 | {best_pnl.win_rate:.1%} |",
            f"| 均价 | {best_pnl.avg_price:.4f} |",
            f"| 平均盈亏 | {best_pnl.avg_pnl:+.4f} |",
            f"| 总盈亏 | {best_pnl.total_pnl:+.2f} |",
            "",
        ])

    # ── 全时间点对比（通过数 + 胜率） ──
    lines.extend([
        "---",
        "",
        "## 入场时间 vs 胜率 / 盈亏",
        "",
        "| 入场时间 | 策略通过 | 盘口不足 | 实际成交 | 胜率 | 均价 | 平均盈亏 | 总盈亏 |",
        "|---------|---------|---------|---------|------|------|---------|--------|",
    ])
    for s in all_stats:
        lines.append(
            f"| {s.entry_sec}s | {s.events_passed} | {s.depth_rejected} | "
            f"{s.events_traded} | {s.win_rate:.1%} | "
            f"{s.avg_price:.4f} | {s.avg_pnl:+.4f} | {s.total_pnl:+.2f} |"
        )
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="尾盘扫尾策略回测 — 每个 Event 最多一笔交易")
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
    print(f"  Event 数: {s['events']}, 快照数: {s['snapshots']:,}")

    print("计算特征...")
    fv = compute_features(df)
    for name in fv:
        print(f"  {name}: {fv[name].notna().sum():,} 有效值")

    print(f"扫描入场时间 ({len(ENTRY_TIMES)} 个时间点)...")
    all_stats = scan_entry_times(df, fv)

    for st in all_stats:
        print(f"  {st.entry_sec}s: {st.events_passed}/{st.events_total} 通过, "
              f"胜率 {st.win_rate:.1%}, 总盈亏 {st.total_pnl:+.2f}")

    print("计算翻转率...")
    reversals = compute_reversal_rates(df)

    print("计算方向锁定...")
    lock_stats = compute_lock_stats(df)

    print("最早入场扫描...")
    _, earliest_summary = backtest_earliest(df, fv)
    es = earliest_summary
    print(f"  最早入场: {es['trades']}笔 胜率{es['win_rate']:.1%} "
          f"均价{es['avg_price']:.4f} 总盈亏{es['total_pnl']:+.2f}")

    print("生成报告...")
    report = generate_report(all_stats, reversals, lock_stats, earliest_summary)

    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = output_dir / f"backtest_report_{ts}.md"
    report_path.write_text(report, encoding="utf-8")

    print(f"\n报告: {report_path}")


if __name__ == "__main__":
    main()
