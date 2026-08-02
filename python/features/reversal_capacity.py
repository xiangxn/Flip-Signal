"""
Feature 3: ReversalCapacity (PRD2 §7 Feature 3)

Given the remaining time, how likely is the market to reverse and erase our edge?

ReversalCapacity = RecentMaxMove / DistanceInFavor

LOWER values are safer — the market hasn't shown the capacity to move
enough to erase the current advantage.
"""

import numpy as np
import pandas as pd

from features.base import Feature


class ReversalCapacity(Feature):
    name = "reversal_capacity"
    description = (
        "RecentMaxMove / DistanceInFavor — "
        "market's recent burst capacity relative to our edge (lower = safer)"
    )

    def compute(self, df: pd.DataFrame) -> pd.Series:
        """Compute ReversalCapacity.

        RecentMaxMove = max absolute price change over last 30 seconds,
        measured as (max(price) - min(price)) / price at start of window.
        """
        distance = np.abs((df["price"] - df["open"]) / df["open"])

        # Max price range over 30s rolling window, normalized
        def _max_move(window: np.ndarray) -> float:
            if len(window) < 2:
                return 0.0
            return float((window.max() - window.min()) / window[0]) if window[0] > 0 else 0.0

        recent_max_move = (
            df.groupby("condition_id")["price"]
            .transform(lambda x: x.rolling(30, min_periods=5).apply(_max_move, raw=True))
        )

        ratio = recent_max_move / distance.replace(0, np.nan)
        return ratio.fillna(0)
