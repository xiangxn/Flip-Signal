"""
Monotonicity chart generator.

Creates per-feature charts showing the relationship between feature values
and win rate. Each chart includes:
  - A bucketed bar chart (win rate per quantile bucket)
  - A fitted trend line
  - Spearman ρ annotation

Charts are saved as PNG files and referenced in the Markdown report.
"""

from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from analysis.pnl import compute_won
from features.base import Feature


# Consistent style — use macOS Chinese font
plt.rcParams.update({
    "figure.dpi": 120,
    "font.size": 10,
    "axes.titlesize": 13,
    "axes.labelsize": 11,
    "font.family": "sans-serif",
    "font.sans-serif": ["PingFang SC", "Heiti SC", "STHeiti", "Arial Unicode MS"],
    "axes.unicode_minus": False,
})


def generate_monotonicity_chart(
    df: pd.DataFrame,
    feature: Feature,
    feature_values: pd.Series,
    output_dir: str,
    n_buckets: int = 10,
) -> str | None:
    """Generate a monotonicity chart for a single feature.

    Parameters
    ----------
    df : pd.DataFrame
    feature : Feature
    feature_values : pd.Series
    output_dir : str
        Directory to save the PNG.
    n_buckets : int

    Returns
    -------
    str | None
        Relative path to the generated PNG, or None if insufficient data.
    """
    distance = (df["price"] - df["open"]) / df["open"]
    won = compute_won(df)
    valid = (distance != 0) & feature_values.notna()

    fv = feature_values[valid].values
    w = won[valid].values

    if len(fv) < 20:
        return None

    # Spearman
    r, p = spearmanr(fv, w)

    # Bucket win rates for bar chart
    edges = np.percentile(fv, np.linspace(0, 100, n_buckets + 1))
    edges = np.unique(edges)

    # Fallback for sparse/binary features: use fewer buckets
    if len(edges) < 3:
        n_buckets = max(2, len(edges) - 1)
        edges = np.percentile(fv, np.linspace(0, 100, n_buckets + 1))
        edges = np.unique(edges)

    bucket_wr = []
    bucket_mid = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        mask = (fv >= lo) & (fv < hi) if i < len(edges) - 2 else (fv >= lo) & (fv <= hi)
        if mask.sum() >= 5:
            bucket_wr.append(float(w[mask].mean()))
            bucket_mid.append((lo + hi) / 2)

    if len(bucket_wr) < 2:
        return None

    # --- Plot ---
    fig, ax = plt.subplots(figsize=(10, 5))

    # Bar chart: win rate per bucket
    xs = np.arange(len(bucket_wr))
    bar_colors = ["#2ecc71" if wr >= 0.7 else "#e74c3c" if wr < 0.55 else "#f39c12"
                  for wr in bucket_wr]
    ax.bar(xs, bucket_wr, color=bar_colors, alpha=0.85, width=0.7)

    # Trend line over raw scatter bins
    if len(bucket_mid) >= 2:
        # Normalize bucket_mid to xs range for the overlay
        mid_min, mid_max = min(bucket_mid), max(bucket_mid)
        if mid_max > mid_min:
            norm_mid = [(m - mid_min) / (mid_max - mid_min) * (len(xs) - 1) for m in bucket_mid]
            z = np.polyfit(norm_mid, bucket_wr, 1)
            pfit = np.poly1d(z)
            x_smooth = np.linspace(0, len(xs) - 1, 50)
            ax.plot(x_smooth, pfit(x_smooth), color="#3498db", linewidth=2,
                    label=f"趋势线 (ρ={r:+.3f}, p={p:.4f})")

    # Baseline: overall win rate
    baseline = float(w.mean())
    ax.axhline(y=baseline, color="#7f8c8d", linestyle="--", linewidth=1,
               label=f"基线胜率={baseline:.1%}")

    ax.set_ylim(0, 1.05)
    ax.yaxis.set_major_formatter(mticker.PercentFormatter(1.0))

    # X-axis labels: bucket ranges
    x_labels = [f"[{edges[i]:.2g},\n{edges[i+1]:.2g}]" for i in range(len(edges) - 1)
                if ((fv >= edges[i]) & (fv < edges[i+1]) if i < len(edges) - 2
                    else (fv >= edges[i]) & (fv <= edges[i+1])).sum() >= 5]
    if len(x_labels) > 8:
        # Show fewer labels if too many
        step = max(1, len(x_labels) // 8)
        shown = []
        for k, lbl in enumerate(x_labels):
            shown.append(lbl if k % step == 0 else "")
        x_labels = shown
    ax.set_xticks(xs)
    ax.set_xticklabels(x_labels, fontsize=7, rotation=45, ha="right")

    ax.set_title(f"{feature.name} — 特征值 vs 胜率")
    ax.set_xlabel("特征值区间")
    ax.set_ylabel("胜率")
    ax.legend(loc="lower right", fontsize=8)
    ax.grid(axis="y", alpha=0.3)

    fig.tight_layout()

    # Save
    out_path = Path(output_dir) / f"chart_{feature.name}.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=120, bbox_inches="tight")
    plt.close(fig)

    return f"chart_{feature.name}.png"
