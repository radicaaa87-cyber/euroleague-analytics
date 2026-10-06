"""Link bookmaker player-point offers to canonical EuroLeague athletes in one batch.

The command is intentionally conservative and fast:
- load unresolved offers once;
- load EuroLeague source identities once;
- resolve in memory;
- apply only exact, unique name matches;
- write all accepted links in one bulk UPDATE.

Without --apply the command is read-only and prints a dry-run summary.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from typing import Any

import psycopg

from euroleague.bookmaker_odds import AthleteCandidate, resolve_participant
from euroleague.config import DatabaseSettings


def _load_candidates(connection: psycopg.Connection[Any]) -> list[AthleteCandidate]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select athlete_id::text, display_name
            from athlete_source_identity
            where source = 'EL'
            order by display_name, athlete_id
            """
        )
        return [
            AthleteCandidate(athlete_id=row[0], display_name=row[1]) for row in cursor.fetchall()
        ]


def _load_unresolved_offers(connection: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select offer_id, bookmaker, participant_text
            from bookmaker_player_points_offer
            where athlete_id is null
            order by offer_id
            """
        )
        return [
            {
                "offer_id": int(row[0]),
                "bookmaker": str(row[1]),
                "participant_text": str(row[2]),
            }
            for row in cursor.fetchall()
        ]


def _resolve_batch(
    offers: list[dict[str, Any]],
    candidates: list[AthleteCandidate],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []

    for offer in offers:
        match = resolve_participant(
            offer["participant_text"],
            candidates,
            bookmaker=offer["bookmaker"],
        )
        row = {
            "offer_id": offer["offer_id"],
            "bookmaker": offer["bookmaker"],
            "participant_text": offer["participant_text"],
            **asdict(match),
        }

        if match.athlete_id is not None and match.confidence == 1.0:
            accepted.append(row)
        else:
            review.append(row)

    return accepted, review


def _apply_links(
    connection: psycopg.Connection[Any],
    accepted: list[dict[str, Any]],
) -> int:
    if not accepted:
        return 0

    payload = [
        {
            "offer_id": row["offer_id"],
            "athlete_id": row["athlete_id"],
            "player_name_raw": row["player_name_raw"],
            "player_name_normalized": row["player_name_normalized"],
            "team_name_raw": row["team_name_raw"],
            "athlete_match_method": f"el_{row['method']}",
            "athlete_match_confidence": row["confidence"],
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
                    athlete_id uuid,
                    player_name_raw text,
                    player_name_normalized text,
                    team_name_raw text,
                    athlete_match_method text,
                    athlete_match_confidence numeric
                )
            ),
            updated as (
                update bookmaker_player_points_offer offer
                set
                    athlete_id = src.athlete_id,
                    player_name_raw = src.player_name_raw,
                    player_name_normalized = src.player_name_normalized,
                    team_name_raw = src.team_name_raw,
                    athlete_match_method = src.athlete_match_method,
                    athlete_match_confidence = src.athlete_match_confidence
                from src
                where offer.offer_id = src.offer_id
                  and offer.athlete_id is null
                returning offer.offer_id
            )
            select count(*) from updated
            """,
            (json.dumps(payload, ensure_ascii=False),),
        )
        return int(cursor.fetchone()[0])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="persist exact unique EL athlete matches; default is dry-run",
    )
    parser.add_argument(
        "--review-limit",
        type=int,
        default=30,
        help="maximum unresolved/review rows printed in the summary",
    )
    args = parser.parse_args(argv)

    settings = DatabaseSettings.from_env()
    with psycopg.connect(settings.url(), prepare_threshold=None) as connection:
        candidates = _load_candidates(connection)
        offers = _load_unresolved_offers(connection)
        accepted, review = _resolve_batch(offers, candidates)

        updated = 0
        if args.apply:
            updated = _apply_links(connection, accepted)
            connection.commit()

    summary = {
        "mode": "apply" if args.apply else "dry_run",
        "el_candidates": len(candidates),
        "unresolved_offers_before": len(offers),
        "auto_match": len(accepted),
        "review_or_unresolved": len(review),
        "updated": updated,
        "review_sample": review[: max(args.review_limit, 0)],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
