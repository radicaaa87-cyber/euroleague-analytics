"""Verify the disposable local ML warehouse and training SQL end to end."""

from __future__ import annotations

import json

import psycopg

from euroleague.incremental_confirmation import load_test_database_settings
from euroleague.model_training import training_dataset_sql

SEASONS = ("E2023", "E2024", "E2025")
EXPECTED_GAMES = {
    "E2023": 331,
    "E2024": 330,
    "E2025": 402,
}


def _count_by_season(cursor: psycopg.Cursor, relation: str) -> dict[str, int]:
    cursor.execute(
        f"""
        select season_code, count(*)::bigint
        from {relation}
        where season_code = any(%s)
        group by season_code
        order by season_code
        """,
        (list(SEASONS),),
    )
    return {str(season): int(count) for season, count in cursor.fetchall()}


def main() -> int:
    settings = load_test_database_settings()
    report: dict[str, object] = {}

    with psycopg.connect(settings.url(), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("set search_path to warehouse")

            games = _count_by_season(cursor, "v_game")
            player_games = _count_by_season(cursor, "v_player_game")
            possessions = _count_by_season(cursor, "v_possession")
            stints = _count_by_season(cursor, "lineup_stint")

            for season, expected in EXPECTED_GAMES.items():
                actual = games.get(season, 0)
                if actual != expected:
                    raise RuntimeError(
                        f"{season} local warehouse game count {actual}, expected {expected}."
                    )
                if player_games.get(season, 0) <= 0:
                    raise RuntimeError(f"{season} has no player-game rows.")
                if possessions.get(season, 0) <= 0:
                    raise RuntimeError(f"{season} has no possession rows.")
                if stints.get(season, 0) <= 0:
                    raise RuntimeError(f"{season} has no lineup-stint rows.")

            sql = training_dataset_sql("official")

            cursor.execute("set statement_timeout = '45min'")
            cursor.execute(
                "explain (costs off, verbose false) " + sql,
                (list(SEASONS), 3),
            )
            plan_lines = [row[0] for row in cursor.fetchall()]
            if not plan_lines:
                raise RuntimeError("Training SQL produced no EXPLAIN plan.")

            cursor.execute(
                "select season_code, count(*)::bigint "
                "from (" + sql + ") training_rows "
                "group by season_code order by season_code",
                (list(SEASONS), 3),
            )
            training_rows = {
                str(season): int(count)
                for season, count in cursor.fetchall()
            }
            missing_training = [
                season for season in SEASONS
                if training_rows.get(season, 0) <= 0
            ]
            if missing_training:
                raise RuntimeError(
                    "Training SQL returned no rows for: "
                    + ", ".join(missing_training)
                )

            cursor.execute("select count(*)::bigint from acb_player_game")
            acb_player_games = int(cursor.fetchone()[0])
            if acb_player_games <= 0:
                raise RuntimeError(
                    "Compact ACB player-game context was not copied into the local warehouse."
                )

            cursor.execute(
                "select count(*)::bigint from pregame_context_collection"
            )
            context_collections = int(cursor.fetchone()[0])

    report.update(
        {
            "seasons": list(SEASONS),
            "games": games,
            "player_games": player_games,
            "possessions": possessions,
            "lineup_stints": stints,
            "training_rows": training_rows,
            "acb_player_games": acb_player_games,
            "pregame_context_collections": context_collections,
            "training_sql_explain_lines": len(plan_lines),
            "status": "ok",
        }
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
