"""Pull a Liga Endesa season from the official ACB API.

Examples:
    python scripts/pull_acb.py --season 2025-26
    python scripts/pull_acb.py --season 2024-25 --with-pbp
    python scripts/pull_acb.py --season 2025-26 --with-pbp --output data/acb_raw

Requires:
    ACB_BEARER_TOKEN
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from acb.client import ACBAPIError, ACBClient  # noqa: E402


def _match_id(row: dict[str, Any]) -> int | None:
    for key in ("idMatch", "id_match", "id"):
        value = row.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull official ACB / Liga Endesa data")
    parser.add_argument(
        "--season",
        required=True,
        help="Season such as 2025-26 or 2024-25",
    )
    parser.add_argument(
        "--output",
        default="data/acb_raw",
        help="Base output directory (default: data/acb_raw)",
    )
    parser.add_argument(
        "--with-pbp",
        action="store_true",
        help="Also pull play-by-play for every discovered match",
    )
    args = parser.parse_args()

    client = ACBClient()
    season_dir = Path(args.output) / args.season

    try:
        weeks = client.matchweeks(args.season)
        _write_json(season_dir / "matchweeks.json", weeks)

        matches = client.season_matches(args.season)
        _write_json(season_dir / "matches.json", matches)

        pulled_pbp = 0
        skipped = 0

        if args.with_pbp:
            pbp_dir = season_dir / "play_by_play"
            for match in matches:
                match_id = _match_id(match)
                if match_id is None:
                    skipped += 1
                    continue

                try:
                    payload = client.play_by_play(match_id)
                except ACBAPIError as failure:
                    print(
                        f"ACB PBP failed for match {match_id}: {failure}",
                        file=sys.stderr,
                    )
                    skipped += 1
                    continue

                _write_json(pbp_dir / f"{match_id}.json", payload)
                pulled_pbp += 1

        summary = {
            "season": args.season,
            "edition_id": client.edition_id(args.season),
            "matches": len(matches),
            "play_by_play_files": pulled_pbp,
            "skipped": skipped,
            "output": str(season_dir),
        }
        _write_json(season_dir / "pull_summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    except (ACBAPIError, ValueError) as failure:
        print(f"ACB pull failed: {failure}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
