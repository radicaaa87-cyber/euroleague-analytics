"""One-command bookmaker archive ingestion: ZIP/PDF -> parse -> bulk database import."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from import_bookmaker_batch import DEFAULT_CHUNK_SIZE, import_payload
from prepare_bookmaker_batch import DEFAULT_WORKERS, prepare_batch


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
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--allow-parse-errors",
        action="store_true",
        help="Import successful documents even when some source PDFs failed.",
    )
    args = parser.parse_args(argv)

    started = time.perf_counter()
    print(
        "BOOKMAKER_PIPELINE_START "
        f"bookmaker={args.bookmaker} input={args.input}",
        flush=True,
    )

    parse_started = time.perf_counter()
    payload = prepare_batch(
        source=args.input,
        bookmaker=args.bookmaker,
        default_year=args.default_year,
        workers=args.workers,
    )
    parse_seconds = time.perf_counter() - parse_started

    summary = {
        "input_pdf_count": payload["input_pdf_count"],
        "unique_pdf_count": payload["unique_pdf_count"],
        "duplicate_pdf_count": payload["duplicate_pdf_count"],
        "parsed_document_count": payload["parsed_document_count"],
        "offer_count": payload["offer_count"],
        "error_count": payload["error_count"],
        "parse_seconds": round(parse_seconds, 3),
    }
    print("BOOKMAKER_PARSE_SUMMARY=" + json.dumps(summary, sort_keys=True), flush=True)

    if payload["error_count"] and not args.allow_parse_errors:
        print(
            "BOOKMAKER_PIPELINE_ABORT parse_errors_present "
            + json.dumps(payload["errors"][:10], ensure_ascii=False),
            flush=True,
        )
        return 2

    if args.dry_run:
        print(
            "BOOKMAKER_PIPELINE_COMPLETE "
            f"mode=dry_run elapsed_seconds={time.perf_counter() - started:.3f}",
            flush=True,
        )
        return 0

    if not payload["documents"]:
        raise RuntimeError("No bookmaker documents were produced from the input.")
    if not payload["offers"]:
        raise RuntimeError(
            "No player-points offers were produced. Refusing to modify the database."
        )

    import_started = time.perf_counter()
    result = import_payload(payload, chunk_size=args.chunk_size)
    import_seconds = time.perf_counter() - import_started
    result["parse_seconds"] = round(parse_seconds, 3)
    result["import_seconds"] = round(import_seconds, 3)
    result["total_seconds"] = round(time.perf_counter() - started, 3)

    print("BOOKMAKER_IMPORT_SUMMARY=" + json.dumps(result, sort_keys=True), flush=True)
    print(
        "BOOKMAKER_PIPELINE_COMPLETE "
        f"mode=import elapsed_seconds={result['total_seconds']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
