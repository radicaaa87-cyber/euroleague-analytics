"""Print structured 72h role context for one upcoming player-game."""

from __future__ import annotations

import argparse
import json

import psycopg

from euroleague.config import DatabaseSettings
from euroleague.pregame_context import load_role_context_features


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", required=True)
    parser.add_argument("--gamecode", required=True, type=int)
    parser.add_argument("--player-id", required=True)
    args = parser.parse_args(argv)

    settings = DatabaseSettings.from_env()
    with psycopg.connect(
        settings.url(),
        autocommit=True,
        prepare_threshold=None,
    ) as connection:
        features = load_role_context_features(
            connection,
            season_code=args.season,
            gamecode=args.gamecode,
            player_id=args.player_id,
        )

    print(json.dumps(features, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
