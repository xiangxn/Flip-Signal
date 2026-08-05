"""
Feature 2: SafetyRatio (PRD2 §7 Feature 2)

Is the current edge large enough relative to recent market noise?

SafetyRatio = DistanceInFavor / RecentVolatility

Higher values mean the current directional move is statistically significant
relative to the market's typical second-to-second fluctuations.
"""

import numpy as np
import pandas as pd

from features.base import Feature


class SafetyRatio(Feature):
    name = "safety_ratio"
    description = (
        "偏离距离 / 近期波动率 — 当前优势相对于市场噪声的显著性，值越大越可靠"
    )

    def compute(self, df: pd.DataFrame) -> pd.Series:
        """Compute SafetyRatio.

        RecentVolatility = sum(abs(return_1s)) over last 30 seconds,
        as recommended by PRD2. This is more robust than std for
        short windows with potential outliers.
        """
        distance = np.abs((df["price"] - df["open"]) / df["open"])

        # Sum of absolute returns over 6-tick rolling window (~30s at 5s ticks)
        recent_vol = (
            df.groupby("condition_id")["ret_10s"]
            .transform(lambda x: x.rolling(6, min_periods=2).apply(lambda w: np.abs(w).sum(), raw=True))
        )

        safety = distance / recent_vol.replace(0, np.nan)
        return safety.fillna(0)
