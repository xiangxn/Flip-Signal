"""
Feature base class and registry.

Each Feature computes a per-snapshot value from the raw snapshot DataFrame.
Features are registered in FEATURES for automatic discovery by analysis and
report modules.
"""

from abc import ABC, abstractmethod

import pandas as pd


class Feature(ABC):
    """Abstract base class for a research feature.

    Subclasses must set `name` and `description` class attributes,
    and implement `compute(df) -> pd.Series`.
    """

    name: str
    description: str

    @abstractmethod
    def compute(self, df: pd.DataFrame) -> pd.Series:
        """Compute feature values for every snapshot.

        Parameters
        ----------
        df : pd.DataFrame
            One row per snapshot, as returned by loader.load_events().

        Returns
        -------
        pd.Series
            Feature value for each row, same index as df.
        """
        ...


# Registry of all features — populated by subclasses.
FEATURES: list[Feature] = []
