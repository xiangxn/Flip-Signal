"""
Remaining Time Conditioning (PRD2 §8.2)

Evaluates a feature separately at specific remaining-second checkpoints.
The same feature value means different things at 60s vs 15s remaining.

Checkpoints: 60s, 30s, 15s, 5s — matching tail-trading decision points.

Now uses real P&L (Polymarket prices) as the primary metric.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from analysis.pnl import compute_pnl, compute_won
from features.base import Feature


CHECKPOINTS = [60, 55, 50, 45, 40, 35, 30, 25, 20, 15, 10, 5]  # remaining seconds (every 5s)


@dataclass
class TimeConditionResult:
    remaining_sec: int
    samples: int
    mean_feature: float
    # Win rate split
    win_rate_above_median: float
    win_rate_below_median: float
    # P&L split (primary metric)
    mean_pnl_above_median: float
    mean_pnl_below_median: float
    separation_wr: float   # WR above - WR below
    separation_pnl: float  # P&L above - P&L below


def analyze_time_conditioned(
    df: pd.DataFrame,
    feature: Feature,
    feature_values: pd.Series,
    tolerance: int = 3,
) -> list[TimeConditionResult]:
    """Evaluate the feature at each remaining-time checkpoint.

    At each checkpoint, snaps to the nearest snapshot within ±tolerance seconds,
    splits at the feature median, and compares win rate + real P&L above vs below
    the median.

    Parameters
    ----------
    df : pd.DataFrame
        Snapshot-level data.
    feature : Feature
        The feature being analysed.
    feature_values : pd.Series
        Pre-computed feature values.
    tolerance : int
        Seconds tolerance for matching a checkpoint (default 3).

    Returns
    -------
    list[TimeConditionResult]
    """
    pnl = compute_pnl(df)
    won = compute_won(df)
    distance = (df["price"] - df["open"]) / df["open"]
    valid = (distance != 0) & feature_values.notna() & pnl.notna()

    results = []
    for checkpoint in CHECKPOINTS:
        near = (
            (df["remaining_sec"] >= checkpoint - tolerance)
            & (df["remaining_sec"] <= checkpoint + tolerance)
            & valid
        )

        subset = feature_values[near]
        subset_won = won[near]
        subset_pnl = pnl[near]

        if len(subset) < 20:
            results.append(TimeConditionResult(
                remaining_sec=checkpoint,
                samples=len(subset),
                mean_feature=0,
                win_rate_above_median=0,
                win_rate_below_median=0,
                mean_pnl_above_median=0,
                mean_pnl_below_median=0,
                separation_wr=0,
                separation_pnl=0,
            ))
            continue

        median = subset.median()
        above = subset >= median
        below = subset < median

        wr_above = float(subset_won[above].mean()) if above.sum() > 0 else float("nan")
        wr_below = float(subset_won[below].mean()) if below.sum() > 0 else float("nan")
        pnl_above = float(subset_pnl[above].mean()) if above.sum() > 0 else float("nan")
        pnl_below = float(subset_pnl[below].mean()) if below.sum() > 0 else float("nan")

        results.append(TimeConditionResult(
            remaining_sec=checkpoint,
            samples=len(subset),
            mean_feature=round(float(subset.mean()), 6),
            win_rate_above_median=round(wr_above, 4),
            win_rate_below_median=round(wr_below, 4),
            mean_pnl_above_median=round(pnl_above, 4),
            mean_pnl_below_median=round(pnl_below, 4),
            separation_wr=round(wr_above - wr_below, 4),
            separation_pnl=round(pnl_above - pnl_below, 4),
        ))

    return results


def time_condition_table(results: list[TimeConditionResult]) -> str:
    """Format time-conditioned results as a markdown table with P&L."""
    lines = [
        "| 剩余时间 | 样本数 | 特征均值 | 胜率≥中位 | 胜率<中位 | "
        "盈亏≥中位 | 盈亏<中位 | 盈亏差 |",
        "|---------|--------|---------|----------|----------|"
        "----------|----------|--------|",
    ]
    import math
    for r in results:
        wa = "—" if math.isnan(r.win_rate_above_median) else f"{r.win_rate_above_median:.2%}"
        wb = "—" if math.isnan(r.win_rate_below_median) else f"{r.win_rate_below_median:.2%}"
        pa = "—" if math.isnan(r.mean_pnl_above_median) else f"{r.mean_pnl_above_median:+.4f}"
        pb = "—" if math.isnan(r.mean_pnl_below_median) else f"{r.mean_pnl_below_median:+.4f}"
        sp = "—" if math.isnan(r.separation_pnl) else f"{r.separation_pnl:+.4f}"
        lines.append(
            f"| {r.remaining_sec}s | {r.samples} | {r.mean_feature:.4g} | "
            f"{wa} | {wb} | {pa} | {pb} | {sp} |"
        )
    return "\n".join(lines)
