"""
Remaining Time Conditioning (PRD2 §8.2)

Evaluates a feature separately at specific remaining-second checkpoints.
The same feature value means different things at 60s vs 15s remaining.

Checkpoints: 60s, 30s, 15s, 5s — matching tail-trading decision points.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from features.base import Feature


CHECKPOINTS = [60, 30, 15, 5]  # remaining seconds to evaluate at


@dataclass
class TimeConditionResult:
    remaining_sec: int
    samples: int
    mean_feature: float
    win_rate_above_median: float
    win_rate_below_median: float
    separation: float  # win_rate_above - win_rate_below (positive = feature works)


def analyze_time_conditioned(
    df: pd.DataFrame,
    feature: Feature,
    feature_values: pd.Series,
    tolerance: int = 3,
) -> list[TimeConditionResult]:
    """Evaluate the feature at each remaining-time checkpoint.

    At each checkpoint, snaps to the nearest snapshot within +-tolerance seconds,
    splits at the feature median, and compares win rates above vs below median.

    Parameters
    ----------
    df : pd.DataFrame
        Snapshot-level data.
    feature : Feature
        The feature being analyzed.
    feature_values : pd.Series
        Pre-computed feature values.
    tolerance : int
        Seconds tolerance for matching a checkpoint (default 3).

    Returns
    -------
    list[TimeConditionResult]
    """
    distance = (df["price"] - df["open"]) / df["open"]
    bet_yes = distance > 0
    bet_no = distance < 0
    won = (bet_yes & (df["outcome"] == 0)) | (bet_no & (df["outcome"] == 1))
    valid = (bet_yes | bet_no) & feature_values.notna()

    results = []
    for checkpoint in CHECKPOINTS:
        # Find snapshots near this remaining time
        near = (df["remaining_sec"] >= checkpoint - tolerance) & \
               (df["remaining_sec"] <= checkpoint + tolerance) & valid

        subset = feature_values[near]
        subset_won = won[near]

        if len(subset) < 20:
            results.append(TimeConditionResult(
                remaining_sec=checkpoint,
                samples=len(subset),
                mean_feature=float(subset.mean()) if len(subset) > 0 else 0,
                win_rate_above_median=0,
                win_rate_below_median=0,
                separation=0,
            ))
            continue

        median = subset.median()
        above = subset >= median
        below = subset < median

        wr_above = float(subset_won[above].mean()) if above.sum() > 0 else float("nan")
        wr_below = float(subset_won[below].mean()) if below.sum() > 0 else float("nan")

        results.append(TimeConditionResult(
            remaining_sec=checkpoint,
            samples=len(subset),
            mean_feature=round(float(subset.mean()), 6),
            win_rate_above_median=round(wr_above, 4),
            win_rate_below_median=round(wr_below, 4),
            separation=round(wr_above - wr_below, 4),
        ))

    return results


def time_condition_table(results: list[TimeConditionResult]) -> str:
    """Format time-conditioned results as a markdown table."""
    lines = [
        "| Remaining | Samples | Mean Feature | WR ≥ Median | WR < Median | Separation |",
        "|-----------|---------|-------------|-------------|-------------|------------|",
    ]
    import math
    for r in results:
        wa = "—" if math.isnan(r.win_rate_above_median) else f"{r.win_rate_above_median:.2%}"
        wb = "—" if math.isnan(r.win_rate_below_median) else f"{r.win_rate_below_median:.2%}"
        sep = "—" if math.isnan(r.separation) else f"{r.separation:+.4f}"
        lines.append(
            f"| {r.remaining_sec}s | {r.samples} | {r.mean_feature:.4g} | "
            f"{wa} | {wb} | {sep} |"
        )
    return "\n".join(lines)
