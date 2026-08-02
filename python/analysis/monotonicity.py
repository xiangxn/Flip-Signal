"""
Monotonicity Analysis (PRD2 §8.3)

Computes Spearman Rank correlation between feature values and win outcomes.
A positive, significant correlation means higher feature values predict
higher win rates — the feature is monotonically useful.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from features.base import Feature


@dataclass
class MonotonicityResult:
    spearman_r: float
    p_value: float
    samples: int
    interpretation: str


def analyze_monotonicity(
    df: pd.DataFrame,
    feature: Feature,
    feature_values: pd.Series,
) -> MonotonicityResult:
    """Compute Spearman Rank correlation between feature and win outcome.

    Uses the bet direction to determine win/loss per row.
    Spearman ρ close to +1 means the feature is a strong positive predictor.
    """
    distance = (df["price"] - df["open"]) / df["open"]
    bet_yes = distance > 0
    bet_no = distance < 0
    won = ((bet_yes & (df["outcome"] == 0)) | (bet_no & (df["outcome"] == 1))).astype(int)

    valid = (bet_yes | bet_no) & feature_values.notna() & won.notna()
    fv = feature_values[valid]
    w = won[valid]

    if len(fv) < 10:
        return MonotonicityResult(
            spearman_r=0, p_value=1.0, samples=len(fv),
            interpretation="Insufficient data",
        )

    r, p = spearmanr(fv, w)

    if p < 0.01 and r > 0.3:
        interp = "Strong positive predictor ✅"
    elif p < 0.05 and r > 0.1:
        interp = "Moderate positive predictor 🟡"
    elif p < 0.05 and r < -0.1:
        interp = "Inverse predictor (check direction) ⚠️"
    elif abs(r) < 0.1:
        interp = "Weak / no relationship ❌"
    else:
        interp = f"Not significant (p={p:.3f})"

    return MonotonicityResult(
        spearman_r=round(r, 4),
        p_value=round(p, 6),
        samples=len(fv),
        interpretation=interp,
    )
