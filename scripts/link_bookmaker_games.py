"""Batch-link bookmaker offers to historical EuroLeague games.

Historical game truth is read from the disposable verified training warehouse.
Bookmaker offers are read from the hosted database. Only a unique exact
athlete+date match is eligible for update.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import psycopg

from euroleague.bookmaker_game_link import HistoricalGameCandidate, match_offer_to_game
from euroleague.config import DatabaseSettings
from euroleague.incremental_confirmation import load_test_database_settings


def _load_historical_candidates(
    connection: psycopg.Connection[Any],
) -> list[HistoricalGameCandidate]:
    with connection.cursor() as cursor:
        cursor.execute("set search_path to warehouse")
        cursor.execute(
            """
            select distinct
                asi.athlete_id::text,
                g.season_code,
                g.gamecode,
                g.local_date::date,
                box.team_code
            from raw_boxscore_player box
            join raw_game g
              on g.season_code = box.season_code
             and g.gamecode = box.gamecode
            join athlete_source_identity asi
              on asi.source = 'EL'
             and asi.source_player_id = box.player_id
            where g.season_code in ('E2023', 'E2024', 'E2025')
              and g.played
            """
        )
        return [
            HistoricalGameCandidate(
                athlete_id=str(row[0]),
                season_code=str(row[1]),
                gamecode=int(row[2]),
                game_date=row[3],
                team_code=str(row[4]),
            )
            for row in cursor.fetchall()
        ]


def _load_offers(connection: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select offer_id, athlete_id::text, offer_date, participant_text, team_name_raw
            from bookmaker_player_points_offer
            where athlete_id is not null
              and gamecode is null
              and offer_date is not null
            order by offer_id
            """
        )
        return [
            {
                "offer_id": int(row[0]),
                "athlete_id": str(row[1]),
                "offer_date": row[2],
                "participant_text": str(row[3]),
                "team_name_raw": row[4],
            }
            for row in cursor.fetchall()
        ]


def _resolve(
    offers: list[dict[str, Any]],
    candidates: list[HistoricalGameCandidate],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_athlete: dict[str, list[HistoricalGameCandidate]] = {}
    for candidate in candidates:
        by_athlete.setdefault(candidate.athlete_id, []).append(candidate)

    accepted: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for offer in offers:
        match = match_offer_to_game(
            athlete_id=offer["athlete_id"],
            offer_date=offer["offer_date"],
            candidates=by_athlete.get(offer["athlete_id"], ()),
        )
        if match is None:
            unresolved.append(offer)
            continue
        accepted.append(
            {
                **offer,
                "season_code": match.season_code,
                "gamecode": match.gamecode,
                "historical_team_code": match.team_code,
                "game_match_method": match.method,
                "game_match_confidence": match.confidence,
            }
        )
    return accepted, unresolved


def _apply(
    connection: psycopg.Connection[Any],
    accepted: list[dict[str, Any]],
) -> int:
    if not accepted:
        return 0

    payload = [
        {
            "offer_id": row["offer_id"],
            "season_code": row["season_code"],
            "gamecode": row["gamecode"],
            "game_match_method": row["game_match_method"],
            "game_match_confidence": row["game_match_confidence"],
        }
        for row in accepted
    ]
    with connection.cursor() as cursor:
        cursor.execute(
            """
            with src as (
                select *
                from jsonb_to_recordset(%s::jsonb) as x(
                    offer_id bigint,
                    season_code text,
                    gamecode integer,
                    game_match_method text,
                    game_match_confidence numeric
                )
            ),
            updated as (
                update bookmaker_player_points_offer offer
                set
                    season_code = src.season_code,
                    gamecode = src.gamecode,
                    game_match_method = src.game_match_method,
                    game_match_confidence = src.game_match_confidence
                from src
                where offer.offer_id = src.offer_id
                  and offer.gamecode is null
                returning offer.offer_id
            )
            select count(*) from updated
            """,
            (json.dumps(payload),),
        )
        return int(cursor.fetchone()[0])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--review-limit", type=int, default=30)
    args = parser.parse_args(argv)

    hosted_url = os.environ.get("BOOKMAKER_DATABASE_URL", "")
    hosted_settings = DatabaseSettings.from_url(hosted_url)
    local_settings = load_test_database_settings()

    with (
        psycopg.connect(local_settings.url(), autocommit=True) as historical,
        psycopg.connect(hosted_settings.url(), prepare_threshold=None) as hosted,
    ):
        candidates = _load_historical_candidates(historical)
        offers = _load_offers(hosted)
        accepted, unresolved = _resolve(offers, candidates)
        updated = _apply(hosted, accepted) if args.apply else 0
        if args.apply:
            hosted.commit()

    summary = {
        "mode": "apply" if args.apply else "dry_run",
        "historical_candidates": len(candidates),
        "eligible_offers": len(offers),
        "auto_game_match": len(accepted),
        "review_or_unresolved": len(unresolved),
        "updated": updated,
        "match_sample": accepted[:20],
        "unresolved_sample": unresolved[: max(args.review_limit, 0)],
    }
    rendered = json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
