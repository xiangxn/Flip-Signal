"""
Feature 4: VolatilityExpansion (PRD2 §7 Feature 4)

Is the market entering an abnormally volatile state?

VolatilityExpansion = CurrentVolatility(10s) / HistoricalVolatility(expanding)

CurrentVolatility = std of 1s returns over last 10s (pre-computed vol_10s)
HistoricalVolatility = expanding std of 1s returns from event start to now

Values > 1 indicate current volatility is ABOVE the event's historical baseline
— the market is more agitated than usual, tail risk increases.
Values < 1 indicate current volatility is BELOW the historical baseline
— the market is calmer than usual, tail sweeping is safer.

Why not Vol10s/Vol60s? Both are short windows. A ratio near 1.0 could mean
both are low (calm) OR both are high (agitated) — the ratio alone can't tell
them apart. The expanding historical baseline fixes this: if the whole event
has been volatile, the baseline is high, and even elevated current vol will
show a modest ratio. If the event has been calm and suddenly spikes, the
ratio will be large.
"""

import numpy as np
import pandas as pd

from features.base import Feature


class VolatilityExpansion(Feature):
    name = "volatility_expansion"
    description = (
        "当前波动率(10s) / 历史波动率(expanding) — "
        "当前波动达到历史水平的多少倍（>1 = 比历史更躁动，尾部风险上升）"
    )

    def compute(self, df: pd.DataFrame) -> pd.Series:
        """Compute VolatilityExpansion.

        Current = vol_10s (pre-computed std of 1s returns over 10s)
        Historical = expanding std of ret_1s from event start (min 30s)
        Ratio = current / historical

        Values well below 1 = market is calmer than its history → safer tail.
        Values well above 1 = market is spiking vs its history → dangerous tail.
        """
        vol_10s = df["vol_10s"]

        # Expanding historical volatility from event start
        # min_periods=30 ensures we have enough data for a meaningful baseline
        hist_vol = (
            df.groupby("condition_id")["ret_1s"]
            .transform(lambda x: x.expanding(min_periods=30).std())
        )

        expansion = vol_10s / hist_vol.replace(0, np.nan)
        return expansion.fillna(1.0)  # neutral default = 1.0
