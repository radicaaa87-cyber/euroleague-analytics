"""One-time import of manually extracted Mozzart player-points offers."""

from __future__ import annotations

import base64
import gzip
import json
import os
from datetime import time
from pathlib import Path

import psycopg

from euroleague.config import DatabaseSettings

PART_GLOB = "data/manual_mozzart_offers.b64.part*"
COLLECTION_ID = 4


def _load_rows() -> list[dict[str, object]]:
    encoded = "".join(
        path.read_text(encoding="utf-8").strip()
        for path in sorted(Path(".").glob(PART_GLOB))
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
    inserted = 0
    missing_documents = 0
    documents_with_offers: set[int] = set()

    with psycopg.connect(
        settings.url(),
        autocommit=True,
        prepare_threshold=None,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select document_id, content_sha256, document_date
                from bookmaker_source_document
                where bookmaker = 'mozzart'
                """
            )
            documents = {
                row[1]: (int(row[0]), row[2])
                for row in cursor.fetchall()
            }

        for row in rows:
            document = documents.get(str(row["content_sha256"]))
            if document is None:
                missing_documents += 1
                continue
            document_id, offer_date = document
            documents_with_offers.add(document_id)

            with connection.cursor() as cursor:
                cursor.execute(
                    """
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
                    values (
                        %s, 'mozzart', %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s
                    )
                    on conflict (document_id, row_sha256) do nothing
                    returning offer_id
                    """,
                    (
                        document_id,
                        offer_date,
                        time.fromisoformat(str(row["event_time_local"])),
                        row.get("source_event_code"),
                        row["participant_text"],
                        row.get("player_name_raw"),
                        row.get("player_name_normalized"),
                        row.get("team_name_raw"),
                        row["points_line"],
                        row.get("under_odds"),
                        row.get("over_odds"),
                        row.get("page_number"),
                        row["row_text"],
                        row["row_sha256"],
                    ),
                )
                inserted += int(cursor.fetchone() is not None)

        with connection.cursor() as cursor:
            cursor.execute(
                """
                update bookmaker_collection_run
                set
                    finished_at = now(),
                    discovery_count = 44,
                    fetched_count = 41,
                    parsed_document_count = %s,
                    inserted_offer_count = %s,
                    error_count = %s,
                    metadata = metadata || %s::jsonb
                where collection_id = %s
                """,
                (
                    len(documents_with_offers),
                    inserted,
                    missing_documents,
                    json.dumps(
                        {
                            "parsed_offer_rows": len(rows),
                            "documents_with_player_points": len(documents_with_offers),
                            "import_method": "github_actions_manual_payload_v1",
                            "git_commit": os.environ.get("GITHUB_SHA"),
                        }
                    ),
                    COLLECTION_ID,
                ),
            )

    print(
        json.dumps(
            {
                "rows": len(rows),
                "inserted": inserted,
                "documents_with_offers": len(documents_with_offers),
                "missing_documents": missing_documents,
            },
            sort_keys=True,
        )
    )
    return 0 if missing_documents == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
