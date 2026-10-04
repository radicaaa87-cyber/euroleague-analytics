"""Match ACB player game logs to EuroLeague player identities.

This creates a reusable cross-league bridge without mixing league statistics.
It normalizes player names, applies safe exact matching first, and writes
ambiguous/unmatched names separately for manual review.

Example:
    python scripts/build_acb_el_player_bridge.py \
        --acb data/acb_processed/2026-27/player_game_logs.csv \
        --el path/to/euroleague_player_file.csv \
        --output data/cross_league/acb_el_player_bridge.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

PLAYER_COLUMNS = (
    "player",
    "game_player",
    "player_name",
    "name",
    "license_name",
)

ID_COLUMNS = (
    "player_id",
    "license_id",
    "player_code",
    "id",
)


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.upper().replace(",", " ")
    value = re.sub(r"[^A-Z0-9 ]+", " ", value)
    tokens = [x for x in value.split() if x]

    # Common basketball-name suffixes should not block a cross-source match.
    suffixes = {"JR", "SR", "II", "III", "IV"}
    while tokens and tokens[-1] in suffixes:
        tokens.pop()

    return " ".join(sorted(tokens))


def choose_column(fieldnames: Iterable[str], candidates: tuple[str, ...]) -> str:
    names = list(fieldnames)
    lower = {name.lower(): name for name in names}
    for candidate in candidates:
        if candidate.lower() in lower:
            return lower[candidate.lower()]
    raise ValueError(f"Could not find any of {candidates!r} in columns: {', '.join(names)}")


def optional_column(fieldnames: Iterable[str], candidates: tuple[str, ...]) -> str | None:
    names = list(fieldnames)
    lower = {name.lower(): name for name in names}
    for candidate in candidates:
        if candidate.lower() in lower:
            return lower[candidate.lower()]
    return None


def load_players(path: Path) -> tuple[str, str | None, list[dict[str, str]]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no header")
        player_col = choose_column(reader.fieldnames, PLAYER_COLUMNS)
        id_col = optional_column(reader.fieldnames, ID_COLUMNS)
        rows = list(reader)
    return player_col, id_col, rows


def unique_players(
    rows: list[dict[str, str]],
    player_col: str,
    id_col: str | None,
) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []
    for row in rows:
        name = (row.get(player_col) or "").strip()
        if not name:
            continue
        pid = (row.get(id_col) or "").strip() if id_col else ""
        key = (pid, name)
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--acb", required=True)
    parser.add_argument("--el", required=True)
    parser.add_argument(
        "--output",
        default="data/cross_league/acb_el_player_bridge.csv",
    )
    args = parser.parse_args()

    acb_path = Path(args.acb)
    el_path = Path(args.el)

    acb_player_col, acb_id_col, acb_rows = load_players(acb_path)
    el_player_col, el_id_col, el_rows = load_players(el_path)

    acb_players = unique_players(acb_rows, acb_player_col, acb_id_col)
    el_players = unique_players(el_rows, el_player_col, el_id_col)

    el_by_norm: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for el_id, el_name in el_players:
        el_by_norm[normalize_name(el_name)].append((el_id, el_name))

    matched: list[dict[str, str]] = []
    ambiguous: list[dict[str, str]] = []
    unmatched: list[dict[str, str]] = []

    for acb_id, acb_name in acb_players:
        norm = normalize_name(acb_name)
        candidates = el_by_norm.get(norm, [])

        if len(candidates) == 1:
            el_id, el_name = candidates[0]
            matched.append(
                {
                    "acb_player_id": acb_id,
                    "acb_player": acb_name,
                    "euroleague_player_id": el_id,
                    "euroleague_player": el_name,
                    "normalized_name": norm,
                    "match_method": "normalized_exact",
                    "status": "MATCHED",
                }
            )
        elif len(candidates) > 1:
            ambiguous.append(
                {
                    "acb_player_id": acb_id,
                    "acb_player": acb_name,
                    "normalized_name": norm,
                    "candidate_count": str(len(candidates)),
                    "candidates": " | ".join(
                        f"{pid}:{name}" if pid else name for pid, name in candidates
                    ),
                    "status": "AMBIGUOUS",
                }
            )
        else:
            unmatched.append(
                {
                    "acb_player_id": acb_id,
                    "acb_player": acb_name,
                    "normalized_name": norm,
                    "status": "UNMATCHED",
                }
            )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("w", newline="", encoding="utf-8") as handle:
        fields = [
            "acb_player_id",
            "acb_player",
            "euroleague_player_id",
            "euroleague_player",
            "normalized_name",
            "match_method",
            "status",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(matched)

    ambiguous_file = output.with_name(output.stem + "_ambiguous.csv")
    with ambiguous_file.open("w", newline="", encoding="utf-8") as handle:
        fields = [
            "acb_player_id",
            "acb_player",
            "normalized_name",
            "candidate_count",
            "candidates",
            "status",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(ambiguous)

    unmatched_file = output.with_name(output.stem + "_unmatched.csv")
    with unmatched_file.open("w", newline="", encoding="utf-8") as handle:
        fields = ["acb_player_id", "acb_player", "normalized_name", "status"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(unmatched)

    print(
        f"Matched={len(matched)} ambiguous={len(ambiguous)} unmatched={len(unmatched)} -> {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
