"""Tests for MODEL 10 situation-signal attribution and repeatability diagnostics."""

from __future__ import annotations

import numpy as np

from euroleague.model_signal_regimes import (
    ablation_signal_contributions,
    apply_pattern_effects,
    build_signal_fingerprints,
    classify_efficiency_cycles,
    cold_context_feature_columns,
    cold_context_temporal_stability,
    diagnostic_feature_columns,
    filter_noisy_signal_contributions,
    learn_efficiency_cycle_effects,
    learn_pattern_effects,
    signal_domain_columns,
    summarize_pattern_stability,
    summarize_repeating_patterns,
    summarize_signal_tiers,
)


class _LinearModel:
    """Tiny deterministic model used to prove ablation arithmetic."""

    def __init__(self, weights: list[float]) -> None:
        self.weights = np.asarray(weights, dtype=float)

    def predict(self, x: np.ndarray) -> np.ndarray:
        return np.asarray(x, dtype=float) @ self.weights


def test_signal_domains_keep_related_features_in_one_independent_confirmation() -> None:
    features = [
        "pre_role2_fga_per_100_trend_l3_vs_l10",
        "pre_minutes_trend_l3_vs_l10",
        "pre_rotation_first_stint_trend_l3_vs_l10",
        "pre_teammate_out_vacated_fga_l5",
        "pre_matchup_height_diff_cm",
        "pre_acb_minutes_last_7d",
        "pre_transition_team_changed",
        "pre_top_pair_shared_minutes",
    ]

    domains = signal_domain_columns(features)

    assert domains["role_volume"] == (0, 1)
    assert domains["rotation"] == (2,)
    assert domains["availability"] == (3,)
    assert domains["matchup_opponent"] == (4,)
    assert domains["schedule_load"] == (5,)
    assert domains["transition"] == (6,)
    assert domains["lineup"] == (7,)


def test_ablation_measures_domain_contribution_without_double_counting_features() -> None:
    features = [
        "pre_role2_fga_per_100_trend_l3_vs_l10",
        "pre_minutes_trend_l3_vs_l10",
        "pre_rotation_first_stint_trend_l3_vs_l10",
        "pre_teammate_out_vacated_fga_l5",
    ]
    reference = np.zeros((3, 4), dtype=float)
    evaluation = np.asarray([[1.0, 2.0, 3.0, 4.0]], dtype=float)
    model = _LinearModel([1.0, 1.0, 1.0, 1.0])

    contributions = ablation_signal_contributions(
        model=model,
        reference_x=reference,
        evaluation_x=evaluation,
        feature_names=features,
        residual_scale=0.5,
        row_gate=np.asarray([1.0]),
    )

    # Two role features are one independent role/volume confirmation.
    assert contributions["role_volume"].tolist() == [1.5]
    assert contributions["rotation"].tolist() == [1.5]
    assert contributions["availability"].tolist() == [2.0]


def test_three_aligned_domains_become_three_plus_signal_tier() -> None:
    contributions = {
        "role_volume": np.asarray([0.70]),
        "rotation": np.asarray([0.40]),
        "availability": np.asarray([0.35]),
        "matchup_opponent": np.asarray([-0.55]),
    }
    final_delta = np.asarray([2.0])

    fingerprints = build_signal_fingerprints(
        contributions,
        final_delta,
        contribution_floor_points=0.25,
    )

    row = fingerprints[0]
    assert row["signal_tier"] == "3+"
    assert row["supporting_signal_count"] == 3
    assert row["supporting_domains"] == [
        "role_volume",
        "rotation",
        "availability",
    ]
    assert row["opposing_domains"] == ["matchup_opponent"]


def test_tier_summary_reports_directional_correction_hit_rate_not_market_hit_rate() -> None:
    actual = np.asarray([14.0, 8.0, 15.0, 7.0])
    naive = np.asarray([10.0, 10.0, 10.0, 10.0])
    predicted = np.asarray([12.0, 9.0, 12.5, 11.0])
    fingerprints = [
        {"signal_tier": "3+", "fingerprint": "role_volume:+|rotation:+|availability:+"},
        {"signal_tier": "3+", "fingerprint": "role_volume:-|rotation:-|schedule_load:-"},
        {"signal_tier": "2", "fingerprint": "role_volume:+|rotation:+"},
        {"signal_tier": "2", "fingerprint": "role_volume:+|rotation:+"},
    ]

    summary = summarize_signal_tiers(
        actual=actual,
        naive=naive,
        predicted=predicted,
        fingerprints=fingerprints,
    )

    assert summary["3+"]["rows"] == 2
    assert summary["3+"]["directional_correction_hit_rate"] == 1.0
    assert summary["2"]["rows"] == 2
    assert summary["2"]["directional_correction_hit_rate"] == 0.5
    assert summary["metric_semantics"]["directional_correction_hit_rate"] == (
        "Whether the model correction from the naive projection moved in the same "
        "direction as the actual result. This is not bookmaker-line betting hit rate."
    )


def test_repeating_patterns_are_ranked_only_after_minimum_repeat_count() -> None:
    actual = np.asarray([14.0, 13.0, 8.0, 12.0, 9.0])
    naive = np.asarray([10.0, 10.0, 10.0, 10.0, 10.0])
    predicted = np.asarray([12.0, 11.0, 12.0, 11.0, 9.0])
    fingerprints = [
        {"signal_tier": "3+", "fingerprint": "role_volume:+|rotation:+|availability:+"},
        {"signal_tier": "3+", "fingerprint": "role_volume:+|rotation:+|availability:+"},
        {"signal_tier": "3+", "fingerprint": "role_volume:+|rotation:+|availability:+"},
        {"signal_tier": "2", "fingerprint": "role_volume:+|rotation:+"},
        {"signal_tier": "1", "fingerprint": "schedule_load:-"},
    ]

    patterns = summarize_repeating_patterns(
        actual=actual,
        naive=naive,
        predicted=predicted,
        fingerprints=fingerprints,
        min_occurrences=3,
    )

    assert len(patterns) == 1
    assert patterns[0]["fingerprint"] == "role_volume:+|rotation:+|availability:+"
    assert patterns[0]["occurrences"] == 3
    assert patterns[0]["directional_correction_hit_rate"] == 2 / 3


def test_pattern_stability_reports_unique_games_players_and_cluster_balanced_rates() -> None:
    actual = np.asarray([8.0, 8.0, 12.0, 12.0])
    naive = np.asarray([10.0, 10.0, 10.0, 10.0])
    predicted = np.asarray([9.0, 9.0, 9.0, 11.0])
    fingerprints = [
        {"signal_tier": "2", "fingerprint": "role_volume:-|efficiency_state:-"},
        {"signal_tier": "2", "fingerprint": "role_volume:-|efficiency_state:-"},
        {"signal_tier": "2", "fingerprint": "role_volume:-|efficiency_state:-"},
        {"signal_tier": "2", "fingerprint": "role_volume:-|efficiency_state:-"},
    ]

    stability = summarize_pattern_stability(
        actual=actual,
        naive=naive,
        predicted=predicted,
        fingerprints=fingerprints,
        groups={
            "game": ["g1", "g1", "g2", "g3"],
            "player": ["p1", "p2", "p1", "p3"],
            "team": ["a", "b", "a", "c"],
        },
        min_occurrences=2,
    )

    row = stability[0]
    assert row["occurrences"] == 4
    assert row["unique_groups"] == {"game": 3, "player": 3, "team": 3}
    assert row["row_directional_hit_rate"] == 0.75
    assert row["cluster_balanced_hit_rate"]["game"] == (1.0 + 0.0 + 1.0) / 3.0


def test_pattern_effect_learning_shrinks_training_only_correction() -> None:
    actual = np.asarray([7.0, 8.0, 9.0, 8.0])
    naive = np.asarray([10.0, 10.0, 10.0, 10.0])
    predicted = np.asarray([9.0, 9.0, 9.0, 9.0])
    fingerprints = [
        {"fingerprint": "role_volume:-|efficiency_state:-"},
        {"fingerprint": "role_volume:-|efficiency_state:-"},
        {"fingerprint": "role_volume:-|efficiency_state:-"},
        {"fingerprint": "role_volume:-|efficiency_state:-"},
    ]

    learned = learn_pattern_effects(
        actual=actual,
        naive=naive,
        predicted=predicted,
        fingerprints=fingerprints,
        min_occurrences=3,
        prior_strength=4.0,
    )

    row = learned["role_volume:-|efficiency_state:-"]
    assert row["occurrences"] == 4
    assert row["mean_model_delta_points"] == -1.0
    assert row["mean_realized_delta_points"] == -2.0
    assert row["raw_correction_points"] == -1.0
    assert row["shrinkage_weight"] == 0.5
    assert row["calibration_correction_points"] == -0.5
    assert row["stable_direction"] is True


def test_pattern_effect_application_preserves_row_specific_delta() -> None:
    naive = np.asarray([10.0, 10.0])
    predicted = np.asarray([9.2, 8.7])
    fingerprints = [
        {"fingerprint": "role_volume:-|efficiency_state:-"},
        {"fingerprint": "unknown"},
    ]
    learned = {
        "role_volume:-|efficiency_state:-": {
            "occurrences": 40,
            "stable_direction": True,
            "calibration_correction_points": -0.4,
        }
    }

    calibrated = apply_pattern_effects(
        naive=naive,
        predicted=predicted,
        fingerprints=fingerprints,
        learned_effects=learned,
    )

    assert np.allclose(calibrated, [8.8, 8.7])



def test_noise_filter_keeps_only_causally_confirmed_positive_signals() -> None:
    features = [
        "pre_l3_fga",
        "pre_l5_fga",
        "pre_l3_minutes",
        "pre_l5_minutes",
    ]
    x = np.asarray(
        [
            [10.0, 10.0, 28.0, 28.0],
            [12.0, 10.0, 31.0, 28.0],
            [8.0, 10.0, 24.0, 28.0],
        ],
        dtype=float,
    )
    contributions = {
        "role_volume": np.asarray([0.7, 0.7, -0.7]),
        "rotation": np.asarray([0.5, 0.5, -0.5]),
        "pace_environment": np.asarray([0.4, 0.4, -0.4]),
        "schedule_load": np.asarray([0.6, 0.6, -0.6]),
        "efficiency_state": np.asarray([0.8, 0.8, -0.8]),
    }

    filtered = filter_noisy_signal_contributions(
        contributions=contributions,
        x=x,
        feature_names=features,
        efficiency_labels=["hot_start", "cold_regression_up", "hot_peak_regression"],
    )

    # Weak/generic positive signals are neutralized.
    assert filtered["role_volume"][0] == 0.0
    assert filtered["rotation"][0] == 0.0
    assert filtered["pace_environment"][0] == 0.0
    assert filtered["schedule_load"][0] == 0.0
    assert filtered["efficiency_state"][0] == 0.0

    # Positive role/rotation need real rising volume; efficiency+ needs cold regression up.
    assert filtered["role_volume"][1] == 0.7
    assert filtered["rotation"][1] == 0.5
    assert filtered["pace_environment"][1] == 0.0
    assert filtered["schedule_load"][1] == 0.0
    assert filtered["efficiency_state"][1] == 0.8

    # Negative role/load survive; efficiency- survives only for a valid regression/decline state.
    assert filtered["role_volume"][2] == -0.7
    assert filtered["rotation"][2] == -0.5
    assert filtered["pace_environment"][2] == -0.4
    assert filtered["schedule_load"][2] == -0.6
    assert filtered["efficiency_state"][2] == -0.8


def test_cold_regression_up_uses_personal_episode_age_and_preserved_volume() -> None:
    features = [
        "pre_last_hand_state",
        "pre_last_ts_delta_vs_prior_l10",
        "pre_cold_streak_games",
        "pre_avg_cold_episode_games",
        "pre_max_cold_episode_games",
        "pre_l3_ts_proxy",
        "pre_l5_ts_proxy",
        "pre_l10_ts_proxy",
        "pre_l3_fga",
        "pre_l5_fga",
        "pre_l10_fga",
        "pre_l3_minutes",
        "pre_l5_minutes",
        "pre_l10_minutes",
    ]
    x = np.asarray(
        [
            # Long/extreme cold run, but opportunity is still expanding.
            [-1, -0.11, 4, 3, 5, 0.47, 0.50, 0.57, 12, 10, 10, 31, 28, 28],
            # Same cold shooting, but role is shrinking: this is decline, not rebound.
            [-1, -0.11, 4, 3, 5, 0.47, 0.50, 0.57, 8, 10, 10, 24, 28, 28],
            # Too early relative to this player's own cold-history duration.
            [-1, -0.11, 1, 4, 7, 0.47, 0.50, 0.57, 12, 10, 10, 31, 28, 28],
        ],
        dtype=float,
    )

    labels = classify_efficiency_cycles(x=x, feature_names=features)

    assert labels == [
        "cold_regression_up",
        "cold_decline",
        "cold_efficiency_only",
    ]


def test_efficiency_modifier_applies_only_expected_regression_directions() -> None:
    predicted = np.asarray([10.0, 10.0, 10.0, 10.0])
    labels = [
        "hot_start",
        "hot_peak_regression",
        "cold_regression_up",
        "cold_decline",
    ]
    learned = {
        "hot_start": {"modifier_points": 0.8},
        "hot_peak_regression": {"modifier_points": -0.5},
        "cold_regression_up": {"modifier_points": 0.7},
        # Wrong sign: a cold decline must never be converted into a PLUS modifier.
        "cold_decline": {"modifier_points": 0.6},
    }

    adjusted = apply_efficiency_cycle_effects(
        predicted=predicted,
        labels=labels,
        learned_effects=learned,
    )

    assert np.allclose(adjusted, [10.0, 9.5, 10.7, 10.0])

def test_efficiency_cycle_classifier_uses_player_personal_hot_history() -> None:
    features = [
        "pre_last_hand_state",
        "pre_last_ts_delta_vs_prior_l10",
        "pre_hot_streak_games",
        "pre_cold_streak_games",
        "pre_avg_hot_episode_games",
        "pre_avg_cold_episode_games",
        "pre_max_hot_episode_games",
        "pre_max_cold_episode_games",
        "pre_l3_ts_proxy",
        "pre_l5_ts_proxy",
        "pre_l10_ts_proxy",
        "pre_l3_fga",
        "pre_l5_fga",
        "pre_l10_fga",
        "pre_l3_minutes",
        "pre_l5_minutes",
        "pre_l10_minutes",
    ]
    x = np.asarray(
        [
            # Same high efficiency, but early versus this player's own hot history.
            [1, 0.10, 2, 0, 5, 0, 9, 0, 0.68, 0.65, 0.58, 12, 11, 10, 30, 29, 28],
            # Hot streak is mature for this player, extreme vs personal L10, volume flat.
            [1, 0.12, 4, 0, 3, 0, 5, 0, 0.68, 0.66, 0.58, 10, 10, 9, 28, 28, 27],
            # Efficiency is already cooling and role volume is falling.
            [1, 0.09, 4, 0, 3, 0, 5, 0, 0.60, 0.65, 0.58, 8, 11, 10, 24, 28, 27],
            [-1, -0.09, 0, 2, 0, 3, 0, 5, 0.49, 0.52, 0.57, 7, 9, 10, 22, 25, 27],
        ],
        dtype=float,
    )

    labels = classify_efficiency_cycles(x=x, feature_names=features)

    assert labels == [
        "hot_start",
        "hot_peak_regression",
        "hot_cooling_decline",
        "cold_decline",
    ]


def test_efficiency_cycle_effect_learning_can_make_mature_hot_a_minus_modifier() -> None:
    actual = np.asarray([8.0, 9.0, 10.0, 11.0])
    predicted = np.asarray([10.0, 10.0, 10.0, 10.0])
    labels = [
        "hot_peak_regression",
        "hot_peak_regression",
        "hot_start",
        "hot_start",
    ]

    learned = learn_efficiency_cycle_effects(
        actual=actual,
        predicted=predicted,
        labels=labels,
        min_occurrences=2,
        prior_strength=2.0,
    )

    regression = learned["hot_peak_regression"]
    assert regression["mean_residual_points"] == -1.5
    assert regression["shrinkage_weight"] == 0.5
    assert regression["modifier_points"] == -0.75


def test_pattern_effect_temporal_stability_blocks_sign_flip() -> None:
    actual = np.asarray([11.0, 11.0, 8.0, 8.0])
    naive = np.asarray([10.0, 10.0, 10.0, 10.0])
    predicted = np.asarray([10.0, 10.0, 10.0, 10.0])
    fingerprints = [{"fingerprint": "role_volume:+"}] * 4

    learned = learn_pattern_effects(
        actual=actual,
        naive=naive,
        predicted=predicted,
        fingerprints=fingerprints,
        temporal_blocks=[1, 1, 2, 2],
        temporal_min_block_occurrences=2,
        min_occurrences=4,
        prior_strength=4.0,
    )

    row = learned["role_volume:+"]
    assert row["temporal_block_mean_corrections"] == {"1": 1.0, "2": -2.0}
    assert row["temporal_stability_passed"] is False
    assert row["calibration_correction_points"] == 0.0


def test_efficiency_effect_uses_stable_temporal_blocks_not_season_average() -> None:
    actual = np.asarray([11.0, 11.0, 11.0, 12.0, 12.0, 12.0])
    predicted = np.asarray([10.0] * 6)
    labels = ["cold_regression_up"] * 6

    learned = learn_efficiency_cycle_effects(
        actual=actual,
        predicted=predicted,
        labels=labels,
        temporal_blocks=[1, 1, 1, 2, 2, 2],
        temporal_min_block_occurrences=3,
        min_occurrences=6,
        prior_strength=6.0,
    )

    row = learned["cold_regression_up"]
    assert row["temporal_stability_passed"] is True
    assert row["temporal_block_mean_residuals"] == {"1": 1.0, "2": 2.0}
    # Learned effect is the block-balanced median (1.5), then shrinkage 0.5.
    assert row["temporal_effect_points"] == 1.5
    assert row["modifier_points"] == 0.75


def test_efficiency_effect_temporal_sign_flip_neutralizes_modifier() -> None:
    actual = np.asarray([11.0, 11.0, 11.0, 9.0, 9.0, 9.0])
    predicted = np.asarray([10.0] * 6)
    labels = ["cold_regression_up"] * 6

    learned = learn_efficiency_cycle_effects(
        actual=actual,
        predicted=predicted,
        labels=labels,
        temporal_blocks=[1, 1, 1, 2, 2, 2],
        temporal_min_block_occurrences=3,
        min_occurrences=6,
        prior_strength=6.0,
    )

    row = learned["cold_regression_up"]
    assert row["temporal_stability_passed"] is False
    assert row["modifier_points"] == 0.0


def test_full_diagnostic_feature_export_keeps_all_pregame_columns() -> None:
    columns = [
        "season_code",
        "gamecode",
        "is_home",
        "pre_l3_fga",
        "pre_l5_minutes",
        "pre_role2_l5_option_rank",
        "target_points",
        "target_fga",
    ]

    assert diagnostic_feature_columns(columns) == [
        "is_home",
        "pre_l3_fga",
        "pre_l5_minutes",
        "pre_role2_l5_option_rank",
    ]


def test_cold_context_uses_role_volume_and_option_context_without_fixed_effects() -> None:
    columns = [
        "pre_last_hand_state",
        "pre_cold_streak_games",
        "pre_last_ts_delta_vs_prior_l10",
        "pre_minutes_trend_l3_vs_l10",
        "pre_fga_trend_l3_vs_l10",
        "pre_role2_team_fga_share_trend_l3_vs_l10",
        "pre_role2_l5_team_scoring_opportunity_share",
        "pre_role2_l5_option_rank",
        "pre_role2_l5_top2_option_rate",
        "pre_opponent_l5_def_rating",
    ]

    selected = cold_context_feature_columns(columns)

    assert "pre_cold_streak_games" in selected
    assert "pre_minutes_trend_l3_vs_l10" in selected
    assert "pre_fga_trend_l3_vs_l10" in selected
    assert "pre_role2_l5_option_rank" in selected
    assert "pre_role2_l5_team_scoring_opportunity_share" in selected
    assert "pre_opponent_l5_def_rating" not in selected


def test_cold_context_temporal_stability_requires_improvement_in_each_evaluable_block() -> None:
    residual = np.asarray([1.0, 2.0, -1.0, -2.0, 1.0, 2.0, -1.0, -2.0])
    correction = np.asarray([0.8, 1.5, -0.7, -1.4, 0.7, 1.4, -0.8, -1.5])
    blocks = [1, 1, 1, 1, 2, 2, 2, 2]

    result = cold_context_temporal_stability(
        residual=residual,
        correction=correction,
        temporal_blocks=blocks,
        min_block_rows=4,
    )

    assert result["passed"] is True
    assert result["eligible_blocks"] == 2
    assert all(row["mae_improvement_points"] > 0 for row in result["blocks"])


def test_cold_context_temporal_stability_rejects_one_bad_time_block() -> None:
    residual = np.asarray([1.0, 2.0, -1.0, -2.0, 1.0, 2.0, -1.0, -2.0])
    correction = np.asarray([0.8, 1.5, -0.7, -1.4, -1.0, -2.0, 1.0, 2.0])
    blocks = [1, 1, 1, 1, 2, 2, 2, 2]

    result = cold_context_temporal_stability(
        residual=residual,
        correction=correction,
        temporal_blocks=blocks,
        min_block_rows=4,
    )

    assert result["passed"] is False
    assert result["blocks"][1]["mae_improvement_points"] < 0
