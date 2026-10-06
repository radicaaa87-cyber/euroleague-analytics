"""Prepare large bookmaker PDF/ZIP batches for fast database ingestion.

This script is deliberately offline: it extracts and parses all source PDFs in
parallel, deduplicates them by SHA-256, and writes one JSON payload. Database
insertion can then happen in a handful of bulk operations instead of one query
per offer.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from euroleague.bookmaker_odds import (
    ParsedOffer,
    infer_document_date,
    parse_meridian_player_points_pages,
    parse_millennium_player_points_pages,
    parse_mozzart_player_points_pages,
    parse_starbet_player_points_pages,
)

PARSER_VERSION = "bookmaker_batch_v1"
MAX_PDF_BYTES = 25 * 1024 * 1024
DEFAULT_WORKERS = min(8, max(2, os.cpu_count() or 4))


def _parse_offers(bookmaker: str, pages: list[str]) -> list[ParsedOffer]:
    if bookmaker == "mozzart":
        return parse_mozzart_player_points_pages(pages)
    if bookmaker == "starbet":
        return parse_starbet_player_points_pages(pages)
    if bookmaker == "meridian":
        return parse_meridian_player_points_pages(pages)
    if bookmaker == "millennium":
        return parse_millennium_player_points_pages(pages)
    raise ValueError(f"Unsupported bookmaker: {bookmaker}")


def _read_pdf_payloads(source: Path) -> list[tuple[str, bytes]]:
    if source.is_dir():
        items: list[tuple[str, bytes]] = []
        for path in sorted(source.rglob("*.pdf")):
            payload = path.read_bytes()
            items.append((str(path.relative_to(source)), payload))
        return items

    if source.suffix.lower() == ".pdf":
        return [(source.name, source.read_bytes())]

    if source.suffix.lower() != ".zip":
        raise ValueError("Input must be a PDF, ZIP, or directory containing PDFs.")

    items = []
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".pdf"):
                continue
            if info.file_size <= 0 or info.file_size > MAX_PDF_BYTES:
                continue
            items.append((info.filename, archive.read(info)))
    return items


def _dedupe_payloads(items: list[tuple[str, bytes]]) -> list[tuple[str, bytes, str]]:
    seen: set[str] = set()
    unique: list[tuple[str, bytes, str]] = []
    for name, payload in items:
        sha256 = hashlib.sha256(payload).hexdigest()
        if sha256 in seen:
            continue
        seen.add(sha256)
        unique.append((name, payload, sha256))
    return unique


def _pages_with_pypdf(payload: bytes) -> list[str]:
    reader = PdfReader(io.BytesIO(payload))
    return [page.extract_text() or "" for page in reader.pages]


def _pages_with_pdftotext(
    *,
    payload: bytes,
    sha256: str,
    workdir: Path,
) -> list[str]:
    executable = shutil.which("pdftotext")
    if executable is None:
        return _pages_with_pypdf(payload)

    pdf_path = workdir / f"{sha256}.pdf"
    text_path = workdir / f"{sha256}.txt"
    pdf_path.write_bytes(payload)
    completed = subprocess.run(
        [
            executable,
            "-layout",
            "-enc",
            "UTF-8",
            str(pdf_path),
            str(text_path),
        ],
        check=False,
        capture_output=True,
        timeout=45,
    )
    if completed.returncode != 0 or not text_path.exists():
        return _pages_with_pypdf(payload)

    text = text_path.read_text(encoding="utf-8", errors="replace")
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages or [""]


def _infer_date(
    *,
    filename: str,
    pages: list[str],
    default_year: int,
) -> str | None:
    capture_at = datetime(default_year, 1, 1, tzinfo=UTC)
    title = pages[0][:1600] if pages else filename
    inferred = infer_document_date(
        filename,
        title=title,
        capture_at=capture_at,
    )
    return inferred.isoformat() if inferred else None


def _process_document(
    *,
    bookmaker: str,
    filename: str,
    payload: bytes,
    sha256: str,
    default_year: int,
    workdir: Path,
) -> dict[str, Any]:
    pages = _pages_with_pdftotext(
        payload=payload,
        sha256=sha256,
        workdir=workdir,
    )
    offers = _parse_offers(bookmaker, pages)
    document_date = _infer_date(
        filename=filename,
        pages=pages,
        default_year=default_year,
    )
    canonical_url = f"manual://sha256/{bookmaker}/{sha256}.pdf"

    return {
        "document": {
            "bookmaker": bookmaker,
            "canonical_url": canonical_url,
            "fetched_url": canonical_url,
            "discovery_method": "manual",
            "document_date": document_date,
            "content_sha256": sha256,
            "content_length": len(payload),
            "page_count": len(pages),
            "parser_version": PARSER_VERSION,
            "parse_status": "parsed" if offers else "no_player_points",
            "metadata": {
                "original_filename": filename,
                "batch_parser": PARSER_VERSION,
            },
        },
        "offers": [
            {
                **asdict(offer),
                "event_time_local": offer.event_time_local.isoformat(),
                "row_sha256": offer.row_sha256,
                "offer_date": document_date,
                "document_sha256": sha256,
            }
            for offer in offers
        ],
    }


def prepare_batch(
    *,
    source: Path,
    bookmaker: str,
    default_year: int,
    workers: int,
) -> dict[str, Any]:
    raw = _read_pdf_payloads(source)
    unique = _dedupe_payloads(raw)
    documents: list[dict[str, Any]] = []
    offers: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    with tempfile.TemporaryDirectory(prefix="bookmaker-batch-") as temp_name:
        workdir = Path(temp_name)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _process_document,
                    bookmaker=bookmaker,
                    filename=filename,
                    payload=payload,
                    sha256=sha256,
                    default_year=default_year,
                    workdir=workdir,
                ): filename
                for filename, payload, sha256 in unique
            }
            for future in as_completed(futures):
                filename = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    errors.append(
                        {
                            "filename": filename,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue
                documents.append(result["document"])
                offers.extend(result["offers"])

    documents.sort(
        key=lambda row: (
            row["document_date"] or "",
            row["metadata"]["original_filename"],
        )
    )
    offers.sort(
        key=lambda row: (
            row["offer_date"] or "",
            row["document_sha256"],
            row["page_number"],
            row["participant_text"],
        )
    )

    return {
        "version": PARSER_VERSION,
        "bookmaker": bookmaker,
        "source_name": source.name,
        "input_pdf_count": len(raw),
        "unique_pdf_count": len(unique),
        "duplicate_pdf_count": len(raw) - len(unique),
        "parsed_document_count": sum(row["parse_status"] == "parsed" for row in documents),
        "offer_count": len(offers),
        "error_count": len(errors),
        "documents": documents,
        "offers": offers,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument(
        "--bookmaker",
        required=True,
        choices=("mozzart", "starbet", "meridian", "millennium"),
    )
    parser.add_argument("--default-year", type=int, required=True)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.workers < 1 or args.workers > 32:
        raise ValueError("--workers must be between 1 and 32")

    result = prepare_batch(
        source=args.input,
        bookmaker=args.bookmaker,
        default_year=args.default_year,
        workers=args.workers,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        "bookmaker_batch "
        f"input_pdfs={result['input_pdf_count']} "
        f"unique_pdfs={result['unique_pdf_count']} "
        f"parsed_docs={result['parsed_document_count']} "
        f"offers={result['offer_count']} "
        f"errors={result['error_count']} "
        f"output={urllib.parse.quote(str(args.output))}"
    )
    return 0 if result["error_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
