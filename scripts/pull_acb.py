"""Pull one ACB season from the current live.acb.com API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from acb.client import ACBAPIError, ACBClient


def _id(row: dict[str, Any]) -> int | None:
    value = row.get("id") or row.get("idMatch") or row.get("id_match")
    try:
        return int(value) if value is not None else None
    except TypeError, ValueError:
        return None


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--season", required=True)
    p.add_argument("--output", default="data/acb_raw")
    p.add_argument("--with-pbp", action="store_true")
    p.add_argument("--with-shots", action="store_true")
    p.add_argument("--with-advanced", action="store_true")
    p.add_argument("--no-boxscores", action="store_true")
    p.add_argument("--workers", type=int, default=int(os.getenv("ACB_PULL_WORKERS", "6")))
    args = p.parse_args()

    client = ACBClient()
    out = Path(args.output) / args.season

    try:
        matches = client.season_matches(args.season)
        _write(out / "matches.json", matches)
        counts = {
            "boxscore_files": 0,
            "play_by_play_files": 0,
            "shot_files": 0,
            "advanced_files": 0,
            "skipped": 0,
        }

        endpoint_errors: list[dict[str, Any]] = []
        jobs: list[tuple[int, str, str]] = []

        for match in matches:
            match_id = _id(match)
            if match_id is None:
                counts["skipped"] += 1
                continue
            if not args.no_boxscores:
                jobs.append((match_id, "boxscores", "boxscore_files"))
            if args.with_pbp:
                jobs.append((match_id, "play_by_play", "play_by_play_files"))
            if args.with_shots:
                jobs.append((match_id, "shots", "shot_files"))
            if args.with_advanced:
                jobs.append((match_id, "advanced", "advanced_files"))

        def fetch_one(job: tuple[int, str, str]) -> tuple[int, str, str, Any]:
            match_id, folder, counter = job
            target = out / folder / f"{match_id}.json"
            if target.exists() and target.stat().st_size > 20:
                return match_id, folder, counter, None
            if folder == "boxscores":
                payload = client.boxscore(args.season, match_id)
            elif folder == "play_by_play":
                payload = client.play_by_play(match_id)
            elif folder == "shots":
                payload = client.shots(match_id)
            elif folder == "advanced":
                payload = client.advanced_stats(match_id)
            else:
                raise ValueError(f"Unknown ACB endpoint folder: {folder}")
            return match_id, folder, counter, payload

        total_jobs = len(jobs)
        completed_jobs = 0
        workers = max(1, min(args.workers, 10))
        print(
            f"ACB: {len(matches)} matches, {total_jobs} endpoint jobs, workers={workers}",
            flush=True,
        )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {pool.submit(fetch_one, job): job for job in jobs}
            for future in as_completed(future_map):
                match_id, folder, counter = future_map[future]
                try:
                    _, _, _, payload = future.result()
                    target = out / folder / f"{match_id}.json"
                    if payload is not None:
                        _write(target, payload)
                    if target.exists():
                        counts[counter] += 1
                except (ACBAPIError, ValueError, OSError) as exc:
                    endpoint_errors.append(
                        {
                            "match_id": match_id,
                            "endpoint": folder,
                            "error": str(exc),
                        }
                    )
                completed_jobs += 1
                if completed_jobs % 50 == 0 or completed_jobs == total_jobs:
                    print(
                        f"ACB progress: {completed_jobs}/{total_jobs} endpoint jobs; errors={len(endpoint_errors)}",
                        flush=True,
                    )

        summary = {
            "season": args.season,
            "edition_id": client.edition_id(args.season),
            "matches": len(matches),
            **counts,
            "failed_requests": len(endpoint_errors),
            "endpoint_errors": endpoint_errors,
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
