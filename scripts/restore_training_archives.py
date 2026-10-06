"""Restore complete training seasons from the immutable Supabase archive.

This command reads archive metadata and Storage only. It never loads historical
rows into the hosted warehouse. The restored cache is intended for the
disposable local training database used by the ML workflow.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import psycopg

from euroleague.archive import SupabaseStorage, restore_current_season_cache
from euroleague.cache import ResponseCache
from euroleague.config import live_runtime_settings
from euroleague.fetch import validate_season_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Restore complete archived EL seasons for local ML training."
    )
    parser.add_argument("seasons", nargs="+", help="season codes, e.g. E2023 E2024 E2025")
    parser.add_argument("--cache-dir", type=Path, default=Path(".training-cache"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    seasons = [validate_season_code(value.strip().upper()) for value in args.seasons]
    cache = ResponseCache(args.cache_dir)
    database_settings, storage_settings = live_runtime_settings(os.environ)
    storage = SupabaseStorage(storage_settings)

    with psycopg.connect(database_settings.url(), autocommit=True) as connection:
        for season in seasons:
            restored = restore_current_season_cache(
                connection,
                cache,
                storage,
                season,
                allow_bootstrap=False,
                allow_incomplete=False,
            )
            if restored.completeness is None:
                raise RuntimeError(f"{season} restored without a completeness record.")
            print(
                f"{season}: restored={restored.restored_responses} "
                f"bytes={restored.exact_bytes} "
                f"played_games={restored.completeness.played_games}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
