"""
Bucket Analysis (PRD2 §8.1)

Splits a feature's values into N quantile buckets and computes win rate plus
realised P&L (using Polymarket entry prices) for each bucket.

This answers: "When the feature is in range X, what was the actual trading outcome?"
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from analysis.pnl import compute_pnl, compute_won
from features.base import Feature


@dataclass
class BucketResult:
    bucket_label: str
    low: float
    high: float
    samples: int
    win_rate: float
    avg_price: float    # average Polymarket entry price in this bucket
    mean_pnl: float     # average realised P&L per share
    total_pnl: float    # sum of P&L across all samples in this bucket


def analyze_buckets(
    df: pd.DataFrame,
    feature: Feature,
    feature_values: pd.Series,
    n_buckets: int = 10,
) -> list[BucketResult]:
    """Compute win rate and real P&L per feature bucket.

    Bet direction:
      - price > open → bet YES (buy YES at yes_price)
      - price < open → bet NO  (buy NO at no_price)

    Parameters
    ----------
    df : pd.DataFrame
        Snapshot-level data with price, open, outcome, yes_price, no_price.
    feature : Feature
        The feature being analysed.
    feature_values : pd.Series
        Pre-computed feature values, same index as df.
    n_buckets : int
        Number of buckets (default 10).

    Returns
    -------
    list[BucketResult]
    """
    distance = (df["price"] - df["open"]) / df["open"]
    valid = (distance != 0) & feature_values.notna()

    fv = feature_values[valid]
    pnl = compute_pnl(df)[valid]
    won = compute_won(df)[valid]

    if len(fv) == 0:
        return []

    # Quantile-based buckets
    bucket_edges = np.percentile(fv, np.linspace(0, 100, n_buckets + 1))
    bucket_edges = np.unique(bucket_edges)

    if len(bucket_edges) < 2:
        return []

    results = []
    for i in range(len(bucket_edges) - 1):
        low, high = bucket_edges[i], bucket_edges[i + 1]
        if i < len(bucket_edges) - 2:
            mask = (fv >= low) & (fv < high)
        else:
            mask = (fv >= low) & (fv <= high)

        bucket_won = won[mask]
        bucket_pnl = pnl[mask]
        samples = len(bucket_pnl)

        wr = float(bucket_won.mean()) if samples > 0 else 0.0
        mp = float(bucket_pnl.mean()) if samples > 0 else 0.0
        tp = float(bucket_pnl.sum()) if samples > 0 else 0.0

        # Compute average entry price for this bucket
        # (direction-dependent: YES bets use yes_price, NO bets use no_price)
        bucket_dist = distance[valid][mask]
        bucket_yes_price = df.loc[valid, "yes_price"][mask]
        bucket_no_price = df.loc[valid, "no_price"][mask]
        entry_prices = pd.Series(np.where(bucket_dist > 0, bucket_yes_price, bucket_no_price),
                                 index=bucket_pnl.index)
        avg_price = float(entry_prices.mean()) if samples > 0 else 0.0

        results.append(BucketResult(
            bucket_label=f"[{low:.4g}, {high:.4g}]",
            low=float(low),
            high=float(high),
            samples=samples,
            win_rate=round(wr, 4),
            avg_price=round(avg_price, 4),
            mean_pnl=round(mp, 4),
            total_pnl=round(tp, 4),
        ))

    return results


def bucket_summary_table(results: list[BucketResult]) -> str:
    """Format bucket results as a markdown table with real P&L."""
    if not results:
        return "_No data_"

    lines = [
        "| 特征值区间 | 样本数 | 胜率 | 均价 | 平均盈亏 | 总盈亏 |",
        "|-----------|--------|------|------|---------|--------|",
    ]
    for r in results:
        lines.append(
            f"| {r.bucket_label} | {r.samples} | {r.win_rate:.2%} | "
            f"{r.avg_price:.4f} | {r.mean_pnl:+.4f} | {r.total_pnl:+.4f} |"
        )
    return "\n".join(lines)
