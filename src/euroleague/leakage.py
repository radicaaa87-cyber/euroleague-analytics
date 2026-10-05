"""Temporal leakage guards for the player-points training dataset."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from euroleague.model_training import model_feature_columns

ROW_KEY_COLUMNS = ("season_code", "gamecode", "player_id")
TIPOFF_COLUMN = "game_tipoff_utc"
CUTOFF_COLUMN = "feature_cutoff_time"


class LeakageAuditError(RuntimeError):
    """Raised when a training dataset contains evidence of future information."""


def _index(columns: list[str]) -> dict[str, int]:
    return {name: position for position, name in enumerate(columns)}


def _require_columns(columns: list[str], required: tuple[str, ...]) -> dict[str, int]:
    index = _index(columns)
    missing = [name for name in required if name not in index]
    if missing:
        raise LeakageAuditError(
            "Leakage audit cannot run because required columns are missing: "
            + ", ".join(missing)
        )
    return index


def _same_value(left: Any, right: Any) -> bool:
    if (
        isinstance(left, float)
        and isinstance(right, float)
        and math.isnan(left)
        and math.isnan(right)
    ):
        return True
    return left == right


def assert_feature_cutoffs_before_tipoff(
    columns: list[str],
    rows: list[tuple[Any, ...]],
) -> dict[str, int]:
    """Require every row's newest historical source time to precede tipoff."""
    index = _require_columns(columns, (*ROW_KEY_COLUMNS, TIPOFF_COLUMN, CUTOFF_COLUMN))
    checked = 0
    violations: list[str] = []

    for row in rows:
        tipoff = row[index[TIPOFF_COLUMN]]
        cutoff = row[index[CUTOFF_COLUMN]]
        key = tuple(row[index[name]] for name in ROW_KEY_COLUMNS)

        if not isinstance(tipoff, datetime):
            raise LeakageAuditError(
                f"{TIPOFF_COLUMN} for {key!r} is not a timestamp: {tipoff!r}."
            )
        if not isinstance(cutoff, datetime):
            violations.append(f"{key!r}: cutoff={cutoff!r}, tipoff={tipoff!r}")
            continue
        if cutoff >= tipoff:
            violations.append(f"{key!r}: cutoff={cutoff!r}, tipoff={tipoff!r}")
        checked += 1

    if violations:
        sample = "; ".join(violations[:5])
        raise LeakageAuditError(
            f"Found {len(violations)} row(s) whose feature cutoff is missing or "
            f"not strictly before tipoff. Sample: {sample}"
        )

    return {"rows_checked": checked, "violations": 0}


def assert_prefix_invariance(
    baseline_columns: list[str],
    baseline_rows: list[tuple[Any, ...]],
    expanded_columns: list[str],
    expanded_rows: list[tuple[Any, ...]],
) -> dict[str, int]:
    """Prove that adding future rows cannot mutate already-computed features."""
    baseline_index = _require_columns(
        baseline_columns,
        (*ROW_KEY_COLUMNS, CUTOFF_COLUMN),
    )
    expanded_index = _require_columns(
        expanded_columns,
        (*ROW_KEY_COLUMNS, CUTOFF_COLUMN),
    )

    baseline_features = model_feature_columns(baseline_columns)
    expanded_features = model_feature_columns(expanded_columns)
    if baseline_features != expanded_features:
        raise LeakageAuditError(
            "Model feature schema changed between baseline and future-expanded datasets."
        )

    compare_columns = [*baseline_features, CUTOFF_COLUMN]
    expanded_by_key = {
        tuple(row[expanded_index[name]] for name in ROW_KEY_COLUMNS): row
        for row in expanded_rows
    }

    missing_keys: list[str] = []
    changed: list[str] = []
    rows_checked = 0

    for baseline_row in baseline_rows:
        key = tuple(baseline_row[baseline_index[name]] for name in ROW_KEY_COLUMNS)
        expanded_row = expanded_by_key.get(key)
        if expanded_row is None:
            missing_keys.append(repr(key))
            continue

        for column in compare_columns:
            left = baseline_row[baseline_index[column]]
            right = expanded_row[expanded_index[column]]
            if not _same_value(left, right):
                changed.append(
                    f"{key!r} {column}: baseline={left!r}, expanded={right!r}"
                )
                break
        rows_checked += 1

    if missing_keys:
        raise LeakageAuditError(
            f"{len(missing_keys)} baseline row(s) disappeared after adding future data. "
            f"Sample: {'; '.join(missing_keys[:5])}"
        )
    if changed:
        raise LeakageAuditError(
            f"{len(changed)} baseline row(s) changed after adding future data. "
            f"Sample: {'; '.join(changed[:5])}"
        )

    return {
        "rows_checked": rows_checked,
        "features_checked": len(baseline_features),
        "mutations": 0,
    }
