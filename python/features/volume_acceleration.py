"""
Feature 7: VolumeAcceleration (PRD2 §7 Feature 7)

Is trading volume accelerating or decelerating?

VolumeAcceleration = Volume(last 10s) / Volume(previous 10s)

Values > 1 indicate increasing market participation — often accompanies
strong directional moves or impending reversals.
"""

import numpy as np
import pandas as pd

from features.base import Feature


class VolumeAcceleration(Feature):
    name = "volume_acceleration"
    description = (
        "Volume(last 10s) / Volume(previous 10s) — "
        "volume momentum (>1 = accelerating)"
    )

    def compute(self, df: pd.DataFrame) -> pd.Series:
        """Compute VolumeAcceleration.

        Total volume per second = BuyVolume1s + SellVolume1s.
        Compares the sum over last 10s vs previous 10s.
        """
        total_vol = df["buy_vol_1s"] + df["sell_vol_1s"]

        # Per event: rolling sum of last 10s and previous 10s
        results = []
        for _condition_id, group in df.groupby("condition_id"):
            vol = group["buy_vol_1s"] + group["sell_vol_1s"]

            vol_10s = vol.rolling(10, min_periods=3).sum()
            vol_prev_10s = vol.shift(10).rolling(10, min_periods=3).sum()

            accel = vol_10s / vol_prev_10s.replace(0, np.nan)
            results.append(accel.fillna(1.0))  # neutral default = 1.0

        if results:
            return pd.concat(results).reindex(df.index)
        return pd.Series(1.0, index=df.index)
