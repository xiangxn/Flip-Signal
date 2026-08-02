"""
Markdown Report Generator (PRD2 §9)

Generates a self-contained Markdown research report for each feature,
including bucket analysis, time-conditioned results, monotonicity,
and pairwise combinations.
"""

from datetime import datetime, timezone

import pandas as pd

from analysis.buckets import BucketResult, analyze_buckets, bucket_summary_table
from analysis.combination import CombinationResult, analyze_combination, combination_matrix
from analysis.monotonicity import MonotonicityResult, analyze_monotonicity
from analysis.time_condition import TimeConditionResult, analyze_time_conditioned, time_condition_table
from features.base import FEATURES, Feature


def generate_report(df: pd.DataFrame) -> str:
    """Generate a complete Markdown research report for all features.

    Parameters
    ----------
    df : pd.DataFrame
        Snapshot-level data from loader.load_events().

    Returns
    -------
    str
        Complete Markdown report.
    """
    lines = [
        "# Feature Research Lab — Analysis Report",
        "",
        f"**Generated**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "---",
        "",
        "## Dataset Summary",
        "",
    ]

    events = df["condition_id"].nunique()
    snapshots = len(df)
    yes = int((df.groupby("condition_id")["outcome"].first() == 0).sum())
    no = events - yes
    ts_min = df["ts"].min()
    ts_max = df["ts"].max()

    lines.extend([
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Events | {events} |",
        f"| Snapshots | {snapshots} |",
        f"| YES (Up) | {yes} ({yes/max(events,1):.1%}) |",
        f"| NO (Down) | {no} ({no/max(events,1):.1%}) |",
        f"| Date Range | {ts_min} → {ts_max} |",
        "",
        "---",
        "",
    ])

    # --- Per-feature analysis ---
    feature_values_cache: dict[str, pd.Series] = {}

    for i, feature in enumerate(FEATURES, 1):
        lines.extend([
            f"## Feature {i}: {feature.name}",
            "",
            f"**{feature.description}**",
            "",
        ])

        # Compute feature values (cached for combinations)
        if feature.name not in feature_values_cache:
            fv = feature.compute(df)
            feature_values_cache[feature.name] = fv
        else:
            fv = feature_values_cache[feature.name]

        # Bucket analysis
        lines.append("### Bucket Analysis")
        lines.append("")
        buckets = analyze_buckets(df, feature, fv)
        lines.append(bucket_summary_table(buckets))
        lines.append("")

        # Time-conditioned analysis
        lines.append("### Time-Conditioned Analysis")
        lines.append("")
        time_results = analyze_time_conditioned(df, feature, fv)
        lines.append(time_condition_table(time_results))
        lines.append("")

        # Monotonicity
        lines.append("### Monotonicity (Spearman Rank)")
        lines.append("")
        mono = analyze_monotonicity(df, feature, fv)
        lines.extend([
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Spearman ρ | {mono.spearman_r} |",
            f"| p-value | {mono.p_value} |",
            f"| Samples | {mono.samples} |",
            f"| Verdict | **{mono.interpretation}** |",
            "",
        ])

        # Best bucket
        best_bucket = _find_best_bucket(buckets)
        if best_bucket:
            lines.extend([
                "### Best Region",
                "",
                f"- **Bucket**: {best_bucket.bucket_label}",
                f"- **Samples**: {best_bucket.samples}",
                f"- **Win Rate**: {best_bucket.win_rate:.2%}",
                f"- **EV**: {best_bucket.ev:+.4f}",
                "",
            ])

        lines.append("---")
        lines.append("")

    # --- Feature Combinations ---
    lines.extend([
        "## Feature Combinations (2D Analysis)",
        "",
        "_Top feature pairs evaluated on a 4×4 quantile grid._",
        "",
    ])

    # Combine adjacent features (F1×F2, F3×F4, F5×F6, F1×F7)
    pairs = [
        (0, 1),   # DistanceToStrike × SafetyRatio
        (2, 3),   # ReversalCapacity × VolatilityExpansion
        (4, 5),   # DirectionPersistence × SignedFlow
        (0, 6),   # DistanceToStrike × VolumeAcceleration
        (1, 2),   # SafetyRatio × ReversalCapacity
        (3, 5),   # VolatilityExpansion × SignedFlow
    ]

    for ai, bi in pairs:
        if ai >= len(FEATURES) or bi >= len(FEATURES):
            continue
        fa, fb = FEATURES[ai], FEATURES[bi]

        # Ensure feature values are computed
        for f in [fa, fb]:
            if f.name not in feature_values_cache:
                feature_values_cache[f.name] = f.compute(df)

        result = analyze_combination(
            df, fa, fb,
            feature_values_cache[fa.name],
            feature_values_cache[fb.name],
        )

        lines.extend([
            f"### {fa.name} × {fb.name}",
            "",
            combination_matrix(result),
            "",
        ])

        if result.best_cell:
            lines.append(
                f"**Best cell**: {result.best_cell['bucket_a']} × {result.best_cell['bucket_b']} "
                f"— WR={result.best_cell['win_rate']:.2%}, n={result.best_cell['samples']}"
            )
            lines.append("")

        if result.worst_cell:
            lines.append(
                f"**Worst cell**: {result.worst_cell['bucket_a']} × {result.worst_cell['bucket_b']} "
                f"— WR={result.worst_cell['win_rate']:.2%}, n={result.worst_cell['samples']}"
            )
            lines.append("")

    return "\n".join(lines)


def _find_best_bucket(buckets: list[BucketResult]) -> BucketResult | None:
    """Find the bucket with the highest win rate (min 10 samples)."""
    valid = [b for b in buckets if b.samples >= 10]
    if not valid:
        return None
    return max(valid, key=lambda b: b.win_rate)
