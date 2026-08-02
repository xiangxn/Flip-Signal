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
        "DistanceInFavor / RecentVolatility — "
        "how many 'noise units' of advantage we have"
    )

    def compute(self, df: pd.DataFrame) -> pd.Series:
        """Compute SafetyRatio.

        RecentVolatility = sum(abs(return_1s)) over last 30 seconds,
        as recommended by PRD2. This is more robust than std for
        short windows with potential outliers.
        """
        distance = np.abs((df["price"] - df["open"]) / df["open"])

        # Sum of absolute 1s returns over 30s rolling window
        recent_vol = (
            df.groupby("condition_id")["ret_1s"]
            .transform(lambda x: x.rolling(30, min_periods=5).apply(lambda w: np.abs(w).sum(), raw=True))
        )

        safety = distance / recent_vol.replace(0, np.nan)
        return safety.fillna(0)
