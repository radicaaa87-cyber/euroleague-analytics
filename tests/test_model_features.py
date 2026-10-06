"""Focused tests for the compact model bundle and one-query training export."""

from __future__ import annotations

import datetime as dt

import pytest

from euroleague import leakage
from euroleague.mcp.model_features import get_player_model_context
from euroleague.model_training import model_feature_columns, training_dataset_sql


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



def test_model_context_gamecode_attaches_weighted_pregame_role_context() -> None:
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
                    "target_tipoff_utc",
                    "target_team_code",
                    "opponent_team_code",
                    "is_home",
                    "self_out_score",
                    "self_doubt_score",
                    "self_max_source_confidence",
                    "teammate_out_vacated_minutes_l5",
                    "teammate_out_vacated_fga_l5",
                    "same_position_out_vacated_minutes_l5",
                    "teammate_max_source_confidence",
                    "context_max_source_confidence",
                    "context_feature_cutoff_time",
                ],
                [
                    (
                        dt.datetime(2026, 10, 10, 18, 0, tzinfo=dt.UTC),
                        "BAR",
                        "OLY",
                        True,
                        0,
                        0,
                        0.95,
                        18.4,
                        6.2,
                        12.1,
                        0.95,
                        0.95,
                        dt.datetime(2026, 10, 10, 9, 0, tzinfo=dt.UTC),
                    )
                ],
            ),
            (
                [
                    "opponent_l5_games",
                    "opponent_l5_off_rating",
                    "opponent_l5_def_rating",
                    "opponent_l5_possessions",
                    "opponent_last_game_date",
                ],
                [(5, 118.2, 109.7, 71.4, "2026-10-08")],
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
            "gamecode": 44,
            "as_of_date": "2026-10-10",
        },
    )

    assert "pregame_context_event" in cursor.statements[4]
    assert cursor.parameters[4] == {
        "season_code": "E2026",
        "gamecode": 44,
        "player_id": "P009862",
    }
    assert cursor.parameters[5] == ("E2026", "OLY", "2026-10-10")
    row = response["rows"][0]
    assert row["target_gamecode"] == 44
    assert row["opponent_team_code"] == "OLY"
    assert row["pregame_role_context"]["teammate_out_vacated_minutes_l5"] == 18.4
    assert row["pregame_role_context"]["context_max_source_confidence"] == 0.95


def test_model_context_rejects_nonpositive_gamecode() -> None:
    cursor = RecordingCursor(
        [
            (["season_code"], [("E2026",)]),
            (["player_id"], [("P009862",)]),
        ]
    )
    with pytest.raises(ValueError, match="positive integer"):
        get_player_model_context(
            cursor,
            {"season": "E2026", "player": "P009862", "gamecode": 0},
        )
    assert len(cursor.statements) == 2


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
    assert "partition by player_id" in sql
    assert "pre_current_season_games" in sql
    assert "partition by tg.team_code" in sql
    assert "order by game_tipoff_utc, gamecode" in sql
    assert "order by tg.utc_date, tg.gamecode" in sql
    assert "feature_cutoff_time" in sql
    assert "pre_team_l5_off_rating" in sql
    assert "pre_opponent_l5_def_rating" in sql
    assert "pre_days_rest" in sql
    assert "pre_hours_rest" in sql
    assert "pre_last_was_away" in sql
    assert "pre_l5_away_rate" in sql
    assert "pre_away_games_last_3" in sql
    assert "pre_away_games_last_7d" in sql
    assert "pre_away_games_last_14d" in sql
    assert "pre_l5_home_away_switch_rate" in sql
    assert "home_away_switch_event" in sql
    assert "pre_games_last_7d" in sql
    assert "pre_games_last_14d" in sql
    assert "pre_minutes_last_7d" in sql
    assert "pre_minutes_last_14d" in sql
    assert "pre_l3_ts_proxy" in sql
    assert "pre_l10_ts_proxy" in sql
    assert "pre_ts_trend_l3_vs_l10" in sql
    assert "hand_pre_l10_ts_mean" in sql
    assert "hand_pre_l10_ts_std" in sql
    assert "pre_last_hand_state" in sql
    assert "pre_hot_streak_games" in sql
    assert "pre_cold_streak_games" in sql
    assert "pre_avg_hot_episode_games" in sql
    assert "pre_avg_cold_episode_games" in sql
    assert "pre_hot_break_role_drop_rate" in sql
    assert "pre_hot_break_eff_reversion_rate" in sql
    assert "pre_cold_break_role_expansion_rate" in sql
    assert "pre_cold_break_eff_recovery_rate" in sql
    assert "lineup_matchup_candidate" in sql
    assert "player_game_inferred_matchup" in sql
    assert "matchup_weight" in sql
    assert "pre_player_height_cm" in sql
    assert "pre_matchup_profile_games" in sql
    assert "pre_matchup_ppm_vs_defender_height_slope" in sql
    assert "pre_ppm_vs_taller_defender_profile" in sql
    assert "pre_ppm_vs_shorter_defender_profile" in sql
    assert "pre_opponent_same_position_avg_height_cm" in sql
    assert "pre_matchup_height_diff_cm" in sql
    assert "player_game_shot_context" in sql
    assert "situational_fga" in sql
    assert "pre_l10_minutes_std" in sql
    assert "pre_l10_fga_std" in sql
    assert "pre_l10_minutes_cv" in sql
    assert "pre_l10_fga_cv" in sql
    assert "pre_minute_drop_foul_reason_share" in sql
    assert "pre_minute_drop_blowout_reason_share" in sql
    assert "pre_minute_spike_overtime_reason_share" in sql
    assert "pre_fga_spike_situational_reason_share" in sql
    assert "pre_fga_spike_role_expansion_share" in sql
    assert "pre_fga_spike_unexplained_share" in sql
    assert "v_athlete_game_history" in sql
    assert "h.source = 'ACB'" in sql
    assert "h.utc_date < pf.game_tipoff_utc" in sql
    assert "pre_acb_l5_minutes" in sql
    assert "pre_acb_l5_fga" in sql
    assert "pre_acb_l5_points" in sql
    assert "pre_acb_minutes_last_7d" in sql
    assert "pre_days_since_last_acb_game" in sql
    assert "pre_acb_vs_el_l5_minutes_gap" in sql
    assert "pre_acb_vs_el_l5_fga_gap" in sql
    assert "pre_combined_minutes_last_7d" in sql
    assert "pre_last_context_neutral_fga_delta_vs_l10" in sql
    assert "pre_last_minutes_delta_vs_l10" in sql
    assert "rows between 20 preceding and 1 preceding" in sql
    assert "rows between unbounded preceding and 1 preceding" in sql
    assert "range between interval '7 days' preceding" in sql
    assert "range between interval '14 days' preceding" in sql
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


def test_model_feature_columns_exclude_targets_ids_and_bookmaker_fields() -> None:
    columns = [
        "player_id",
        "team_code",
        "is_home",
        "pre_l10_minutes",
        "pre_l10_pbp_on_off_rating",
        "pre_team_l5_off_rating",
        "pre_opponent_l5_def_rating",
        "pre_days_rest",
        "pre_l10_ts_proxy",
        "pre_hot_streak_games",
        "pre_avg_hot_episode_games",
        "pre_hot_break_eff_reversion_rate",
        "pre_player_height_cm",
        "pre_matchup_profile_games",
        "pre_matchup_ppm_vs_defender_height_slope",
        "pre_opponent_same_position_avg_height_cm",
        "pre_matchup_height_diff_cm",
        "pre_l10_minutes_std",
        "pre_l10_fga_std",
        "pre_minute_drop_foul_reason_share",
        "pre_fga_spike_role_expansion_share",
        "pre_last_context_neutral_fga_delta_vs_l10",
        "feature_cutoff_time",
        "game_tipoff_utc",
        "target_points",
        "target_minutes",
        "bookmaker_line",
    ]
    assert model_feature_columns(columns) == [
        "is_home",
        "pre_l10_minutes",
        "pre_l10_pbp_on_off_rating",
        "pre_team_l5_off_rating",
        "pre_opponent_l5_def_rating",
        "pre_days_rest",
        "pre_l10_ts_proxy",
        "pre_hot_streak_games",
        "pre_avg_hot_episode_games",
        "pre_hot_break_eff_reversion_rate",
        "pre_player_height_cm",
        "pre_matchup_profile_games",
        "pre_matchup_ppm_vs_defender_height_slope",
        "pre_opponent_same_position_avg_height_cm",
        "pre_matchup_height_diff_cm",
        "pre_l10_minutes_std",
        "pre_l10_fga_std",
        "pre_minute_drop_foul_reason_share",
        "pre_fga_spike_role_expansion_share",
        "pre_last_context_neutral_fga_delta_vs_l10",
    ]


LEAKAGE_COLUMNS = [
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


def _leakage_row(
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
        _leakage_row(
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

    result = leakage.assert_feature_cutoffs_before_tipoff(LEAKAGE_COLUMNS, rows)

    assert result == {"rows_checked": 1, "violations": 0}


def test_cutoff_gate_rejects_source_at_or_after_tipoff() -> None:
    tipoff = dt.datetime(2025, 1, 10, 19, 30, tzinfo=dt.UTC)
    rows = [_leakage_row("E2024", 100, "P1", tipoff, tipoff, 12.0, 111.5, 15.0)]

    with pytest.raises(leakage.LeakageAuditError, match="not strictly before tipoff"):
        leakage.assert_feature_cutoffs_before_tipoff(LEAKAGE_COLUMNS, rows)


def test_prefix_invariance_accepts_future_rows_without_old_feature_changes() -> None:
    tipoff = dt.datetime(2025, 1, 10, 19, 30, tzinfo=dt.UTC)
    baseline = [
        _leakage_row(
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
        _leakage_row(
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

    result = leakage.assert_prefix_invariance(
        LEAKAGE_COLUMNS,
        baseline,
        LEAKAGE_COLUMNS,
        expanded,
    )

    assert result["rows_checked"] == 1
    assert result["features_checked"] == 3
    assert result["mutations"] == 0


def test_prefix_invariance_rejects_future_mutation_of_old_feature() -> None:
    tipoff = dt.datetime(2025, 1, 10, 19, 30, tzinfo=dt.UTC)
    baseline = [
        _leakage_row(
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
        _leakage_row(
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

    with pytest.raises(leakage.LeakageAuditError, match="changed after adding future data"):
        leakage.assert_prefix_invariance(
            LEAKAGE_COLUMNS,
            baseline,
            LEAKAGE_COLUMNS,
            expanded,
        )
