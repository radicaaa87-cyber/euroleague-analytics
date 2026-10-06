"""Bulk import of the cleaned Mozzart player-points archive."""

from __future__ import annotations

import base64
import gzip
import json
import os
from pathlib import Path

import psycopg

from euroleague.config import DatabaseSettings

PART_GLOB = "data/manual_mozzart_offers.b64.part*"
COLLECTION_ID = 4


def _load_rows() -> list[dict[str, object]]:
    encoded = "".join(
        path.read_text(encoding="utf-8").strip() for path in sorted(Path(".").glob(PART_GLOB))
    )
    if not encoded:
        raise RuntimeError("Manual Mozzart payload parts are missing.")
    payload = gzip.decompress(base64.b64decode(encoded))
    rows = json.loads(payload)
    if not isinstance(rows, list):
        raise RuntimeError("Manual Mozzart payload must be a JSON array.")
    return rows


def main() -> int:
    rows = _load_rows()
    settings = DatabaseSettings.from_env()

    with psycopg.connect(settings.url(), prepare_threshold=None) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select document_id, content_sha256, document_date
                from bookmaker_source_document
                where bookmaker = 'mozzart'
                """
            )
            documents = {row[1]: (int(row[0]), row[2]) for row in cursor.fetchall()}

        missing = sorted(
            {
                str(row["content_sha256"])
                for row in rows
                if str(row["content_sha256"]) not in documents
            }
        )
        if missing:
            print(
                json.dumps(
                    {
                        "rows": len(rows),
                        "missing_documents": len(missing),
                        "missing_sha256": missing[:10],
                    },
                    sort_keys=True,
                )
            )
            return 2

        prepared: list[dict[str, object]] = []
        document_ids: set[int] = set()
        for row in rows:
            document_id, offer_date = documents[str(row["content_sha256"])]
            document_ids.add(document_id)
            prepared.append(
                {
                    "document_id": document_id,
                    "offer_date": offer_date.isoformat(),
                    "event_time_local": row["event_time_local"],
                    "source_event_code": row.get("source_event_code"),
                    "participant_text": row["participant_text"],
                    "player_name_raw": row.get("player_name_raw"),
                    "player_name_normalized": row.get("player_name_normalized"),
                    "team_name_raw": row.get("team_name_raw"),
                    "points_line": row["points_line"],
                    "under_odds": row.get("under_odds"),
                    "over_odds": row.get("over_odds"),
                    "page_number": row.get("page_number"),
                    "row_text": row["row_text"],
                    "row_sha256": row["row_sha256"],
                }
            )

        with connection.cursor() as cursor:
            cursor.execute(
                """
                delete from bookmaker_player_points_offer
                where bookmaker = 'mozzart'
                  and document_id = any(%s::bigint[])
                """,
                (sorted(document_ids),),
            )

            cursor.execute(
                """
                with src as (
                    select *
                    from jsonb_to_recordset(%s::jsonb) as x(
                        document_id bigint,
                        offer_date date,
                        event_time_local time,
                        source_event_code text,
                        participant_text text,
                        player_name_raw text,
                        player_name_normalized text,
                        team_name_raw text,
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
                        points_line,
                        under_odds,
                        over_odds,
                        page_number,
                        row_text,
                        row_sha256
                    )
                    select
                        document_id,
                        'mozzart',
                        offer_date,
                        event_time_local,
                        source_event_code,
                        participant_text,
                        player_name_raw,
                        player_name_normalized,
                        team_name_raw,
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
                (json.dumps(prepared, ensure_ascii=False),),
            )
            inserted = int(cursor.fetchone()[0])

            cursor.execute(
                """
                update bookmaker_collection_run
                set
                    finished_at = now(),
                    discovery_count = 44,
                    fetched_count = 41,
                    parsed_document_count = %s,
                    inserted_offer_count = %s,
                    error_count = 0,
                    collector_version = 'optimized_manual_zip_v2',
                    metadata = metadata || %s::jsonb
                where collection_id = %s
                """,
                (
                    len(document_ids),
                    inserted,
                    json.dumps(
                        {
                            "parsed_offer_rows": len(rows),
                            "documents_with_player_points": len(document_ids),
                            "import_method": "github_actions_bulk_jsonb_v2",
                            "source_zip": "Srbija 13.11.2024.zip",
                            "git_commit": os.environ.get("GITHUB_SHA"),
                        }
                    ),
                    COLLECTION_ID,
                ),
            )

        connection.commit()

    print(
        json.dumps(
            {
                "rows": len(rows),
                "inserted": inserted,
                "documents_with_offers": len(document_ids),
                "missing_documents": 0,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
