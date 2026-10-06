"""Copy compact production context into the disposable ML warehouse.

Historical EuroLeague rows are rebuilt locally from immutable archives. This
script adds only the small cross-competition/context tables the model needs:
canonical identities, ACB game/player box scores and archived pregame context.
The large ACB play-by-play/event table is deliberately excluded.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from euroleague.config import DatabaseSettings
from euroleague.incremental_confirmation import load_test_database_settings


def _rows(connection: Any, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        return list(cursor.fetchall())


def _executemany(connection: Any, query: str, rows: Iterable[tuple[Any, ...]]) -> int:
    materialized = list(rows)
    if not materialized:
        return 0
    with connection.cursor() as cursor:
        cursor.executemany(query, materialized)
    return len(materialized)


def main() -> int:
    source_settings = DatabaseSettings.from_url(os.environ.get("DATABASE_URL", ""))
    target_settings = load_test_database_settings()

    with (
        psycopg.connect(source_settings.url(), autocommit=True) as source,
        psycopg.connect(target_settings.url(), autocommit=True) as target,
    ):
        with target.cursor() as cursor:
            cursor.execute("set search_path to warehouse")

        athletes = _rows(
            source,
            """
            select athlete_id, display_name, birth_date, created_at, updated_at
            from athlete
            order by athlete_id
            """,
        )
        athlete_count = _executemany(
            target,
            """
            insert into athlete (
                athlete_id, display_name, birth_date, created_at, updated_at
            )
            values (%s, %s, %s, %s, %s)
            on conflict (athlete_id) do nothing
            """,
            athletes,
        )

        identities_raw = _rows(
            source,
            """
            select
                source, source_player_id, athlete_id, display_name, birth_date,
                height_cm, country_code, team_source_id, match_status, evidence,
                first_seen_at, last_seen_at, created_at
            from athlete_source_identity
            order by source, source_player_id
            """,
        )
        identities = [(*row[:9], Jsonb(row[9]), *row[10:]) for row in identities_raw]
        identity_count = _executemany(
            target,
            """
            insert into athlete_source_identity (
                source, source_player_id, athlete_id, display_name, birth_date,
                height_cm, country_code, team_source_id, match_status, evidence,
                first_seen_at, last_seen_at, created_at
            )
            values (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            on conflict (source, source_player_id) do nothing
            """,
            identities,
        )

        acb_games_raw = _rows(
            source,
            """
            select
                match_id, competition_id, competition_name, start_at,
                home_team_source_id, away_team_source_id, home_team_name,
                away_team_name, home_score, away_score, match_finished,
                fetched_at, raw_header
            from acb_game
            order by match_id
            """,
        )
        acb_games = [
            (*row[:12], Jsonb(row[12]) if row[12] is not None else None) for row in acb_games_raw
        ]
        acb_game_count = _executemany(
            target,
            """
            insert into acb_game (
                match_id, competition_id, competition_name, start_at,
                home_team_source_id, away_team_source_id, home_team_name,
                away_team_name, home_score, away_score, match_finished,
                fetched_at, raw_header
            )
            values (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            on conflict (match_id) do nothing
            """,
            acb_games,
        )

        acb_players_raw = _rows(
            source,
            """
            select
                match_id, source_player_id, team_source_id, display_name,
                jersey_number, is_starter, minutes_seconds, points,
                two_made, two_attempted, three_made, three_attempted,
                free_throw_made, free_throw_attempted, offensive_rebounds,
                defensive_rebounds, assists, steals, turnovers, blocks,
                fouls_committed, fouls_received, plus_minus, valuation, raw_line
            from acb_player_game
            order by match_id, source_player_id
            """,
        )
        acb_players = [(*row[:24], Jsonb(row[24])) for row in acb_players_raw]
        acb_player_count = _executemany(
            target,
            """
            insert into acb_player_game (
                match_id, source_player_id, team_source_id, display_name,
                jersey_number, is_starter, minutes_seconds, points,
                two_made, two_attempted, three_made, three_attempted,
                free_throw_made, free_throw_attempted, offensive_rebounds,
                defensive_rebounds, assists, steals, turnovers, blocks,
                fouls_committed, fouls_received, plus_minus, valuation, raw_line
            )
            values (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s
            )
            on conflict (match_id, source_player_id) do nothing
            """,
            acb_players,
        )

        context_raw = _rows(
            source,
            """
            select
                season_code, gamecode, team_code, player_id, event_type,
                role_direction, severity, source_confidence, source_name,
                source_url, publisher_url, headline, summary, published_at,
                fetched_at, query_text, classification_method, metadata
            from pregame_context_event
            where season_code in ('E2023', 'E2024', 'E2025')
            order by season_code, gamecode, team_code, published_at, source_url
            """,
        )
        context_rows = [(*row[:17], Jsonb(row[17])) for row in context_raw]
        context_count = _executemany(
            target,
            """
            insert into pregame_context_event (
                season_code, gamecode, team_code, player_id, event_type,
                role_direction, severity, source_confidence, source_name,
                source_url, publisher_url, headline, summary, published_at,
                fetched_at, query_text, classification_method, metadata
            )
            values (
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            on conflict do nothing
            """,
            context_rows,
        )

        collection_raw = _rows(
            source,
            """
            select
                season_code, gamecode, team_code, collected_at,
                query_count, successful_query_count, failed_query_count,
                players_queried, items_seen, inserted_event_count,
                collector_version, metadata
            from pregame_context_collection
            where season_code in ('E2023', 'E2024', 'E2025')
            order by season_code, gamecode, team_code, collected_at
            """,
        )
        collection_rows = [
            (*row[:11], Jsonb(row[11]))
            for row in collection_raw
        ]
        collection_count = _executemany(
            target,
            """
            insert into pregame_context_collection (
                season_code, gamecode, team_code, collected_at,
                query_count, successful_query_count, failed_query_count,
                players_queried, items_seen, inserted_event_count,
                collector_version, metadata
            )
            values (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            collection_rows,
        )

    print(
        "training context copied: "
        f"athletes={athlete_count} identities={identity_count} "
        f"acb_games={acb_game_count} acb_player_games={acb_player_count} "
        f"pregame_context={context_count} pregame_collections={collection_count}; "
        "acb_event=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
