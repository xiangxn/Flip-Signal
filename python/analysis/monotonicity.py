"""
Monotonicity Analysis (PRD2 §8.3)

Computes two types of predictive-power metrics for each feature:

  1. Spearman Rank correlation (continuous monotonicity)
     — feature vs won (0/1)  : does higher feature → higher win rate?
     — feature vs P&L (real) : does higher feature → higher profit?

  2. Separation analysis (binary classifier test)
     — For features with a natural threshold (signed_flow @ 0,
       volatility_expansion @ 1, volume_acceleration @ 1), splits at
       that threshold and compares win rates above vs below.
     — For others, uses median split.
     — Chi-squared test for statistical significance.

For tail-end sweeping, what matters is whether the feature helps filter
good trades from bad ones — either monotonically or as a classifier.
The verdict uses the STRONGER of the two signals.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import chi2_contingency, spearmanr

from analysis.pnl import compute_pnl, compute_won
from features.base import Feature

# Features with a natural semantic threshold (not just median).
# The threshold is meaningful: above = favourable, below = unfavourable.
NATURAL_THRESHOLDS: dict[str, float] = {
    "volatility_expansion": 1.0,  # <1 = calming, >1 = escalating
    "cumulative_buy_pct": 0.5,    # >0.5 = cumulative flow supports direction
    "imbalance_trend": 0.0,       # positive = depth supports direction
}


@dataclass
class MonotonicityResult:
    # --- Spearman (continuous monotonicity) ---
    spearman_r_won: float
    p_value_won: float
    spearman_r_pnl: float
    p_value_pnl: float

    # --- Separation (binary classifier test) ---
    separation_threshold: float = 0.0
    separation_is_natural: bool = False   # threshold is semantically meaningful
    wr_above: float = 0.0                 # win rate above threshold
    wr_below: float = 0.0                # win rate below threshold
    separation: float = 0.0              # wr_above - wr_below
    separation_p_value: float = 1.0      # chi-squared p-value
    n_above: int = 0
    n_below: int = 0

    samples: int = 0
    interpretation: str = ""
    is_predictive: bool = False
    # Which method produced the verdict
    verdict_method: str = ""  # "spearman" | "separation" | "both" | "none"


def analyze_monotonicity(
    df: pd.DataFrame,
    feature: Feature,
    feature_values: pd.Series,
) -> MonotonicityResult:
    """Compute both Spearman and separation analyses for a feature.

    Verdict logic:
      1. If Spearman ρ is significant → use Spearman verdict.
      2. If Spearman is weak but separation is significant AND uses a natural
         threshold → report as "分类有效".
      3. Otherwise → no relationship.
    """
    distance = (df["price"] - df["open"]) / df["open"]
    pnl = compute_pnl(df)
    won = compute_won(df)

    valid = (distance != 0) & feature_values.notna() & pnl.notna()
    fv = feature_values[valid]
    p = pnl[valid]
    w = won[valid]

    if len(fv) < 20:
        return MonotonicityResult(
            spearman_r_won=0, p_value_won=1.0,
            spearman_r_pnl=0, p_value_pnl=1.0,
            samples=len(fv),
            interpretation="数据不足",
            is_predictive=False,
            verdict_method="none",
        )

    # --- Spearman ---
    r_won, pv_won = spearmanr(fv, w)
    r_pnl, pv_pnl = spearmanr(fv, p)

    # --- Separation analysis ---
    # Use natural threshold if available, otherwise median
    thr = NATURAL_THRESHOLDS.get(feature.name, float(fv.median()))
    is_natural = feature.name in NATURAL_THRESHOLDS

    above = fv >= thr
    below = fv < thr

    n_above = int(above.sum())
    n_below = int(below.sum())

    wr_above = float(w[above].mean()) if n_above > 0 else 0.0
    wr_below = float(w[below].mean()) if n_below > 0 else 0.0
    separation = wr_above - wr_below

    # Chi-squared test for independence: feature-group × win/loss
    sep_pv = 1.0
    if n_above >= 5 and n_below >= 5:
        n_win_above = int(w[above].sum())
        n_win_below = int(w[below].sum())
        table = np.array([
            [n_win_above, n_above - n_win_above],
            [n_win_below, n_below - n_win_below],
        ])
        try:
            _, sep_pv, _, _ = chi2_contingency(table)
            sep_pv = float(sep_pv)
        except ValueError:
            sep_pv = 1.0
    else:
        sep_pv = 1.0

    # --- Verdict: use the stronger signal ---
    spearman_strong = (pv_won < 0.01 and abs(r_won) > 0.2)
    spearman_moderate = (pv_won < 0.05 and abs(r_won) > 0.1)
    separation_strong = (sep_pv < 0.01 and abs(separation) > 0.05) and is_natural
    separation_moderate = (sep_pv < 0.05 and abs(separation) > 0.03) and is_natural

    if spearman_strong:
        if r_won > 0:
            interp = "强正向预测 ✅"
        else:
            interp = "强反向预测 ⚠️"
        predictive = True
        method = "spearman"
    elif separation_strong and not spearman_moderate:
        # Strong separation signal even though Spearman is weak (e.g. signed_flow)
        interp = "分类有效 ✅"
        predictive = True
        method = "separation"
    elif spearman_moderate:
        if r_won > 0:
            interp = "中等正向预测 🟡"
        else:
            interp = "中等反向预测 ⚠️"
        predictive = True
        method = "spearman"
    elif separation_moderate:
        interp = "分类有效 🟡"
        predictive = True
        method = "separation"
    elif pv_won < 0.05 or sep_pv < 0.05:
        interp = "弱 / 无关系 ❌"
        predictive = False
        method = "none"
    else:
        interp = "弱 / 无关系 ❌"
        predictive = False
        method = "none"

    return MonotonicityResult(
        spearman_r_won=round(r_won, 4),
        p_value_won=round(pv_won, 6),
        spearman_r_pnl=round(r_pnl, 4),
        p_value_pnl=round(pv_pnl, 6),
        separation_threshold=round(thr, 4),
        separation_is_natural=is_natural,
        wr_above=round(wr_above, 4),
        wr_below=round(wr_below, 4),
        separation=round(separation, 4),
        separation_p_value=round(sep_pv, 6),
        n_above=n_above,
        n_below=n_below,
        samples=len(fv),
        interpretation=interp,
        is_predictive=predictive,
        verdict_method=method,
    )
