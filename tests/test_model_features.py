"""Focused tests for the compact model bundle and one-query training export."""

from __future__ import annotations

import pytest

from euroleague.mcp.model_features import get_player_model_context
from euroleague.model_training import training_dataset_sql


class RecordingCursor:
    def __init__(self, answers: list[tuple[list[str], list[tuple]]]) -> None:
        self.answers = answers
        self.statements: list[str] = []
        self.parameters: list[tuple] = []
        self.description: list[tuple] = []
        self._rows: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.statements.append(sql)
        self.parameters.append(params)
        columns, rows = self.answers.pop(0)
        self.description = [(name,) for name in columns]
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return list(self._rows)


def test_model_context_applies_as_of_date_before_rolling_features() -> None:
    cursor = RecordingCursor(
        [
            (["season_code"], [("E2026",)]),
            (["player_id"], [("P009862",)]),
            (
                ["player_id", "player_name", "history_games", "l10_minutes"],
                [("P009862", "PUNTER, KEVIN", 10, 25.1)],
            ),
            (
                [
                    "pbp_history_games",
                    "l10_pbp_offensive_possessions",
                    "l10_pbp_stint_count",
                ],
                [(10, 48.2, 22.1)],
            ),
            (
                [
                    "games_included",
                    "total_games",
                    "first_game",
                    "last_game",
                    "scheduled_games",
                    "last_loaded_at",
                ],
                [(30, 30, "2026-09-24", "2026-10-02", 380, None)],
            ),
            (["reason", "games"], []),
            (["games"], [(0,)]),
        ]
    )

    response = get_player_model_context(
        cursor,
        {
            "season": "E2026",
            "player": "P009862",
            "as_of_date": "2026-10-05",
        },
    )

    summary_sql = cursor.statements[2]
    assert "p.utc_date::date < %s" in summary_sql
    assert cursor.parameters[2] == ("E2026", "P009862", "2026-10-05", 10)
    pbp_sql = cursor.statements[3]
    assert "complete reconstructed" not in pbp_sql.lower()
    assert "v_possession" in pbp_sql
    assert "lineup_stint" in pbp_sql
    assert "pg.utc_date::date < %s" in pbp_sql
    assert cursor.parameters[3] == (
        "E2026",
        "P009862",
        "2026-10-05",
        10,
        "E2026",
        "E2026",
        "E2026",
    )
    assert response["row_count"] == 1
    assert response["rows"][0]["pbp_history_games"] == 10
    assert response["minutes_basis"]["value"] == "official"
    assert "bookmaker" in " ".join(response["caveats"]).lower()


def test_model_context_rejects_a_short_lookback_before_summary_query() -> None:
    cursor = RecordingCursor(
        [
            (["season_code"], [("E2026",)]),
            (["player_id"], [("P009862",)]),
        ]
    )
    with pytest.raises(ValueError, match="between 10 and 20"):
        get_player_model_context(
            cursor,
            {"season": "E2026", "player": "P009862", "lookback": 5},
        )
    assert len(cursor.statements) == 2


def test_training_query_never_uses_target_game_in_rolling_windows() -> None:
    sql = training_dataset_sql("official").lower()
    assert "rows between 3 preceding and 1 preceding" in sql
    assert "rows between 5 preceding and 1 preceding" in sql
    assert "rows between 10 preceding and 1 preceding" in sql
    assert "target_points" in sql
    assert "v_possession" in sql
    assert "v_lineup_player" in sql
    assert "lineup_stint" in sql
    assert "pre_l10_pbp_on_off_rating" in sql
    assert "pre_l5_pbp_primary_lineup_share" in sql
    assert "transition_offense" not in sql
    assert "bookmaker" not in sql
    assert "central_line" not in sql


def test_training_query_supports_all_three_minutes_bases() -> None:
    assert "p.seconds_official as seconds_played" in training_dataset_sql("official")
    assert "p.seconds_corrected as seconds_played" in training_dataset_sql("corrected")
    assert "p.seconds_raw as seconds_played" in training_dataset_sql("raw")


def test_training_query_rejects_unknown_minutes_basis() -> None:
    with pytest.raises(ValueError, match="minutes_basis"):
        training_dataset_sql("invented")
