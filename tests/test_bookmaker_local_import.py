"""Tests for fast local bookmaker PDF batch discovery."""

from __future__ import annotations

import zipfile
from pathlib import Path

from euroleague.bookmaker_local_archive import iter_pdf_payloads


def test_iter_pdf_payloads_reads_directory_once_and_skips_non_pdf(tmp_path: Path) -> None:
    (tmp_path / "a.pdf").write_bytes(b"%PDF-one")
    (tmp_path / "b.PDF").write_bytes(b"%PDF-two")
    (tmp_path / "note.txt").write_text("ignore", encoding="utf-8")

    rows = list(iter_pdf_payloads(tmp_path))

    assert [(row.name, row.payload) for row in rows] == [
        ("a.pdf", b"%PDF-one"),
        ("b.PDF", b"%PDF-two"),
    ]


def test_iter_pdf_payloads_reads_zip_without_extracting_to_disk(tmp_path: Path) -> None:
    archive = tmp_path / "offers.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("folder/a.pdf", b"%PDF-one")
        handle.writestr("folder/b.PDF", b"%PDF-two")
        handle.writestr("folder/readme.txt", b"ignore")

    rows = list(iter_pdf_payloads(archive))

    assert [(row.name, row.payload) for row in rows] == [
        ("folder/a.pdf", b"%PDF-one"),
        ("folder/b.PDF", b"%PDF-two"),
    ]


def test_iter_pdf_payloads_rejects_non_pdf_input(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("not a PDF", encoding="utf-8")

    try:
        list(iter_pdf_payloads(path))
    except ValueError as exc:
        assert "PDF, ZIP, or directory" in str(exc)
    else:
        raise AssertionError("non-PDF input should be rejected")
