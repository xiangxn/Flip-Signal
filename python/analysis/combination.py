"""
Feature Combination Analysis (PRD2 §8.4)

Evaluates pairs of features in a 2D grid to discover synergy.
For each cell in the grid, computes win rate and real P&L.

Now uses Polymarket entry prices for P&L computation.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from analysis.pnl import compute_pnl, compute_won
from features.base import Feature


@dataclass
class CombinationResult:
    feature_a: str
    feature_b: str
    cells: list[dict] = field(default_factory=list)
    best_cell: dict | None = None   # by mean P&L
    worst_cell: dict | None = None  # by mean P&L


def analyze_combination(
    df: pd.DataFrame,
    feature_a: Feature,
    feature_b: Feature,
    values_a: pd.Series,
    values_b: pd.Series,
    buckets: int = 4,
) -> CombinationResult:
    """Evaluate a 2D feature combination grid.

    Parameters
    ----------
    df : pd.DataFrame
        Snapshot-level data.
    feature_a, feature_b : Feature
        The two features to combine.
    values_a, values_b : pd.Series
        Pre-computed feature values.
    buckets : int
        Number of buckets per feature (default 4, giving up to 16 cells).

    Returns
    -------
    CombinationResult
    """
    pnl = compute_pnl(df)
    won = compute_won(df)
    distance = (df["price"] - df["open"]) / df["open"]

    valid = (distance != 0) & values_a.notna() & values_b.notna() & pnl.notna()
    va = values_a[valid]
    vb = values_b[valid]
    w = won[valid]
    p = pnl[valid]

    if len(va) < buckets * buckets * 5:
        return CombinationResult(
            feature_a=feature_a.name,
            feature_b=feature_b.name,
        )

    labels = [f"Q{i + 1}" for i in range(buckets)]
    try:
        qa = pd.qcut(va, buckets, labels=labels, duplicates="drop")
        qb = pd.qcut(vb, buckets, labels=labels, duplicates="drop")
    except ValueError:
        return CombinationResult(
            feature_a=feature_a.name,
            feature_b=feature_b.name,
        )

    cells = []
    best = None
    worst = None

    for la in qa.cat.categories:
        for lb in qb.cat.categories:
            mask = (qa == la) & (qb == lb)
            cell_won = w[mask]
            cell_pnl = p[mask]
            n = len(cell_won)
            wr = float(cell_won.mean()) if n > 0 else 0
            mp = float(cell_pnl.mean()) if n > 0 else 0

            cell = {
                "bucket_a": str(la),
                "bucket_b": str(lb),
                "samples": int(n),
                "win_rate": round(wr, 4),
                "mean_pnl": round(mp, 4),
            }
            cells.append(cell)

            if n >= 10:
                if best is None or mp > best["mean_pnl"]:
                    best = cell
                if worst is None or mp < worst["mean_pnl"]:
                    worst = cell

    return CombinationResult(
        feature_a=feature_a.name,
        feature_b=feature_b.name,
        cells=cells,
        best_cell=best,
        worst_cell=worst,
    )


def combination_matrix(result: CombinationResult) -> str:
    """Format combination result as a markdown win-rate matrix with P&L."""
    if not result.cells:
        return "_Insufficient data for combination analysis_"

    a_labels = sorted(set(c["bucket_a"] for c in result.cells))
    b_labels = sorted(set(c["bucket_b"] for c in result.cells))

    header = (
        "| "
        + result.feature_a
        + " \\ "
        + result.feature_b
        + " | "
        + " | ".join(b_labels)
        + " |"
    )
    sep = "|" + "|".join(["---"] * (len(b_labels) + 1)) + "|"

    rows = [header, sep]
    for la in a_labels:
        vals = []
        for lb in b_labels:
            cell = next(
                (c for c in result.cells if c["bucket_a"] == la and c["bucket_b"] == lb),
                None,
            )
            if cell and cell["samples"] >= 10:
                vals.append(
                    f"{cell['win_rate']:.1%} P&L{cell['mean_pnl']:+.3f} (n={cell['samples']})"
                )
            else:
                vals.append("—")
        rows.append("| " + la + " | " + " | ".join(vals) + " |")

    return "\n".join(rows)
