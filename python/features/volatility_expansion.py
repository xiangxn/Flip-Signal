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
        "Volatility(10s) / Volatility(60s) — "
        "short-term vs medium-term volatility ratio (>1 = escalating)"
    )

    def compute(self, df: pd.DataFrame) -> pd.Series:
        """Compute VolatilityExpansion.

        Volatility60s is computed from the 1s returns over a 60s rolling window,
        matching the same std-dev methodology as the pre-computed Volatility10s.
        """
        vol_10s = df["vol_10s"]

        # Compute 60s volatility from 1s returns
        vol_60s = (
            df.groupby("condition_id")["ret_1s"]
            .transform(lambda x: x.rolling(60, min_periods=10).std())
        )

        expansion = vol_10s / vol_60s.replace(0, np.nan)
        return expansion.fillna(1.0)  # neutral default = 1.0
