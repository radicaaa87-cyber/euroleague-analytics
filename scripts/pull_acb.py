"""Pull one ACB season from the current live.acb.com API."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from acb.client import ACBAPIError, ACBClient  # noqa: E402

def _id(row: dict[str, Any]) -> int | None:
    value = row.get("id") or row.get("idMatch") or row.get("id_match")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None

def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--season", required=True)
    p.add_argument("--output", default="data/acb_raw")
    p.add_argument("--with-pbp", action="store_true")
    p.add_argument("--with-shots", action="store_true")
    p.add_argument("--with-advanced", action="store_true")
    p.add_argument("--no-boxscores", action="store_true")
    args = p.parse_args()

    client = ACBClient()
    out = Path(args.output) / args.season

    try:
        matches = client.season_matches(args.season)
        _write(out / "matches.json", matches)
        counts = {"boxscore_files":0,"play_by_play_files":0,"shot_files":0,"advanced_files":0,"skipped":0}

        for match in matches:
            match_id = _id(match)
            if match_id is None:
                counts["skipped"] += 1
                continue

            calls = []
            if not args.no_boxscores:
                calls.append(("boxscores", client.boxscore(args.season, match_id), "boxscore_files"))
            if args.with_pbp:
                calls.append(("play_by_play", client.play_by_play(match_id), "play_by_play_files"))
            if args.with_shots:
                calls.append(("shots", client.shots(match_id), "shot_files"))
            if args.with_advanced:
                calls.append(("advanced", client.advanced_stats(match_id), "advanced_files"))

            for folder, payload, counter in calls:
                _write(out / folder / f"{match_id}.json", payload)
                counts[counter] += 1

        summary = {
            "season": args.season,
            "edition_id": client.edition_id(args.season),
            "matches": len(matches),
            **counts,
            "output": str(out),
        }
        _write(out / "pull_summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except (ACBAPIError, ValueError) as exc:
        print(f"ACB pull failed: {exc}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
