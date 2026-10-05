"""Load one fully archived historical EuroLeague season into the hosted warehouse.

This is intentionally a one-season, additive path. It restores the season from
the private immutable archive, requires the cache to be complete, then reuses
the same idempotent incremental writer as the live season. Existing games and
other seasons are never replaced.

Example:
    python scripts/load_hosted_historical.py E2023
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

import psycopg

from euroleague.archive import (
    SupabaseStorage,
    assert_complete_played_cache,
    restore_for_resume,
)
from euroleague.cache import ResponseCache
from euroleague.config import live_runtime_settings
from euroleague.fetch import validate_season_code
from euroleague.live import run_live_pipeline

LIVE_SEASONS = {"E2026", "SC2026"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Restore one complete historical season from the private archive and "
            "add it idempotently to the hosted warehouse."
        )
    )
    parser.add_argument("season", help="Historical EuroLeague season code, e.g. E2023")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    season_code = validate_season_code(args.season.strip().upper())
    if season_code in LIVE_SEASONS:
        raise SystemExit(
            f"{season_code} is a live season and must use the normal live pipeline."
        )

    database_settings, storage_settings = live_runtime_settings(os.environ)

    with tempfile.TemporaryDirectory(prefix=f"{season_code.lower()}-hosted-backfill-") as root:
        cache = ResponseCache(Path(root) / "cache")
        storage = SupabaseStorage(storage_settings)

        with psycopg.connect(database_settings.url(), autocommit=True) as connection:
            restored = restore_for_resume(
                connection,
                cache,
                storage,
                season_code,
            )
            if restored.bootstrap_required:
                raise RuntimeError(
                    f"{season_code} has no archived source responses; refusing hosted load."
                )

            # Do not permit a partial historical season to enter production.
            assert_complete_played_cache(cache, season_code)

            summary = run_live_pipeline(
                connection,
                cache,
                season_code,
            )

    print(summary.as_log_line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
