"""
Markdown 报告生成器

为每个特征生成包含桶分析、时间条件分析、单调性分析和图表的完整 Markdown 报告。
"""

from datetime import datetime, timezone

import pandas as pd

from analysis.buckets import BucketResult, analyze_buckets, bucket_summary_table
from analysis.combination import CombinationResult, analyze_combination, combination_matrix
from analysis.monotonicity import MonotonicityResult, analyze_monotonicity
from analysis.time_condition import (
    TimeConditionResult, analyze_time_conditioned, time_condition_table,
)
from analysis.viz import generate_monotonicity_chart
from features.base import FEATURES, Feature


def generate_report(df: pd.DataFrame, output_dir: str = "../reports/") -> str:
    """生成完整的 Markdown 研究报告。

    Parameters
    ----------
    df : pd.DataFrame
        loader.load_events() 返回的 snapshot 级数据。
    output_dir : str
        图表输出目录。

    Returns
    -------
    str
        完整的 Markdown 报告。
    """
    lines = [
        "# Feature Research Lab — 分析报告",
        "",
        f"**生成时间**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "---",
        "",
        "## 数据集概览",
        "",
    ]

    events = df["condition_id"].nunique()
    snapshots = len(df)
    yes = int((df.groupby("condition_id")["outcome"].first() == 0).sum())
    no = events - yes
    ts_min = df["ts"].min()
    ts_max = df["ts"].max()

    lines.extend([
        "| 指标 | 数值 |",
        "|------|------|",
        f"| 事件数 | {events} |",
        f"| 快照数 | {snapshots} |",
        f"| YES (涨) | {yes} ({yes / max(events, 1):.1%}) |",
        f"| NO (跌) | {no} ({no / max(events, 1):.1%}) |",
        f"| 时间范围 | {ts_min} → {ts_max} |",
        "",
        "---",
        "",
    ])

    # --- 逐特征分析 ---
    feature_values_cache: dict[str, pd.Series] = {}
    mono_results: dict[str, MonotonicityResult] = {}

    for i, feature in enumerate(FEATURES, 1):
        lines.extend([
            f"## 特征 {i}: {feature.name}",
            "",
            f"**{feature.description}**",
            "",
        ])

        # 计算特征值
        if feature.name not in feature_values_cache:
            fv = feature.compute(df)
            feature_values_cache[feature.name] = fv
        else:
            fv = feature_values_cache[feature.name]

        # --- 桶分析 ---
        lines.append("### 桶分析")
        lines.append("")
        lines.append("_按特征值等分区间，统计各区间的胜率和真实盈亏。_")
        lines.append("")
        buckets = analyze_buckets(df, feature, fv)
        lines.append(bucket_summary_table(buckets))
        lines.append("")

        # --- 时间条件分析 ---
        lines.append("### 时间条件分析")
        lines.append("")
        lines.append("_在尾盘关键时间点，按特征中位数分组对比。_")
        lines.append("")
        time_results = analyze_time_conditioned(df, feature, fv)
        lines.append(time_condition_table(time_results))
        lines.append("")

        # --- 单调性 + 分组分离 ---
        mono = analyze_monotonicity(df, feature, fv)
        mono_results[feature.name] = mono

        if mono.verdict_method in ("spearman", "both"):
            lines.append("### 单调性 (Spearman 秩相关)")
            lines.append("")
            lines.extend([
                "| 指标 | 数值 |",
                "|------|------|",
                f"| Spearman ρ (vs 胜/负) | {mono.spearman_r_won} (p={mono.p_value_won}) |",
                f"| Spearman ρ (vs P&L) | {mono.spearman_r_pnl} (p={mono.p_value_pnl}) |",
                f"| 样本数 | {mono.samples} |",
                f"| 判定 | **{mono.interpretation}** |",
                "",
            ])

        # Separation analysis — always show if natural threshold exists
        if mono.separation_is_natural or mono.verdict_method == "separation":
            thr_label = "自然阈值" if mono.separation_is_natural else "中位数"
            lines.append("### 分组分离分析")
            lines.append("")
            lines.append(
                f"_按 {thr_label} "
                f"({mono.separation_threshold:.4g}) 将特征分为两组，"
                f"对比胜率差异。_"
            )
            lines.append("")
            lines.extend([
                "| 分组 | 样本数 | 胜率 |",
                "|------|--------|------|",
                f"| ≥ {mono.separation_threshold:.4g} | {mono.n_above} | {mono.wr_above:.2%} |",
                f"| < {mono.separation_threshold:.4g} | {mono.n_below} | {mono.wr_below:.2%} |",
                "",
                f"**胜率差**: {mono.separation:+.2%}  "
                f"(χ² p={mono.separation_p_value:.4f})",
                "",
            ])
            lines.append(f"**判定**: {mono.interpretation}")
            lines.append("")

        # --- 单调性图表 ---
        lines.append("### 单调性图表")
        lines.append("")
        chart_path = generate_monotonicity_chart(df, feature, fv, output_dir)
        if chart_path:
            lines.append(f"![{feature.name} 单调性]({chart_path})")
            lines.append("")
        else:
            lines.append("_数据不足，无法生成图表_")
            lines.append("")

        # --- 最佳区间 ---
        best_bucket = _find_best_bucket(buckets)
        if best_bucket:
            lines.extend([
                "### 最佳区间",
                "",
                f"- **特征值区间**: {best_bucket.bucket_label}",
                f"- **样本数**: {best_bucket.samples}",
                f"- **胜率**: {best_bucket.win_rate:.2%}",
                f"- **均价**: {best_bucket.avg_price:.4f}",
                f"- **平均盈亏**: {best_bucket.mean_pnl:+.4f}",
                "",
            ])

        lines.append("---")
        lines.append("")

    # --- 特征组合分析 ---
    lines.extend([
        "## 特征组合分析 (2D)",
        "",
        "_对选定的特征对进行 4×4 分位数网格分析，每格显示胜率和平均盈亏。_",
        "",
    ])

    pairs = [
        (0, 1),   # DistanceToStrike × SafetyRatio
        (2, 3),   # ReversalCapacity × VolatilityExpansion
        (0, 4),   # DistanceToStrike × DirectionPersistence
        (1, 2),   # SafetyRatio × ReversalCapacity
        (0, 5),   # DistanceToStrike × CumulativeBuyPct
        (0, 6),   # DistanceToStrike × ImbalanceTrend
    ]

    for ai, bi in pairs:
        if ai >= len(FEATURES) or bi >= len(FEATURES):
            continue
        fa, fb = FEATURES[ai], FEATURES[bi]

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
                f"**最佳格子**: {result.best_cell['bucket_a']} × {result.best_cell['bucket_b']} "
                f"— 胜率={result.best_cell['win_rate']:.2%}, "
                f"平均盈亏={result.best_cell['mean_pnl']:+.4f}, "
                f"n={result.best_cell['samples']}"
            )
            lines.append("")

        if result.worst_cell:
            lines.append(
                f"**最差格子**: {result.worst_cell['bucket_a']} × {result.worst_cell['bucket_b']} "
                f"— 胜率={result.worst_cell['win_rate']:.2%}, "
                f"平均盈亏={result.worst_cell['mean_pnl']:+.4f}, "
                f"n={result.worst_cell['samples']}"
            )
            lines.append("")

    # --- 特征单调性汇总 ---
    lines.extend([
        "---",
        "",
        "## 特征预测力汇总",
        "",
        "_综合 Spearman 单调性和分组分离两种分析方法。"
        "扫尾盘关注的是胜率一致性——无论是单调递增还是分类有效都有意义。_",
        "",
        "| # | 特征 | 方法 | ρ vs 胜率 | 分离阈值 | 胜率差 | p值 | 方向 | 判定 |",
        "|---|------|------|----------|---------|--------|------|------|------|",
    ])

    # Sort: predictive features first, then by strength
    def _sort_key(kv):
        _, mono = kv
        if mono.is_predictive:
            return (0, -abs(mono.separation) if mono.verdict_method == "separation"
                    else -abs(mono.spearman_r_won))
        return (1, -abs(mono.spearman_r_won))

    sorted_features = sorted(mono_results.items(), key=_sort_key)

    for j, (name, mono) in enumerate(sorted_features, 1):
        if mono.verdict_method == "spearman":
            method = "Spearman"
            direction = (
                "正向" if mono.spearman_r_won > 0
                else "反向" if mono.spearman_r_won < 0
                else "无"
            )
            sep_info = "—"
            sep_pval = "—"
        elif mono.verdict_method == "separation":
            method = "分组分离"
            direction = (
                "正向" if mono.separation > 0
                else "反向" if mono.separation < 0
                else "无"
            )
            sep_info = f"{mono.separation:+.2%}"
            sep_pval = f"{mono.separation_p_value:.4f}"
        else:
            method = "—"
            direction = "—"
            sep_info = "—"
            sep_pval = "—"

        lines.append(
            f"| {j} | {name} | {method} | {mono.spearman_r_won:+.4f} | "
            f"{mono.separation_threshold:.4g} | {sep_info} | "
            f"{sep_pval} | {direction} | **{mono.interpretation}** |"
        )

    lines.append("")

    return "\n".join(lines)


def _find_best_bucket(buckets: list[BucketResult]) -> BucketResult | None:
    """找到胜率最高的桶（最少 10 个样本）。"""
    valid = [b for b in buckets if b.samples >= 10]
    if not valid:
        return None
    return max(valid, key=lambda b: b.win_rate)
