"""Fast one-pass import of user-supplied bookmaker PDFs.

This path is for PDFs we already possess. It does no web discovery and no
per-offer database queries:

1. read each PDF payload once from a PDF, directory, or ZIP;
2. extract text and parse all offers in memory;
3. load athlete candidates once and resolve identities in memory;
4. bulk-upsert all documents in one SQL statement;
5. bulk-insert all offers in one SQL statement.

Game linking remains a separate batch operation against the verified historical
warehouse so ingestion never waits on historical joins.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
from pypdf import PdfReader

from euroleague.bookmaker_local_archive import iter_pdf_payloads
from euroleague.bookmaker_odds import (
    AthleteCandidate,
    ParsedOffer,
    infer_document_date,
    parse_meridian_player_points_pages,
    parse_millennium_player_points_pages,
    parse_mozzart_player_points_pages,
    parse_starbet_player_points_pages,
    resolve_participant,
)
from euroleague.config import DatabaseSettings

IMPORT_VERSION = "local_pdf_batch_v1"
MAX_PDF_BYTES = 25 * 1024 * 1024

PARSERS = {
    "mozzart": parse_mozzart_player_points_pages,
    "starbet": parse_starbet_player_points_pages,
    "meridian": parse_meridian_player_points_pages,
    "millennium": parse_millennium_player_points_pages,
}


def _extract_pages(payload: bytes) -> list[str]:
    if len(payload) > MAX_PDF_BYTES:
        raise ValueError("PDF exceeds 25 MB safety limit.")
    if not payload.startswith(b"%PDF"):
        raise ValueError("Input payload is not a PDF.")
    reader = PdfReader(io.BytesIO(payload))
    return [(page.extract_text() or "") for page in reader.pages]


def _athlete_candidates(connection: psycopg.Connection[Any]) -> list[AthleteCandidate]:
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
            AthleteCandidate(athlete_id=str(row[0]), display_name=str(row[1]))
            for row in cursor.fetchall()
            if row[1]
        ]


def _start_collection(
    connection: psycopg.Connection[Any],
    *,
    bookmaker: str,
    dates: list[date],
) -> int:
    from_date = min(dates) if dates else date.today()
    to_date = max(dates) if dates else from_date
    with connection.cursor() as cursor:
        cursor.execute(
            """
            insert into bookmaker_collection_run (
                from_date,
                to_date,
                bookmakers,
                collector_version,
                metadata
            )
            values (%s, %s, %s, %s, %s::jsonb)
            returning collection_id
            """,
            (
                from_date,
                to_date,
                [bookmaker],
                IMPORT_VERSION,
                json.dumps({"import_method": "local_pdf_batch"}),
            ),
        )
        return int(cursor.fetchone()[0])


def _upsert_documents_bulk(
    connection: psycopg.Connection[Any],
    rows: list[dict[str, Any]],
) -> dict[str, int]:
    if not rows:
        return {}

    with connection.cursor() as cursor:
        cursor.execute(
            """
            with src as (
                select *
                from jsonb_to_recordset(%s::jsonb) as x(
                    canonical_url text,
                    document_date date,
                    content_sha256 text,
                    content_length integer,
                    page_count integer,
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
                    archive_capture_at,
                    content_sha256,
                    content_length,
                    page_count,
                    parser_version,
                    parse_status,
                    metadata
                )
                select
                    %s,
                    canonical_url,
                    canonical_url,
                    'manual_local_batch',
                    document_date,
                    null,
                    content_sha256,
                    content_length,
                    page_count,
                    %s,
                    parse_status,
                    metadata
                from src
                on conflict (bookmaker, canonical_url, content_sha256)
                do update set
                    fetched_at = now(),
                    page_count = excluded.page_count,
                    parser_version = excluded.parser_version,
                    parse_status = excluded.parse_status,
                    metadata = bookmaker_source_document.metadata || excluded.metadata
                returning document_id, content_sha256
            )
            select document_id, content_sha256
            from upserted
            """,
            (json.dumps(rows, ensure_ascii=False), rows[0]["bookmaker"], IMPORT_VERSION),
        )
        return {str(sha): int(document_id) for document_id, sha in cursor.fetchall()}


def _insert_offers_bulk(
    connection: psycopg.Connection[Any],
    rows: list[dict[str, Any]],
) -> int:
    if not rows:
        return 0

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
            inserted as (
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
            select count(*) from inserted
            """,
            (json.dumps(rows, ensure_ascii=False, default=str),),
        )
        return int(cursor.fetchone()[0])


def _finish_collection(
    connection: psycopg.Connection[Any],
    collection_id: int,
    *,
    documents: int,
    parsed_documents: int,
    parsed_offers: int,
    inserted_offers: int,
    errors: list[dict[str, str]],
    elapsed_seconds: float,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            update bookmaker_collection_run
            set
                finished_at = now(),
                discovery_count = %s,
                fetched_count = %s,
                parsed_document_count = %s,
                inserted_offer_count = %s,
                error_count = %s,
                metadata = metadata || %s::jsonb
            where collection_id = %s
            """,
            (
                documents,
                documents,
                parsed_documents,
                inserted_offers,
                len(errors),
                json.dumps(
                    {
                        "parsed_offer_rows": parsed_offers,
                        "elapsed_seconds": round(elapsed_seconds, 3),
                        "errors": errors[:50],
                    },
                    ensure_ascii=False,
                ),
                collection_id,
            ),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="PDF, ZIP, or directory of PDFs")
    parser.add_argument("--bookmaker", choices=sorted(PARSERS), required=True)
    args = parser.parse_args(argv)

    started = time.monotonic()
    payloads = iter_pdf_payloads(args.input)
    parser_fn = PARSERS[args.bookmaker]

    parsed_docs: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    all_dates: list[date] = []

    for item in payloads:
        try:
            pages = _extract_pages(item.payload)
            document_date = infer_document_date(
                item.name,
                title=pages[0][:1000] if pages else None,
            )
            if document_date is not None:
                all_dates.append(document_date)
            offers = parser_fn(pages)
            sha256 = hashlib.sha256(item.payload).hexdigest()
            parsed_docs.append(
                {
                    "name": item.name,
                    "bookmaker": args.bookmaker,
                    "canonical_url": f"manual://{item.name}",
                    "document_date": document_date,
                    "content_sha256": sha256,
                    "content_length": len(item.payload),
                    "page_count": len(pages),
                    "parse_status": "parsed" if offers else "no_player_points",
                    "metadata": {
                        "source_name": item.name,
                        "import_method": "local_pdf_batch",
                    },
                    "offers": offers,
                }
            )
        except Exception as exc:
            errors.append(
                {
                    "source_name": item.name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    settings = DatabaseSettings.from_env()
    with psycopg.connect(settings.url(), prepare_threshold=None) as connection:
        candidates = _athlete_candidates(connection)
        collection_id = _start_collection(
            connection,
            bookmaker=args.bookmaker,
            dates=all_dates,
        )

        document_rows = [
            {key: value for key, value in document.items() if key not in {"name", "offers"}}
            for document in parsed_docs
        ]
        document_ids = _upsert_documents_bulk(connection, document_rows)

        identity_cache: dict[str, Any] = {}
        offer_rows: list[dict[str, Any]] = []
        for document in parsed_docs:
            document_id = document_ids[document["content_sha256"]]
            for offer in document["offers"]:
                assert isinstance(offer, ParsedOffer)
                identity = identity_cache.get(offer.participant_text)
                if identity is None:
                    identity = resolve_participant(
                        offer.participant_text,
                        candidates,
                        bookmaker=args.bookmaker,
                    )
                    identity_cache[offer.participant_text] = identity

                offer_rows.append(
                    {
                        "document_id": document_id,
                        "bookmaker": args.bookmaker,
                        "offer_date": document["document_date"],
                        "event_time_local": offer.event_time_local,
                        "source_event_code": offer.source_event_code,
                        "participant_text": offer.participant_text,
                        "player_name_raw": identity.player_name_raw,
                        "player_name_normalized": identity.player_name_normalized,
                        "team_name_raw": identity.team_name_raw,
                        "athlete_id": identity.athlete_id,
                        "athlete_match_method": identity.method,
                        "athlete_match_confidence": identity.confidence,
                        "points_line": offer.points_line,
                        "under_odds": offer.under_odds,
                        "over_odds": offer.over_odds,
                        "page_number": offer.page_number,
                        "row_text": offer.row_text,
                        "row_sha256": offer.row_sha256,
                    }
                )

        inserted_offers = _insert_offers_bulk(connection, offer_rows)
        elapsed = time.monotonic() - started
        _finish_collection(
            connection,
            collection_id,
            documents=len(payloads),
            parsed_documents=sum(bool(row["offers"]) for row in parsed_docs),
            parsed_offers=len(offer_rows),
            inserted_offers=inserted_offers,
            errors=errors,
            elapsed_seconds=elapsed,
        )
        connection.commit()

    print(
        json.dumps(
            {
                "bookmaker": args.bookmaker,
                "documents_seen": len(payloads),
                "documents_parsed": len(parsed_docs),
                "documents_with_offers": sum(bool(row["offers"]) for row in parsed_docs),
                "offers_parsed": len(offer_rows),
                "offers_inserted": inserted_offers,
                "errors": len(errors),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "database_round_trips_shape": "fixed_batch",
                "game_linking": "deferred",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
