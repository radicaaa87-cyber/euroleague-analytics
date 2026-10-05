#!/usr/bin/env python3
"""Export leakage-safe player-points training features with one DB query."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from euroleague.model_training import export_training_dataset


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export compact player-game features directly from PostgreSQL. "
            "This bypasses ChatGPT/MCP paging and row-budget consumption."
        )
    )
    parser.add_argument(
        "--season",
        action="append",
        required=True,
        dest="seasons",
        help="Season code to include. Repeat for multiple seasons.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Destination CSV path.",
    )
    parser.add_argument(
        "--minutes-basis",
        choices=("official", "corrected", "raw"),
        default="official",
    )
    parser.add_argument(
        "--min-history-games",
        type=int,
        default=3,
        help="Minimum prior games required for a training row (1-10).",
    )
    args = parser.parse_args()

    result = export_training_dataset(
        seasons=args.seasons,
        output_path=args.output,
        minutes_basis=args.minutes_basis,
        min_history_games=args.min_history_games,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
