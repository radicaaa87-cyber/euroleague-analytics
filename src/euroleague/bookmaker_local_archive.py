"""Local bookmaker PDF archive helpers.

User-supplied PDFs are already the source material. This module deliberately
avoids web discovery and yields each PDF payload exactly once from a file,
directory, or ZIP archive.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import zipfile


@dataclass(frozen=True)
class LocalPdfPayload:
    name: str
    payload: bytes


def iter_pdf_payloads(input_path: Path) -> list[LocalPdfPayload]:
    """Read PDF payloads once from one PDF, a directory, or a ZIP archive."""
    path = Path(input_path)

    if path.is_dir():
        files = sorted(
            item
            for item in path.rglob("*")
            if item.is_file() and item.suffix.lower() == ".pdf"
        )
        return [LocalPdfPayload(name=item.relative_to(path).as_posix(), payload=item.read_bytes()) for item in files]

    if path.is_file() and path.suffix.lower() == ".pdf":
        return [LocalPdfPayload(name=path.name, payload=path.read_bytes())]

    if path.is_file() and path.suffix.lower() == ".zip":
        rows: list[LocalPdfPayload] = []
        with zipfile.ZipFile(path) as handle:
            for name in sorted(handle.namelist()):
                if name.lower().endswith(".pdf") and not name.endswith("/"):
                    rows.append(LocalPdfPayload(name=name, payload=handle.read(name)))
        return rows

    raise ValueError("input must be a PDF, ZIP, or directory containing PDFs")
