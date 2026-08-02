"""
Bucket Analysis (PRD2 §8.1)

Splits a feature's values into N equal-width (or equal-count) buckets
and computes win rate and EV for each bucket.

This answers: "When the feature is in range X, what's the historical outcome?"
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from features.base import Feature


@dataclass
class BucketResult:
    bucket_label: str
    low: float
    high: float
    samples: int
    win_rate: float
    ev: float  # expected value: win_rate * 1 + (1-win_rate) * (-1) = 2*win_rate - 1


def analyze_buckets(
    df: pd.DataFrame,
    feature: Feature,
    feature_values: pd.Series,
    n_buckets: int = 10,
) -> list[BucketResult]:
    """Compute win rate and EV per feature bucket.

    The bet direction for each row is inferred from price vs open:
      - price > open → bet YES
      - price < open → bet NO

    A bet wins when:
      - bet YES and outcome == 0 (Up won), or bet NO and outcome == 1 (Down won)

    Parameters
    ----------
    df : pd.DataFrame
        Snapshot-level data with 'price', 'open', 'outcome' columns.
    feature : Feature
        The feature being analyzed.
    feature_values : pd.Series
        Pre-computed feature values, same index as df.
    n_buckets : int
        Number of buckets (default 10).

    Returns
    -------
    list[BucketResult]
        One result per bucket, sorted by bucket low value.
    """
    # Determine bet direction and win
    distance = (df["price"] - df["open"]) / df["open"]
    bet_yes = distance > 0
    bet_no = distance < 0

    won = (bet_yes & (df["outcome"] == 0)) | (bet_no & (df["outcome"] == 1))

    # Remove rows where price == open (no bet direction)
    valid = bet_yes | bet_no
    fv = feature_values[valid]
    won = won[valid]

    if len(fv) == 0:
        return []

    # Create quantile-based buckets
    bucket_edges = np.percentile(fv, np.linspace(0, 100, n_buckets + 1))
    # Deduplicate edges (in case of many identical values)
    bucket_edges = np.unique(bucket_edges)

    if len(bucket_edges) < 2:
        return []

    results = []
    for i in range(len(bucket_edges) - 1):
        low, high = bucket_edges[i], bucket_edges[i + 1]
        mask = (fv >= low) & (fv < high) if i < len(bucket_edges) - 2 else (fv >= low) & (fv <= high)
        bucket_data = won[mask]

        samples = len(bucket_data)
        wr = float(bucket_data.mean()) if samples > 0 else 0.0
        ev = 2 * wr - 1  # simplified: win → +1, lose → -1

        results.append(BucketResult(
            bucket_label=f"[{low:.4g}, {high:.4g}]",
            low=float(low),
            high=float(high),
            samples=samples,
            win_rate=round(wr, 4),
            ev=round(ev, 4),
        ))

    return results


def bucket_summary_table(results: list[BucketResult]) -> str:
    """Format bucket results as a markdown table."""
    if not results:
        return "_No data_"

    lines = [
        "| Bucket | Samples | Win Rate | EV |",
        "|--------|---------|----------|-----|",
    ]
    for r in results:
        lines.append(
            f"| {r.bucket_label} | {r.samples} | {r.win_rate:.2%} | {r.ev:+.4f} |"
        )
    return "\n".join(lines)
