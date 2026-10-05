from __future__ import annotations

import datetime as dt
from euroleague import leakage


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
    tipoff: dt.datetime,
    cutoff: dt.datetime,
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
    tipoff = dt.datetime(2025, 1, 10, 19, 30, tzinfo=dt.UTC)
    rows = [
        _row(
            "E2024",
            100,
            "P1",
            tipoff,
            tipoff - dt.timedelta(days=3),
            12.0,
            111.5,
            15.0,
        )
    ]

    result = leakage.assert_feature_cutoffs_before_tipoff(COLUMNS, rows)

    assert result == {"rows_checked": 1, "violations": 0}


def test_cutoff_gate_rejects_source_at_or_after_tipoff() -> None:
    tipoff = dt.datetime(2025, 1, 10, 19, 30, tzinfo=dt.UTC)
    rows = [_row("E2024", 100, "P1", tipoff, tipoff, 12.0, 111.5, 15.0)]

    try:
        leakage.assert_feature_cutoffs_before_tipoff(COLUMNS, rows)
    except leakage.LeakageAuditError as error:
        assert "not strictly before tipoff" in str(error)
    else:
        raise AssertionError("Leakage cutoff gate accepted a source at tipoff.")


def test_prefix_invariance_accepts_future_rows_without_old_feature_changes() -> None:
    tipoff = dt.datetime(2025, 1, 10, 19, 30, tzinfo=dt.UTC)
    baseline = [
        _row(
            "E2024",
            100,
            "P1",
            tipoff,
            tipoff - dt.timedelta(days=3),
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
            tipoff + dt.timedelta(days=250),
            tipoff + dt.timedelta(days=240),
            14.0,
            108.0,
            17.0,
        ),
    ]

    result = leakage.assert_prefix_invariance(COLUMNS, baseline, COLUMNS, expanded)

    assert result["rows_checked"] == 1
    assert result["features_checked"] == 3
    assert result["mutations"] == 0


def test_prefix_invariance_rejects_future_mutation_of_old_feature() -> None:
    tipoff = dt.datetime(2025, 1, 10, 19, 30, tzinfo=dt.UTC)
    baseline = [
        _row(
            "E2024",
            100,
            "P1",
            tipoff,
            tipoff - dt.timedelta(days=3),
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
            tipoff - dt.timedelta(days=3),
            13.0,
            111.5,
            15.0,
        )
    ]

    try:
        leakage.assert_prefix_invariance(COLUMNS, baseline, COLUMNS, expanded)
    except leakage.LeakageAuditError as error:
        assert "changed after adding future data" in str(error)
    else:
        raise AssertionError("Prefix invariance gate accepted a mutated historical feature.")
