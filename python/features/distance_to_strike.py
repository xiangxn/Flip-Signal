"""
Feature 1: DistanceToStrike (PRD2 §7 Feature 1)

How far is the current price from the open price, normalized by open price.
Direction-normalized: always positive when the market moves in the bet direction.
"""

import numpy as np
import pandas as pd

from features.base import Feature


class DistanceToStrike(Feature):
    name = "distance_to_strike"
    description = (
        "|CurrentPrice - OpenPrice| / OpenPrice — "
        "direction-normalized distance from strike"
    )

    def compute(self, df: pd.DataFrame) -> pd.Series:
        """Compute DistanceInFavor for each snapshot.

        The bet direction is inferred from the current price vs open:
          - price > open → bet YES → DistanceInFavor = +(price-open)/open
          - price < open → bet NO  → DistanceInFavor = +(open-price)/open

        Always non-negative; larger means the market has moved further
        in a direction that could be bet upon.
        """
        distance = (df["price"] - df["open"]) / df["open"]
        return np.abs(distance)
