"""
Feature 6: SignedFlow (PRD2 §7 Feature 6)

Is order flow aligned with the current price direction?

SignedFlow = (BuyVolume - SellVolume) / (BuyVolume + SellVolume)

Normalized to [-1, +1]. Positive when buying dominates, negative when selling
dominates. For feature analysis, we use direction-adjusted flow.
"""

import numpy as np
import pandas as pd

from features.base import Feature


class SignedFlow(Feature):
    name = "signed_flow"
    description = (
        "Normalized net order flow: (BuyVol - SellVol) / (BuyVol + SellVol) — "
        "direction-adjusted for analysis"
    )

    def compute(self, df: pd.DataFrame) -> pd.Series:
        """Compute direction-adjusted signed flow.

        Uses the pre-computed SignedFlow1s from the snapshot, then adjusts
        sign so positive flow = flow in the direction of the bet.

        Raw signed_flow_1s = BuyVolume1s - SellVolume1s.
        This normalizes by total volume and adjusts for bet direction.
        """
        raw_flow = df["signed_flow_1s"]
        total_vol = df["buy_vol_1s"] + df["sell_vol_1s"]

        # Normalize to [-1, 1]
        normalized = raw_flow / total_vol.replace(0, np.nan)

        # Direction-adjust: positive = flow in favor of current move
        distance = (df["price"] - df["open"]) / df["open"]
        direction = np.sign(distance)
        direction = direction.replace(0, 1)  # flat → assume positive

        adjusted = normalized * direction

        return adjusted.fillna(0)
