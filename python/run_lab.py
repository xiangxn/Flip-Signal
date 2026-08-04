#!/usr/bin/env python3
"""
Feature Research Lab — Main Entry Point

Loads event data from JSONL files, computes all 7 research features,
runs the analysis pipeline, and generates a Markdown report.

Usage:
    python run_lab.py --data ../data/lab/ --output ../reports/

Setup (first time):
    python -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from features import FEATURES
from loader import load_events, summary
from report.generator import generate_report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Feature Research Lab — analyze BTC 5-min event data",
    )
    parser.add_argument(
        "--data", default="../data/",
        help="Directory containing events_*.jsonl files (default: ../data/)",
    )
    parser.add_argument(
        "--output", default="../reports/",
        help="Directory for output reports (default: ../reports/)",
    )
    parser.add_argument(
        "--list-features", action="store_true",
        help="List registered features and exit",
    )
    args = parser.parse_args()

    if args.list_features:
        print("Registered features:")
        for i, f in enumerate(FEATURES, 1):
            print(f"  {i}. {f.name:30s} — {f.description}")
        return

    # Resolve paths relative to this script
    script_dir = Path(__file__).resolve().parent
    data_dir = (script_dir / args.data).resolve() if not os.path.isabs(args.data) else Path(args.data).resolve()
    output_dir = (script_dir / args.output).resolve() if not os.path.isabs(args.output) else Path(args.output).resolve()

    print(f"Data dir:  {data_dir}")
    print(f"Output dir: {output_dir}")

    # Load
    print("Loading events...")
    try:
        df = load_events(str(data_dir))
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    s = summary(df)
    print(f"  Events: {s['events']} ({s['yes_events']} YES, {s['no_events']} NO)")
    print(f"  Snapshots: {s['snapshots']:,}")
    print(f"  Date range: {s['date_range']}")

    if s["events"] < 10:
        print("Warning: fewer than 10 events — analysis may not be meaningful.")

    # Compute features
    print(f"Computing {len(FEATURES)} features...")
    feature_cache = {}
    for i, feature in enumerate(FEATURES, 1):
        print(f"  [{i}/{len(FEATURES)}] {feature.name}...")
        feature_cache[feature.name] = feature.compute(df)

    # Generate report
    print("Generating report...")
    output_dir.mkdir(parents=True, exist_ok=True)
    report = generate_report(df, str(output_dir))

    # Write report
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = output_dir / f"feature_report_{timestamp}.md"
    report_path.write_text(report, encoding="utf-8")

    print(f"\nReport written to: {report_path}")
    print(f"Size: {report_path.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
