"""
Feature 4: VolatilityExpansion (PRD2 §7 Feature 4)

Is the market entering an abnormally volatile state?

VolatilityExpansion = Volatility10s / Volatility60s

Values > 1 indicate short-term volatility is accelerating above the
longer-term baseline — tail risk increases.
"""

import numpy as np
import pandas as pd

from features.base import Feature


class VolatilityExpansion(Feature):
    name = "volatility_expansion"
    description = (
        "短期波动率(10s) / 中期波动率(60s) — 波动是否在急剧放大（>1 = 市场躁动，尾部风险上升）"
    )

    def compute(self, df: pd.DataFrame) -> pd.Series:
        """Compute VolatilityExpansion.

        Volatility60s is computed from the 1s returns over a 60s rolling window,
        matching the same std-dev methodology as the pre-computed Volatility10s.
        """
        vol_10s = df["vol_10s"]

        # Compute 60s volatility = 12-tick rolling std (~60s at 5s ticks)
        vol_60s = (
            df.groupby("condition_id")["ret_1s"]
            .transform(lambda x: x.rolling(12, min_periods=3).std())
        )

        expansion = vol_10s / vol_60s.replace(0, np.nan)
        return expansion.fillna(1.0)  # neutral default = 1.0
