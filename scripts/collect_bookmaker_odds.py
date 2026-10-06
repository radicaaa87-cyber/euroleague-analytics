"""Autonomously discover and archive EuroLeague bookmaker player-points offers."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import requests
from pypdf import PdfReader

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

COLLECTOR_VERSION = "bookmaker_archive_v2"
USER_AGENT = "euroleague-analytics-bookmaker-archive/1.0"
WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
BRAVE_SEARCH = "https://api.search.brave.com/res/v1/web/search"
MAX_PDF_BYTES = 25 * 1024 * 1024

BOOKMAKER_CONFIG: dict[str, dict[str, Any]] = {
    "starbet": {
        "domains": {"starbet.rs", "www.starbet.rs"},
        "wayback_patterns": [
            "starbet.rs/content/Documents/*",
            "www.starbet.rs/content/Documents/*",
        ],
        "search_queries": [
            'site:starbet.rs/content/Documents filetype:pdf "Euroleague Player"',
            'site:starbet.rs/content/Documents filetype:pdf "Ukupno Poena" Euroleague',
        ],
        "seeds": [
            "https://starbet.rs/content/Documents/Dopuna22.11.pdf",
            "https://www.starbet.rs/content/Documents/Dopuna15.11.2024.pdf",
            "https://www.starbet.rs/content/Documents/Dopuna%2024.10.2025.pdf",
            "https://www.starbet.rs/content/Documents/dopuna%2026.12.2025.pdf",
        ],
    },
    "mozzart": {
        "domains": {"mozzartbet.com", "www.mozzartbet.com"},
        "wayback_patterns": [
            "mozzartbet.com/*",
            "www.mozzartbet.com/*",
        ],
        "search_queries": [
            'site:mozzartbet.com filetype:pdf EVROLIGA "Broj poena igrača"',
            'site:mozzartbet.com filetype:pdf "KOSARKA - IGRAČI" EVROLIGA',
        ],
        "seeds": [],
    },
    "meridian": {
        "domains": {
            "meridianbet.rs",
            "www.meridianbet.rs",
            "coupons.merbet.com",
            "merbet.com",
            "www.merbet.com",
        },
        "wayback_patterns": [
            "coupons.merbet.com/*",
            "meridianbet.rs/*",
            "www.meridianbet.rs/*",
        ],
        "search_queries": [
            'site:coupons.merbet.com filetype:pdf EVROLIGA "poeni igrača"',
            'site:meridianbet.rs filetype:pdf EVROLIGA "poeni igrača"',
            "site:meridianbet.rs filetype:pdf dopuna košarka EVROLIGA",
        ],
        "listing_pages": ["https://coupons.merbet.com/files"],
        "seeds": [],
    },
    "millennium": {
        "domains": {"millenniumbet.rs", "www.millenniumbet.rs"},
        "wayback_patterns": [
            "millenniumbet.rs/*",
            "www.millenniumbet.rs/*",
        ],
        "search_queries": [
            'site:millenniumbet.rs filetype:pdf EVROLIGA "poeni igrača"',
            "site:millenniumbet.rs filetype:pdf dopuna košarka EVROLIGA",
        ],
        "listing_pages": [],
        "seeds": [],
    },
}


@dataclass(frozen=True)
class DiscoveredDocument:
    bookmaker: str
    canonical_url: str
    discovery_method: str
    archive_timestamp: str | None = None
    title: str | None = None

    @property
    def archive_capture_at(self) -> datetime | None:
        if not self.archive_timestamp:
            return None
        return datetime.strptime(self.archive_timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)

    @property
    def archive_url(self) -> str | None:
        if not self.archive_timestamp:
            return None
        return f"https://web.archive.org/web/{self.archive_timestamp}id_/{self.canonical_url}"


def _date_arg(value: str) -> date:
    return date.fromisoformat(value)


def _normalize_url(url: str) -> str:
    split = urllib.parse.urlsplit(url.strip())
    scheme = split.scheme.lower() or "https"
    host = split.netloc.lower()
    path = urllib.parse.quote(urllib.parse.unquote(split.path), safe="/()._-")
    return urllib.parse.urlunsplit((scheme, host, path, split.query, ""))


def _allowed_document(bookmaker: str, url: str) -> bool:
    host = urllib.parse.urlsplit(url).netloc.lower()
    return host in BOOKMAKER_CONFIG[bookmaker]["domains"]


def _discover_wayback(
    session: requests.Session,
    bookmaker: str,
    *,
    from_date: date,
    to_date: date,
    max_results: int,
) -> list[DiscoveredDocument]:
    results: list[DiscoveredDocument] = []
    for pattern in BOOKMAKER_CONFIG[bookmaker]["wayback_patterns"]:
        params = [
            ("url", pattern),
            ("output", "json"),
            ("fl", "timestamp,original,mimetype,statuscode"),
            ("filter", "statuscode:200"),
            ("filter", "mimetype:application/pdf"),
            ("collapse", "digest"),
            ("from", str(from_date.year)),
            ("to", str(to_date.year)),
            ("limit", str(max_results)),
        ]
        response = session.get(WAYBACK_CDX, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or len(payload) < 2:
            continue
        header = payload[0]
        for row in payload[1:]:
            item = dict(zip(header, row, strict=False))
            url = _normalize_url(item.get("original", ""))
            timestamp = item.get("timestamp")
            if not url or not timestamp or not _allowed_document(bookmaker, url):
                continue
            capture = datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            if not from_date <= capture.date() <= to_date:
                continue
            results.append(
                DiscoveredDocument(
                    bookmaker=bookmaker,
                    canonical_url=url,
                    discovery_method="wayback_cdx",
                    archive_timestamp=timestamp,
                )
            )
    return results


def _discover_brave(
    session: requests.Session,
    bookmaker: str,
    *,
    api_key: str,
) -> list[DiscoveredDocument]:
    results: list[DiscoveredDocument] = []
    for query in BOOKMAKER_CONFIG[bookmaker]["search_queries"]:
        response = session.get(
            BRAVE_SEARCH,
            headers={"X-Subscription-Token": api_key},
            params={
                "q": query,
                "count": 20,
                "country": "RS",
                "search_lang": "sr",
            },
            timeout=30,
        )
        response.raise_for_status()
        for item in response.json().get("web", {}).get("results", []):
            url = _normalize_url(item.get("url", ""))
            if not url or not _allowed_document(bookmaker, url):
                continue
            results.append(
                DiscoveredDocument(
                    bookmaker=bookmaker,
                    canonical_url=url,
                    discovery_method="brave_search",
                    title=item.get("title"),
                )
            )
    return results


PDF_REF_RE = re.compile(
    r"""(?P<ref>(?:https?://)?[^"'<>\\s]+\.pdf(?:\?[^"'<>\\s]*)?)""",
    re.IGNORECASE,
)


def _discover_listing_pages(
    session: requests.Session,
    bookmaker: str,
) -> list[DiscoveredDocument]:
    results: list[DiscoveredDocument] = []
    for page_url in BOOKMAKER_CONFIG[bookmaker].get("listing_pages", []):
        response = session.get(page_url, timeout=30)
        response.raise_for_status()
        body = response.text.replace("\\/", "/")
        for match in PDF_REF_RE.finditer(body):
            ref = match.group("ref").strip()
            if ref.startswith("//"):
                ref = "https:" + ref
            url = _normalize_url(urllib.parse.urljoin(page_url, ref))
            if not _allowed_document(bookmaker, url):
                continue
            results.append(
                DiscoveredDocument(
                    bookmaker=bookmaker,
                    canonical_url=url,
                    discovery_method="listing_page",
                    title=ref.rsplit("/", 1)[-1],
                )
            )
    return results


def _discover_documents(
    session: requests.Session,
    bookmakers: list[str],
    *,
    from_date: date,
    to_date: date,
    max_docs: int,
    explicit_urls: Iterable[str],
) -> list[DiscoveredDocument]:
    results: list[DiscoveredDocument] = []

    for bookmaker in bookmakers:
        for url in BOOKMAKER_CONFIG[bookmaker]["seeds"]:
            results.append(
                DiscoveredDocument(
                    bookmaker=bookmaker,
                    canonical_url=_normalize_url(url),
                    discovery_method="seed",
                )
            )

        with contextlib.suppress(requests.RequestException, ValueError):
            results.extend(
                _discover_wayback(
                    session,
                    bookmaker,
                    from_date=from_date,
                    to_date=to_date,
                    max_results=max_docs * 3,
                )
            )

        with contextlib.suppress(requests.RequestException, ValueError):
            results.extend(_discover_listing_pages(session, bookmaker))

        api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
        if api_key:
            with contextlib.suppress(requests.RequestException, ValueError):
                results.extend(
                    _discover_brave(
                        session,
                        bookmaker,
                        api_key=api_key,
                    )
                )

    for url in explicit_urls:
        normalized = _normalize_url(url)
        for bookmaker in bookmakers:
            if _allowed_document(bookmaker, normalized):
                results.append(
                    DiscoveredDocument(
                        bookmaker=bookmaker,
                        canonical_url=normalized,
                        discovery_method="manual",
                    )
                )
                break

    deduped: dict[tuple[str, str], DiscoveredDocument] = {}
    priority = {
        "manual": 5,
        "listing_page": 4,
        "brave_search": 3,
        "wayback_cdx": 2,
        "seed": 1,
    }
    for item in results:
        key = (item.bookmaker, item.canonical_url)
        current = deduped.get(key)
        if (
            current is None
            or priority[item.discovery_method] > priority[current.discovery_method]
            or (current.archive_timestamp is None and item.archive_timestamp is not None)
        ):
            deduped[key] = item

    ordered = sorted(
        deduped.values(),
        key=lambda item: item.archive_timestamp or "",
        reverse=True,
    )
    return ordered[:max_docs]


def _download_pdf(
    session: requests.Session,
    item: DiscoveredDocument,
) -> tuple[bytes, str]:
    urls = [item.canonical_url]
    if item.archive_url:
        urls.append(item.archive_url)

    last_error: Exception | None = None
    for url in urls:
        try:
            response = session.get(url, timeout=35, stream=True, allow_redirects=True)
            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(256 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    raise ValueError("PDF exceeds 25 MB safety limit.")
                chunks.append(chunk)
            payload = b"".join(chunks)
            if not payload.startswith(b"%PDF"):
                raise ValueError("Downloaded body is not a PDF.")
            return payload, response.url
        except (requests.RequestException, ValueError) as exc:
            last_error = exc

    if last_error is None:
        raise RuntimeError("No download URL was available.")
    raise last_error


def _extract_pages(payload: bytes) -> list[str]:
    reader = PdfReader(io.BytesIO(payload))
    pages: list[str] = []
    for page in reader.pages:
        # The bookmaker parsers are line-oriented. Default extraction preserves
        # logical reading order better than layout mode for multi-column odds PDFs.
        text = page.extract_text() or ""
        pages.append(text)
    return pages


def _parse_offers(bookmaker: str, pages: list[str]) -> list[ParsedOffer]:
    if bookmaker == "starbet":
        return parse_starbet_player_points_pages(pages)
    if bookmaker == "mozzart":
        return parse_mozzart_player_points_pages(pages)
    if bookmaker == "meridian":
        return parse_meridian_player_points_pages(pages)
    if bookmaker == "millennium":
        return parse_millennium_player_points_pages(pages)
    return []


def _athlete_candidates(connection: psycopg.Connection[Any]) -> list[AthleteCandidate]:
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


def _resolve_game(
    connection: psycopg.Connection[Any],
    *,
    athlete_id: str | None,
    offer_date: date | None,
) -> tuple[str | None, int | None, str | None, float | None]:
    if athlete_id is None or offer_date is None:
        return None, None, None, None

    with connection.cursor() as cursor:
        cursor.execute(
            """
            select
                pg.season_code,
                pg.gamecode,
                pg.game_date,
                abs(pg.game_date - %s::date) as day_distance
            from athlete_source_identity asi
            join v_player_game pg
              on pg.player_id = asi.source_player_id
            where asi.source = 'EL'
              and asi.athlete_id = %s::uuid
              and pg.game_date between %s::date - 1 and %s::date + 1
            order by day_distance, pg.gamecode
            limit 3
            """,
            (offer_date, athlete_id, offer_date, offer_date),
        )
        rows = cursor.fetchall()

    if not rows:
        return None, None, None, None

    best_distance = int(rows[0][3])
    same_distance = [row for row in rows if int(row[3]) == best_distance]
    if len(same_distance) != 1:
        return None, None, None, None

    season_code, gamecode, _, _ = same_distance[0]
    confidence = 1.0 if best_distance == 0 else 0.85
    method = "player_game_same_date" if best_distance == 0 else "player_game_adjacent_date"
    return season_code, int(gamecode), method, confidence


def _insert_document(
    connection: psycopg.Connection[Any],
    *,
    item: DiscoveredDocument,
    fetched_url: str,
    payload: bytes,
    document_date: date | None,
    page_count: int,
    parse_status: str,
    metadata: dict[str, Any],
) -> int:
    sha256 = hashlib.sha256(payload).hexdigest()
    with connection.cursor() as cursor:
        cursor.execute(
            """
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
            values (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
            )
            on conflict (bookmaker, canonical_url, content_sha256)
            do update set
                fetched_url = excluded.fetched_url,
                fetched_at = now(),
                page_count = excluded.page_count,
                parser_version = excluded.parser_version,
                parse_status = excluded.parse_status,
                metadata = bookmaker_source_document.metadata || excluded.metadata
            returning document_id
            """,
            (
                item.bookmaker,
                item.canonical_url,
                fetched_url,
                item.discovery_method,
                document_date,
                item.archive_capture_at,
                sha256,
                len(payload),
                page_count,
                COLLECTOR_VERSION,
                parse_status,
                json.dumps(metadata),
            ),
        )
        return int(cursor.fetchone()[0])


def _insert_offer(
    connection: psycopg.Connection[Any],
    *,
    document_id: int,
    offer_date: date | None,
    offer: ParsedOffer,
    candidates: list[AthleteCandidate],
) -> bool:
    identity = resolve_participant(
        offer.participant_text,
        candidates,
        bookmaker=offer.bookmaker,
    )
    season_code, gamecode, game_method, game_confidence = _resolve_game(
        connection,
        athlete_id=identity.athlete_id,
        offer_date=offer_date,
    )

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
                athlete_id,
                athlete_match_method,
                athlete_match_confidence,
                season_code,
                gamecode,
                game_match_method,
                game_match_confidence,
                points_line,
                under_odds,
                over_odds,
                page_number,
                row_text,
                row_sha256
            )
            values (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::uuid, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            on conflict (document_id, row_sha256) do nothing
            returning offer_id
            """,
            (
                document_id,
                offer.bookmaker,
                offer_date,
                offer.event_time_local,
                offer.source_event_code,
                offer.participant_text,
                identity.player_name_raw,
                identity.player_name_normalized,
                identity.team_name_raw,
                identity.athlete_id,
                identity.method,
                identity.confidence,
                season_code,
                gamecode,
                game_method,
                game_confidence,
                offer.points_line,
                offer.under_odds,
                offer.over_odds,
                offer.page_number,
                offer.row_text,
                offer.row_sha256,
            ),
        )
        return cursor.fetchone() is not None



def _prepare_offer_rows(
    *,
    document_id: int,
    offer_date: date | None,
    offers: list[ParsedOffer],
    candidates: list[AthleteCandidate],
    identity_cache: dict[tuple[str, str], Any],
) -> list[tuple[Any, ...]]:
    """Prepare raw offer rows without per-offer database lookups."""
    rows: list[tuple[Any, ...]] = []
    for offer in offers:
        cache_key = (offer.bookmaker, offer.participant_text)
        identity = identity_cache.get(cache_key)
        if identity is None:
            identity = resolve_participant(
                offer.participant_text,
                candidates,
                bookmaker=offer.bookmaker,
            )
            identity_cache[cache_key] = identity

        rows.append(
            (
                document_id,
                offer.bookmaker,
                offer_date,
                offer.event_time_local,
                offer.source_event_code,
                offer.participant_text,
                identity.player_name_raw,
                identity.player_name_normalized,
                identity.team_name_raw,
                identity.athlete_id,
                identity.method,
                identity.confidence,
                offer.points_line,
                offer.under_odds,
                offer.over_odds,
                offer.page_number,
                offer.row_text,
                offer.row_sha256,
            )
        )
    return rows


def _insert_offers_bulk(
    connection: psycopg.Connection[Any],
    rows: list[tuple[Any, ...]],
) -> int:
    """Bulk-insert raw offers in one executemany pipeline.

    Athlete identity is resolved in Python, while game linking is deliberately
    deferred. This avoids one game-resolution query plus one insert round-trip
    per offer and makes archive ingestion scale to thousands of rows.
    """
    if not rows:
        return 0

    document_ids = sorted({int(row[0]) for row in rows})
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select count(*)
            from bookmaker_player_points_offer
            where document_id = any(%s)
            """,
            (document_ids,),
        )
        before = int(cursor.fetchone()[0])

        cursor.executemany(
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
            values (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::uuid, %s, %s,
                %s, %s, %s, %s, %s, %s
            )
            on conflict (document_id, row_sha256) do nothing
            """,
            rows,
        )

        cursor.execute(
            """
            select count(*)
            from bookmaker_player_points_offer
            where document_id = any(%s)
            """,
            (document_ids,),
        )
        after = int(cursor.fetchone()[0])
    return max(after - before, 0)


def _start_collection(
    connection: psycopg.Connection[Any],
    *,
    from_date: date,
    to_date: date,
    bookmakers: list[str],
) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            insert into bookmaker_collection_run (
                from_date,
                to_date,
                bookmakers,
                collector_version
            )
            values (%s, %s, %s, %s)
            returning collection_id
            """,
            (from_date, to_date, bookmakers, COLLECTOR_VERSION),
        )
        return int(cursor.fetchone()[0])


def _finish_collection(
    connection: psycopg.Connection[Any],
    collection_id: int,
    *,
    discovery_count: int,
    fetched_count: int,
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
                discovery_count = %s,
                fetched_count = %s,
                parsed_document_count = %s,
                inserted_offer_count = %s,
                error_count = %s,
                metadata = %s::jsonb
            where collection_id = %s
            """,
            (
                discovery_count,
                fetched_count,
                parsed_document_count,
                inserted_offer_count,
                error_count,
                json.dumps(metadata),
                collection_id,
            ),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bookmakers",
        default="mozzart,starbet,meridian,millennium",
        help="Comma-separated: mozzart,starbet,meridian,millennium",
    )
    parser.add_argument("--from-date", type=_date_arg, default=date(2024, 9, 1))
    parser.add_argument("--to-date", type=_date_arg, default=date.today())
    parser.add_argument("--max-docs", type=int, default=80)
    parser.add_argument("--url", action="append", default=[])
    args = parser.parse_args(argv)

    bookmakers = [item.strip().lower() for item in args.bookmakers.split(",") if item.strip()]
    unsupported = sorted(set(bookmakers) - BOOKMAKER_CONFIG.keys())
    if unsupported:
        raise ValueError(f"Unsupported bookmakers: {unsupported}")
    if args.to_date < args.from_date:
        raise ValueError("to-date must be on or after from-date.")
    if not 1 <= args.max_docs <= 500:
        raise ValueError("max-docs must be between 1 and 500.")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*;q=0.8"})

    discovered = _discover_documents(
        session,
        bookmakers,
        from_date=args.from_date,
        to_date=args.to_date,
        max_docs=args.max_docs,
        explicit_urls=args.url,
    )

    settings = DatabaseSettings.from_env()
    fetched_count = 0
    parsed_document_count = 0
    inserted_offer_count = 0
    error_count = 0
    errors: list[dict[str, str]] = []
    pending_offer_rows: list[tuple[Any, ...]] = []
    identity_cache: dict[tuple[str, str], Any] = {}

    with psycopg.connect(
        settings.url(),
        autocommit=True,
        prepare_threshold=None,
    ) as connection:
        collection_id = _start_collection(
            connection,
            from_date=args.from_date,
            to_date=args.to_date,
            bookmakers=bookmakers,
        )
        candidates = _athlete_candidates(connection)

        for item in discovered:
            try:
                payload, fetched_url = _download_pdf(session, item)
                fetched_count += 1
                pages = _extract_pages(payload)
                document_date = infer_document_date(
                    item.canonical_url,
                    title=(pages[0][:1000] if pages else item.title),
                    capture_at=item.archive_capture_at,
                )
                if document_date and not (
                    args.from_date - timedelta(days=2)
                    <= document_date
                    <= args.to_date + timedelta(days=2)
                ):
                    continue

                offers = _parse_offers(item.bookmaker, pages)
                status = "parsed" if offers else "no_player_points"
                document_id = _insert_document(
                    connection,
                    item=item,
                    fetched_url=fetched_url,
                    payload=payload,
                    document_date=document_date,
                    page_count=len(pages),
                    parse_status=status,
                    metadata={"title": item.title},
                )
                if offers:
                    parsed_document_count += 1
                    pending_offer_rows.extend(
                        _prepare_offer_rows(
                            document_id=document_id,
                            offer_date=document_date,
                            offers=offers,
                            candidates=candidates,
                            identity_cache=identity_cache,
                        )
                    )
            except Exception as exc:
                error_count += 1
                errors.append(
                    {
                        "bookmaker": item.bookmaker,
                        "url": item.canonical_url,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

        inserted_offer_count = _insert_offers_bulk(
            connection,
            pending_offer_rows,
        )

        _finish_collection(
            connection,
            collection_id,
            discovery_count=len(discovered),
            fetched_count=fetched_count,
            parsed_document_count=parsed_document_count,
            inserted_offer_count=inserted_offer_count,
            error_count=error_count,
            metadata={"errors": errors[:50]},
        )

    print(
        "bookmaker_archive "
        f"discovered={len(discovered)} "
        f"fetched={fetched_count} "
        f"parsed_docs={parsed_document_count} "
        f"inserted_offers={inserted_offer_count} "
        f"errors={error_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
