"""
Event loader — reads JSONL event files produced by cmd/lab into a pandas DataFrame.

Each line in a JSONL file is a JSON-encoded Event. Each Event contains
~300 ResearchSnapshots. The loader flattens everything into one row per
snapshot, with event-level fields (outcome, close_price) denormalized.

Usage:
    from loader import load_events
    df = load_events("data/lab/")
"""

import json
from pathlib import Path

import pandas as pd


def load_events(data_dir: str) -> pd.DataFrame:
    """Load all events from JSONL files into a DataFrame.

    Parameters
    ----------
    data_dir : str
        Path to the directory containing events_*.json or events_*.jsonl files.

    Returns
    -------
    pd.DataFrame
        One row per snapshot. Columns include all ResearchSnapshot fields
        plus event-level fields: condition_id, outcome, close_price.
    """
    rows: list[dict] = []
    paths = sorted(Path(data_dir).glob("events_*.json*"))

    if not paths:
        raise FileNotFoundError(f"No events_*.json* files found in {data_dir}")

    for path in paths:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)

                condition_id = event["condition_id"]
                outcome = event["outcome"]
                close_price = event["close_price"]

                for snap in event["snapshots"]:
                    snap["condition_id"] = condition_id
                    snap["outcome"] = outcome
                    snap["close_price"] = close_price
                    rows.append(snap)

    df = pd.DataFrame(rows)

    # Ensure correct types
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df["outcome"] = df["outcome"].astype("int8")

    return df


def event_count(df: pd.DataFrame) -> int:
    """Return the number of unique events in the DataFrame."""
    return df["condition_id"].nunique()


def snapshot_count(df: pd.DataFrame) -> int:
    """Return the total number of snapshots."""
    return len(df)


def summary(df: pd.DataFrame) -> dict:
    """Return a summary dict with key statistics."""
    # outcome: 0=Up(YES), 1=Down(NO)
    yes_events = int((df.groupby("condition_id")["outcome"].first() == 0).sum())
    no_events = df["condition_id"].nunique() - yes_events
    return {
        "events": df["condition_id"].nunique(),
        "snapshots": len(df),
        "yes_events": yes_events,
        "no_events": no_events,
        "yes_ratio": yes_events / max(df["condition_id"].nunique(), 1),
        "date_range": f"{df['ts'].min()} → {df['ts'].max()}",
    }
