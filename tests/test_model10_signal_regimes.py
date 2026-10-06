"""Tests for MODEL 10 situation-signal attribution and repeatability diagnostics."""

from __future__ import annotations

import numpy as np

from euroleague.model_signal_regimes import (
    ablation_signal_contributions,
    build_signal_fingerprints,
    signal_domain_columns,
    apply_pattern_effects,
    learn_pattern_effects,
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
