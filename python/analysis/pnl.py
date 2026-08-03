"""
Shared P&L computation using real Polymarket prices.

Instead of assuming even-money (50/50) payoffs, this module uses the actual
yes_price / no_price from each snapshot to compute realized profit or loss
per share.

Bet direction:
  - price > open → bet YES (buy YES token at yes_price)
  - price < open → bet NO  (buy NO token at no_price)

Win condition:
  - bet YES & outcome == 0 (UP won) → profit = 1 - yes_price
  - bet NO  & outcome == 1 (DOWN won) → profit = 1 - no_price
  - lose → loss = -entry_price
"""

import numpy as np
import pandas as pd


def compute_pnl(df: pd.DataFrame) -> pd.Series:
    """Compute per-snapshot realised P&L using Polymarket entry prices.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain: price, open, yes_price, no_price, outcome.
        outcome: 0 = Up(YES) won, 1 = Down(NO) won.

    Returns
    -------
    pd.Series
        P&L per share for each row where a bet direction can be determined.
        Rows where price == open (no direction) get NaN.
    """
    distance = (df["price"] - df["open"]) / df["open"]
    bet_yes = distance > 0
    bet_no = distance < 0
    flat = distance == 0

    won_yes = (df["outcome"] == 0)
    won_no = (df["outcome"] == 1)

    pnl = pd.Series(np.nan, index=df.index)

    # Bet YES: entry at yes_price
    yes_mask = bet_yes & ~flat
    pnl.loc[yes_mask & won_yes] = 1.0 - df.loc[yes_mask & won_yes, "yes_price"]
    pnl.loc[yes_mask & won_no] = -df.loc[yes_mask & won_no, "yes_price"]

    # Bet NO: entry at no_price
    no_mask = bet_no & ~flat
    pnl.loc[no_mask & won_no] = 1.0 - df.loc[no_mask & won_no, "no_price"]
    pnl.loc[no_mask & won_yes] = -df.loc[no_mask & won_yes, "no_price"]

    return pnl


def compute_won(df: pd.DataFrame) -> pd.Series:
    """Compute per-snapshot win/loss (1=win, 0=loss) for the current direction.

    Useful as a classification target alongside compute_pnl.
    """
    distance = (df["price"] - df["open"]) / df["open"]
    bet_yes = distance > 0
    bet_no = distance < 0

    won = (bet_yes & (df["outcome"] == 0)) | (bet_no & (df["outcome"] == 1))
    return won.astype(int)
