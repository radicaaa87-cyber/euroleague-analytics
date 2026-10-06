"""Bulk-import a prepared bookmaker batch into PostgreSQL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import psycopg

from euroleague.bookmaker_odds import AthleteCandidate, resolve_participant
from euroleague.config import DatabaseSettings

IMPORTER_VERSION = "bookmaker_batch_import_v1"
DEFAULT_CHUNK_SIZE = 2000


def _athlete_candidates(
    connection: psycopg.Connection[Any],
) -> list[AthleteCandidate]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select athlete_id::text, display_name
            from athlete
            union
            select athlete_id::text, display_name
            from athlete_source_identity
            where source = 'EL'
            """
        )
        return [
            AthleteCandidate(athlete_id=row[0], display_name=row[1])
            for row in cursor.fetchall()
            if row[1]
        ]


def _start_run(
    connection: psycopg.Connection[Any],
    *,
    bookmaker: str,
    dates: list[str],
    input_pdf_count: int,
    unique_pdf_count: int,
) -> int:
    from_date = min(dates) if dates else None
    to_date = max(dates) if dates else None
    with connection.cursor() as cursor:
        cursor.execute(
            """
            insert into bookmaker_collection_run (
                from_date,
                to_date,
                bookmakers,
                discovery_count,
                fetched_count,
                collector_version,
                metadata
            )
            values (%s, %s, %s, %s, %s, %s, %s::jsonb)
            returning collection_id
            """,
            (
                from_date,
                to_date,
                [bookmaker],
                input_pdf_count,
                unique_pdf_count,
                IMPORTER_VERSION,
                json.dumps(
                    {
                        "mode": "prepared_batch",
                        "input_pdf_count": input_pdf_count,
                        "unique_pdf_count": unique_pdf_count,
                    }
                ),
            ),
        )
        return int(cursor.fetchone()[0])


def _upsert_documents(
    connection: psycopg.Connection[Any],
    documents: list[dict[str, Any]],
) -> dict[str, int]:
    if not documents:
        return {}

    with connection.cursor() as cursor:
        cursor.execute(
            """
            with src as (
                select *
                from jsonb_to_recordset(%s::jsonb) as x(
                    bookmaker text,
                    canonical_url text,
                    fetched_url text,
                    discovery_method text,
                    document_date date,
                    content_sha256 text,
                    content_length integer,
                    page_count integer,
                    parser_version text,
                    parse_status text,
                    metadata jsonb
                )
            ),
            upserted as (
                insert into bookmaker_source_document (
                    bookmaker,
                    canonical_url,
                    fetched_url,
                    discovery_method,
                    document_date,
                    content_sha256,
                    content_length,
                    page_count,
                    parser_version,
                    parse_status,
                    metadata
                )
                select
                    bookmaker,
                    canonical_url,
                    fetched_url,
                    discovery_method,
                    document_date,
                    content_sha256,
                    content_length,
                    page_count,
                    parser_version,
                    parse_status,
                    metadata
                from src
                on conflict (bookmaker, canonical_url, content_sha256)
                do update set
                    fetched_url = excluded.fetched_url,
                    fetched_at = now(),
                    document_date = excluded.document_date,
                    content_length = excluded.content_length,
                    page_count = excluded.page_count,
                    parser_version = excluded.parser_version,
                    parse_status = excluded.parse_status,
                    metadata = bookmaker_source_document.metadata
                        || excluded.metadata
                returning document_id, content_sha256
            )
            select document_id, content_sha256
            from upserted
            """,
            (json.dumps(documents, ensure_ascii=False),),
        )
        return {str(sha256): int(document_id) for document_id, sha256 in cursor.fetchall()}


def _offer_rows(
    *,
    offers: list[dict[str, Any]],
    document_ids: dict[str, int],
    candidates: list[AthleteCandidate],
) -> list[dict[str, Any]]:
    identity_cache: dict[tuple[str, str], Any] = {}
    rows: list[dict[str, Any]] = []

    for offer in offers:
        document_id = document_ids.get(str(offer["document_sha256"]))
        if document_id is None:
            raise RuntimeError(f"No document_id for SHA {offer['document_sha256']}")

        cache_key = (offer["bookmaker"], offer["participant_text"])
        identity = identity_cache.get(cache_key)
        if identity is None:
            identity = resolve_participant(
                offer["participant_text"],
                candidates,
                bookmaker=offer["bookmaker"],
            )
            identity_cache[cache_key] = identity

        rows.append(
            {
                "document_id": document_id,
                "bookmaker": offer["bookmaker"],
                "offer_date": offer["offer_date"],
                "event_time_local": offer["event_time_local"],
                "source_event_code": offer["source_event_code"],
                "participant_text": offer["participant_text"],
                "player_name_raw": identity.player_name_raw,
                "player_name_normalized": identity.player_name_normalized,
                "team_name_raw": identity.team_name_raw,
                "athlete_id": identity.athlete_id,
                "athlete_match_method": identity.method,
                "athlete_match_confidence": identity.confidence,
                "points_line": offer["points_line"],
                "under_odds": offer["under_odds"],
                "over_odds": offer["over_odds"],
                "page_number": offer["page_number"],
                "row_text": offer["row_text"],
                "row_sha256": offer["row_sha256"],
            }
        )
    return rows


def _replace_offers_bulk(
    connection: psycopg.Connection[Any],
    *,
    rows: list[dict[str, Any]],
    document_ids: list[int],
    chunk_size: int,
) -> int:
    if document_ids:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                delete from bookmaker_player_points_offer
                where document_id = any(%s::bigint[])
                """,
                (document_ids,),
            )

    inserted = 0
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start : start + chunk_size]
        with connection.cursor() as cursor:
            cursor.execute(
                """
                with src as (
                    select *
                    from jsonb_to_recordset(%s::jsonb) as x(
                        document_id bigint,
                        bookmaker text,
                        offer_date date,
                        event_time_local time,
                        source_event_code text,
                        participant_text text,
                        player_name_raw text,
                        player_name_normalized text,
                        team_name_raw text,
                        athlete_id uuid,
                        athlete_match_method text,
                        athlete_match_confidence numeric,
                        points_line numeric,
                        under_odds numeric,
                        over_odds numeric,
                        page_number integer,
                        row_text text,
                        row_sha256 text
                    )
                ),
                ins as (
                    insert into bookmaker_player_points_offer (
                        document_id,
                        bookmaker,
                        offer_date,
                        event_time_local,
                        source_event_code,
                        participant_text,
                        player_name_raw,
                        player_name_normalized,
                        team_name_raw,
                        athlete_id,
                        athlete_match_method,
                        athlete_match_confidence,
                        points_line,
                        under_odds,
                        over_odds,
                        page_number,
                        row_text,
                        row_sha256
                    )
                    select
                        document_id,
                        bookmaker,
                        offer_date,
                        event_time_local,
                        source_event_code,
                        participant_text,
                        player_name_raw,
                        player_name_normalized,
                        team_name_raw,
                        athlete_id,
                        athlete_match_method,
                        athlete_match_confidence,
                        points_line,
                        under_odds,
                        over_odds,
                        page_number,
                        row_text,
                        row_sha256
                    from src
                    on conflict (document_id, row_sha256) do nothing
                    returning offer_id
                )
                select count(*) from ins
                """,
                (json.dumps(chunk, ensure_ascii=False),),
            )
            inserted += int(cursor.fetchone()[0])
    return inserted


def _finish_run(
    connection: psycopg.Connection[Any],
    *,
    collection_id: int,
    parsed_document_count: int,
    inserted_offer_count: int,
    error_count: int,
    metadata: dict[str, Any],
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            update bookmaker_collection_run
            set
                finished_at = now(),
                parsed_document_count = %s,
                inserted_offer_count = %s,
                error_count = %s,
                metadata = metadata || %s::jsonb
            where collection_id = %s
            """,
            (
                parsed_document_count,
                inserted_offer_count,
                error_count,
                json.dumps(metadata, ensure_ascii=False),
                collection_id,
            ),
        )


def import_payload(
    payload: dict[str, Any],
    *,
    chunk_size: int,
) -> dict[str, Any]:
    documents = list(payload.get("documents") or [])
    offers = list(payload.get("offers") or [])
    bookmaker = str(payload["bookmaker"])
    dates = [str(row["document_date"]) for row in documents if row.get("document_date")]

    settings = DatabaseSettings.from_env()
    with psycopg.connect(settings.url()) as connection:
        collection_id = _start_run(
            connection,
            bookmaker=bookmaker,
            dates=dates,
            input_pdf_count=int(payload.get("input_pdf_count") or len(documents)),
            unique_pdf_count=int(payload.get("unique_pdf_count") or len(documents)),
        )
        candidates = _athlete_candidates(connection)
        document_ids = _upsert_documents(connection, documents)
        rows = _offer_rows(
            offers=offers,
            document_ids=document_ids,
            candidates=candidates,
        )
        inserted_offer_count = _replace_offers_bulk(
            connection,
            rows=rows,
            document_ids=sorted(document_ids.values()),
            chunk_size=chunk_size,
        )
        _finish_run(
            connection,
            collection_id=collection_id,
            parsed_document_count=int(payload.get("parsed_document_count") or 0),
            inserted_offer_count=inserted_offer_count,
            error_count=int(payload.get("error_count") or 0),
            metadata={
                "source_name": payload.get("source_name"),
                "batch_version": payload.get("version"),
                "offer_count": len(offers),
                "identity_candidates": len(candidates),
                "game_linking": "deferred",
            },
        )
        connection.commit()

    return {
        "collection_id": collection_id,
        "documents": len(documents),
        "offers": len(offers),
        "inserted_offers": inserted_offer_count,
        "identity_candidates": len(candidates),
    }


def import_batch(
    *,
    input_path: Path,
    chunk_size: int,
) -> dict[str, Any]:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    return import_payload(payload, chunk_size=chunk_size)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
    )
    args = parser.parse_args(argv)

    if not 100 <= args.chunk_size <= 5000:
        raise ValueError("--chunk-size must be between 100 and 5000")

    result = import_batch(
        input_path=args.input,
        chunk_size=args.chunk_size,
    )
    print("bookmaker_batch_import=" + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
