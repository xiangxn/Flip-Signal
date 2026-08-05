"""
Feature 5: DirectionPersistence (PRD2 §7 Feature 5)

Is the current direction sustained or choppy?

DirectionPersistence = proportion of 1s returns in the last N seconds
that share the same sign as the cumulative return from open.

High persistence → trend-like behavior. Low persistence → choppy/reversing.
"""

import numpy as np
import pandas as pd

from features.base import Feature


class DirectionPersistence(Feature):
    name = "direction_persistence"
    description = (
        "过去20秒内与开盘方向一致的1秒回报占比 — 趋势是否整齐划一（高 = 趋势流畅，低 = 来回拉锯）"
    )

    def compute(self, df: pd.DataFrame) -> pd.Series:
        """Compute DirectionPersistence.

        Looks back 4 ticks (~20s at 5s ticks). For each snapshot, counts
        how many of the last 4 returns have the same sign as the return-from-open.
        """
        return_from_open = (df["price"] - df["open"]) / df["open"]
        direction = np.sign(return_from_open)

        def _aligned_fraction(window_returns: np.ndarray, window_dir: float) -> float:
            if len(window_returns) < 2:
                return 0.5  # neutral
            aligned = np.sum(np.sign(window_returns) == window_dir)
            return float(aligned / len(window_returns))

        # Apply per event
        results = []
        for condition_id, group in df.groupby("condition_id"):
            returns = group["ret_10s"].values
            dirs = direction.loc[group.index].values

            persistence = np.full(len(group), 0.5)
            for i in range(len(group)):
                start = max(0, i - 3)  # look back 4 ticks
                window = returns[start : i + 1]
                persistence[i] = _aligned_fraction(window, dirs[i])

            results.append(pd.Series(persistence, index=group.index))

        if results:
            return pd.concat(results).reindex(df.index)
        return pd.Series(0.5, index=df.index)
