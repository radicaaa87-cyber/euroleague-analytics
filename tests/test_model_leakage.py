from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from euroleague.leakage import (
    LeakageAuditError,
    assert_feature_cutoffs_before_tipoff,
    assert_prefix_invariance,
)


COLUMNS = [
    "season_code",
    "gamecode",
    "game_tipoff_utc",
    "feature_cutoff_time",
    "player_id",
    "is_home",
    "pre_l10_points",
    "pre_opponent_l5_def_rating",
    "target_points",
]


def _row(
    season: str,
    gamecode: int,
    player: str,
    tipoff: datetime,
    cutoff: datetime,
    pre_points: float,
    opponent_def: float,
    target: float,
) -> tuple:
    return (
        season,
        gamecode,
        tipoff,
        cutoff,
        player,
        True,
        pre_points,
        opponent_def,
        target,
    )


def test_cutoff_gate_accepts_strictly_historical_sources() -> None:
    tipoff = datetime(2025, 1, 10, 19, 30, tzinfo=UTC)
    rows = [
        _row(
            "E2024",
            100,
            "P1",
            tipoff,
            tipoff - timedelta(days=3),
            12.0,
            111.5,
            15.0,
        )
    ]

    result = assert_feature_cutoffs_before_tipoff(COLUMNS, rows)

    assert result == {"rows_checked": 1, "violations": 0}


def test_cutoff_gate_rejects_source_at_or_after_tipoff() -> None:
    tipoff = datetime(2025, 1, 10, 19, 30, tzinfo=UTC)
    rows = [_row("E2024", 100, "P1", tipoff, tipoff, 12.0, 111.5, 15.0)]

    with pytest.raises(LeakageAuditError, match="not strictly before tipoff"):
        assert_feature_cutoffs_before_tipoff(COLUMNS, rows)


def test_prefix_invariance_accepts_future_rows_without_old_feature_changes() -> None:
    tipoff = datetime(2025, 1, 10, 19, 30, tzinfo=UTC)
    baseline = [
        _row(
            "E2024",
            100,
            "P1",
            tipoff,
            tipoff - timedelta(days=3),
            12.0,
            111.5,
            15.0,
        )
    ]
    expanded = [
        *baseline,
        _row(
            "E2025",
            1,
            "P1",
            tipoff + timedelta(days=250),
            tipoff + timedelta(days=240),
            14.0,
            108.0,
            17.0,
        ),
    ]

    result = assert_prefix_invariance(COLUMNS, baseline, COLUMNS, expanded)

    assert result["rows_checked"] == 1
    assert result["features_checked"] == 3
    assert result["mutations"] == 0


def test_prefix_invariance_rejects_future_mutation_of_old_feature() -> None:
    tipoff = datetime(2025, 1, 10, 19, 30, tzinfo=UTC)
    baseline = [
        _row(
            "E2024",
            100,
            "P1",
            tipoff,
            tipoff - timedelta(days=3),
            12.0,
            111.5,
            15.0,
        )
    ]
    expanded = [
        _row(
            "E2024",
            100,
            "P1",
            tipoff,
            tipoff - timedelta(days=3),
            13.0,
            111.5,
            15.0,
        )
    ]

    with pytest.raises(LeakageAuditError, match="changed after adding future data"):
        assert_prefix_invariance(COLUMNS, baseline, COLUMNS, expanded)
