"""MODEL 10 situation-signal attribution for player-points validation.

This module does not replace the point predictor. It explains the predictor by
grouping correlated pre-game features into basketball situation domains, then
measuring how much each domain changes the selected model's prediction for each
row. The resulting fingerprints can be audited for repeatability without using
bookmaker lines or future information as model inputs.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np

SIGNAL_DOMAIN_ORDER = (
    "role_volume",
    "rotation",
    "availability",
    "matchup_opponent",
    "pace_environment",
    "schedule_load",
    "lineup",
    "efficiency_state",
    "transition",
)


def _signal_domain(feature: str) -> str | None:
    """Map one feature to one independent basketball situation domain."""
    name = feature.lower()

    if name.startswith("pre_rotation_"):
        return "rotation"

    if (
        name.startswith("pre_context_")
        or name.startswith("pre_self_")
        or name.startswith("pre_teammate_")
    ):
        return "availability"

    if "matchup" in name or "opponent_same_position" in name or "opponent_rotation" in name:
        return "matchup_opponent"

    if (
        name.startswith("pre_team_")
        or name.startswith("pre_opponent_l5_")
        or "pace" in name
        or "possessions" in name
    ) and not name.startswith("pre_role2_"):
        return "pace_environment"

    if "top_pair" in name or "top_triple" in name or "top_lineup" in name or "lineup" in name:
        return "lineup"

    if name.startswith("pre_transition_"):
        return "transition"

    if name.startswith("pre_acb_"):
        if any(
            token in name
            for token in (
                "games_last_7d",
                "minutes_last_7d",
                "days_since",
            )
        ):
            return "schedule_load"
        return "transition"

    if name.startswith("pre_combined_"):
        return "schedule_load"

    if any(
        token in name
        for token in (
            "travel",
            "rest",
            "games_last_7d",
            "games_last_14d",
            "minutes_last_7d",
            "minutes_last_14d",
            "days_since",
            "hours_rest",
        )
    ):
        return "schedule_load"

    if any(
        token in name
        for token in (
            "hot_",
            "cold_",
            "hand_state",
            "ts_trend",
            "ts_proxy",
            "eff_reversion",
            "eff_recovery",
            "2p_pct",
            "3p_pct",
            "ft_pct",
        )
    ):
        return "efficiency_state"

    if name.startswith("pre_role2_"):
        return "role_volume"

    if any(
        token in name
        for token in (
            "minutes",
            "fga",
            "3pa",
            "fta",
            "starter",
            "points",
            "option_rank",
            "scoring_opportun",
            "team_fga_share",
        )
    ):
        return "role_volume"

    return None


def signal_domain_columns(feature_names: list[str]) -> dict[str, tuple[int, ...]]:
    """Return feature-column indices grouped into independent situation domains."""
    grouped: dict[str, list[int]] = {domain: [] for domain in SIGNAL_DOMAIN_ORDER}
    for index, feature in enumerate(feature_names):
        domain = _signal_domain(feature)
        if domain is not None:
            grouped[domain].append(index)

    return {domain: tuple(grouped[domain]) for domain in SIGNAL_DOMAIN_ORDER if grouped[domain]}


def _reference_medians(reference_x: np.ndarray) -> np.ndarray:
    """Build one leakage-safe neutral reference value per feature."""
    reference = np.asarray(reference_x, dtype=float)
    if reference.ndim != 2:
        raise ValueError("reference_x must be a two-dimensional feature matrix.")

    medians = np.zeros(reference.shape[1], dtype=float)
    for column in range(reference.shape[1]):
        finite = reference[:, column][np.isfinite(reference[:, column])]
        medians[column] = float(np.median(finite)) if len(finite) else 0.0
    return medians


def ablation_signal_contributions(
    *,
    model: Any,
    reference_x: np.ndarray,
    evaluation_x: np.ndarray,
    feature_names: list[str],
    residual_scale: float,
    row_gate: np.ndarray,
) -> dict[str, np.ndarray]:
    """Measure each situation domain by neutralizing that domain and re-predicting."""
    evaluation = np.asarray(evaluation_x, dtype=float)
    if evaluation.ndim != 2:
        raise ValueError("evaluation_x must be a two-dimensional feature matrix.")
    if evaluation.shape[1] != len(feature_names):
        raise ValueError("feature_names must match evaluation_x columns.")

    gate = np.asarray(row_gate, dtype=float)
    if gate.shape != (evaluation.shape[0],):
        raise ValueError("row_gate must contain one value per evaluation row.")

    domains = signal_domain_columns(feature_names)
    medians = _reference_medians(reference_x)
    full_raw = np.asarray(model.predict(evaluation), dtype=float)
    contributions: dict[str, np.ndarray] = {}

    for domain, columns in domains.items():
        ablated = evaluation.copy()
        ablated[:, list(columns)] = medians[list(columns)]
        ablated_raw = np.asarray(model.predict(ablated), dtype=float)
        contributions[domain] = (full_raw - ablated_raw) * float(residual_scale) * gate

    return contributions


def build_signal_fingerprints(
    contributions: dict[str, np.ndarray],
    final_delta: np.ndarray,
    *,
    contribution_floor_points: float = 0.25,
) -> list[dict[str, Any]]:
    """Create one auditable situation fingerprint and 0/1/2/3+ tier per row."""
    if contribution_floor_points <= 0:
        raise ValueError("contribution_floor_points must be positive.")

    delta = np.asarray(final_delta, dtype=float)
    row_count = len(delta)
    for domain, values in contributions.items():
        if np.asarray(values).shape != (row_count,):
            raise ValueError(f"Contribution array for {domain} has the wrong shape.")

    rows: list[dict[str, Any]] = []
    for row_index in range(row_count):
        direction = 1 if delta[row_index] > 0 else (-1 if delta[row_index] < 0 else 0)
        supporting: list[str] = []
        opposing: list[str] = []
        material_parts: list[str] = []
        domain_points: dict[str, float] = {}

        for domain in SIGNAL_DOMAIN_ORDER:
            if domain not in contributions:
                continue
            value = float(np.asarray(contributions[domain])[row_index])
            domain_points[domain] = value
            if not np.isfinite(value) or abs(value) < contribution_floor_points:
                continue

            material_parts.append(f"{domain}:{'+' if value > 0 else '-'}")
            value_direction = 1 if value > 0 else -1
            if direction != 0 and value_direction == direction:
                supporting.append(domain)
            elif direction != 0:
                opposing.append(domain)

        supporting_count = len(supporting)
        tier = "3+" if supporting_count >= 3 else str(supporting_count)
        rows.append(
            {
                "signal_tier": tier,
                "supporting_signal_count": supporting_count,
                "supporting_domains": supporting,
                "opposing_domains": opposing,
                "fingerprint": "|".join(material_parts) if material_parts else "none",
                "domain_contribution_points": domain_points,
                "model_delta_points": float(delta[row_index]),
            }
        )

    return rows


def _directional_hits(
    actual: np.ndarray,
    naive: np.ndarray,
    predicted: np.ndarray,
) -> np.ndarray:
    """Return whether the model correction moved toward the realized scoring direction."""
    actual_delta = np.asarray(actual, dtype=float) - np.asarray(naive, dtype=float)
    model_delta = np.asarray(predicted, dtype=float) - np.asarray(naive, dtype=float)
    return (actual_delta * model_delta) > 0.0


def summarize_signal_tiers(
    *,
    actual: np.ndarray,
    naive: np.ndarray,
    predicted: np.ndarray,
    fingerprints: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize validation behavior by independent supporting-signal count."""
    hits = _directional_hits(actual, naive, predicted)
    if len(fingerprints) != len(hits):
        raise ValueError("fingerprints must contain one row per prediction.")

    summary: dict[str, Any] = {}
    for tier in ("0", "1", "2", "3+"):
        positions = [index for index, row in enumerate(fingerprints) if row["signal_tier"] == tier]
        if not positions:
            summary[tier] = {
                "rows": 0,
                "directional_correction_hit_rate": None,
            }
            continue

        tier_hits = hits[np.asarray(positions, dtype=int)]
        summary[tier] = {
            "rows": len(positions),
            "directional_correction_hit_rate": float(np.mean(tier_hits)),
        }

    summary["metric_semantics"] = {
        "directional_correction_hit_rate": (
            "Whether the model correction from the naive projection moved in the same "
            "direction as the actual result. This is not bookmaker-line betting hit rate."
        )
    }
    return summary


def summarize_repeating_patterns(
    *,
    actual: np.ndarray,
    naive: np.ndarray,
    predicted: np.ndarray,
    fingerprints: list[dict[str, Any]],
    min_occurrences: int = 3,
) -> list[dict[str, Any]]:
    """Rank repeated situation fingerprints by frequency and directional reliability."""
    if min_occurrences < 2:
        raise ValueError("min_occurrences must be at least 2.")

    hits = _directional_hits(actual, naive, predicted)
    actual_delta = np.asarray(actual, dtype=float) - np.asarray(naive, dtype=float)
    model_delta = np.asarray(predicted, dtype=float) - np.asarray(naive, dtype=float)
    if len(fingerprints) != len(hits):
        raise ValueError("fingerprints must contain one row per prediction.")

    positions_by_pattern: dict[str, list[int]] = defaultdict(list)
    tier_by_pattern: dict[str, str] = {}
    for index, row in enumerate(fingerprints):
        fingerprint = str(row["fingerprint"])
        if fingerprint == "none":
            continue
        positions_by_pattern[fingerprint].append(index)
        tier_by_pattern[fingerprint] = str(row["signal_tier"])

    rows: list[dict[str, Any]] = []
    for fingerprint, positions in positions_by_pattern.items():
        if len(positions) < min_occurrences:
            continue

        selected = np.asarray(positions, dtype=int)
        rows.append(
            {
                "fingerprint": fingerprint,
                "signal_tier": tier_by_pattern[fingerprint],
                "occurrences": len(positions),
                "directional_correction_hit_rate": float(np.mean(hits[selected])),
                "mean_model_delta_points": float(np.mean(model_delta[selected])),
                "mean_realized_delta_points": float(np.mean(actual_delta[selected])),
            }
        )

    rows.sort(
        key=lambda item: (
            item["occurrences"],
            item["directional_correction_hit_rate"],
        ),
        reverse=True,
    )
    return rows


def summarize_pattern_stability(
    *,
    actual: np.ndarray,
    naive: np.ndarray,
    predicted: np.ndarray,
    fingerprints: list[dict[str, Any]],
    groups: dict[str, list[Any]],
    min_occurrences: int = 20,
) -> list[dict[str, Any]]:
    """Measure whether repeated patterns survive game/player/team clustering."""
    if min_occurrences < 2:
        raise ValueError("min_occurrences must be at least 2.")

    hits = _directional_hits(actual, naive, predicted)
    row_count = len(hits)
    if len(fingerprints) != row_count:
        raise ValueError("fingerprints must contain one row per prediction.")
    for name, values in groups.items():
        if len(values) != row_count:
            raise ValueError(f"group {name} must contain one value per prediction.")

    positions_by_pattern: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(fingerprints):
        fingerprint = str(row["fingerprint"])
        if fingerprint != "none":
            positions_by_pattern[fingerprint].append(index)

    rows: list[dict[str, Any]] = []
    for fingerprint, positions in positions_by_pattern.items():
        if len(positions) < min_occurrences:
            continue

        selected = np.asarray(positions, dtype=int)
        unique_groups: dict[str, int] = {}
        balanced_rates: dict[str, float] = {}
        for name, values in groups.items():
            hit_buckets: dict[str, list[bool]] = defaultdict(list)
            for position in positions:
                hit_buckets[str(values[position])].append(bool(hits[position]))
            unique_groups[name] = len(hit_buckets)
            balanced_rates[name] = float(
                np.mean([np.mean(bucket) for bucket in hit_buckets.values()])
            )

        rows.append(
            {
                "fingerprint": fingerprint,
                "occurrences": len(positions),
                "row_directional_hit_rate": float(np.mean(hits[selected])),
                "unique_groups": unique_groups,
                "cluster_balanced_hit_rate": balanced_rates,
            }
        )

    rows.sort(
        key=lambda item: (
            item["occurrences"],
            item["row_directional_hit_rate"],
        ),
        reverse=True,
    )
    return rows


def learn_pattern_effects(
    *,
    actual: np.ndarray,
    naive: np.ndarray,
    predicted: np.ndarray,
    fingerprints: list[dict[str, Any]],
    min_occurrences: int = 20,
    prior_strength: float = 20.0,
) -> dict[str, dict[str, Any]]:
    """Learn shrunk pattern-level delta corrections from training-only OOF rows."""
    if min_occurrences < 2:
        raise ValueError("min_occurrences must be at least 2.")
    if prior_strength <= 0:
        raise ValueError("prior_strength must be positive.")

    actual_values = np.asarray(actual, dtype=float)
    naive_values = np.asarray(naive, dtype=float)
    predicted_values = np.asarray(predicted, dtype=float)
    if not (len(actual_values) == len(naive_values) == len(predicted_values) == len(fingerprints)):
        raise ValueError("actual, naive, predicted and fingerprints must align.")

    model_delta = predicted_values - naive_values
    realized_delta = actual_values - naive_values
    hits = _directional_hits(actual_values, naive_values, predicted_values)

    positions_by_pattern: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(fingerprints):
        fingerprint = str(row["fingerprint"])
        if fingerprint != "none":
            positions_by_pattern[fingerprint].append(index)

    learned: dict[str, dict[str, Any]] = {}
    for fingerprint, positions in positions_by_pattern.items():
        if len(positions) < min_occurrences:
            continue

        selected = np.asarray(positions, dtype=int)
        mean_model = float(np.mean(model_delta[selected]))
        mean_realized = float(np.mean(realized_delta[selected]))
        raw_correction = mean_realized - mean_model
        weight = float(len(positions) / (len(positions) + prior_strength))
        stable_direction = bool(
            mean_model != 0.0
            and mean_realized != 0.0
            and np.sign(mean_model) == np.sign(mean_realized)
        )
        learned[fingerprint] = {
            "occurrences": len(positions),
            "mean_model_delta_points": mean_model,
            "mean_realized_delta_points": mean_realized,
            "directional_correction_hit_rate": float(np.mean(hits[selected])),
            "raw_correction_points": raw_correction,
            "shrinkage_weight": weight,
            "calibration_correction_points": raw_correction * weight if stable_direction else 0.0,
            "stable_direction": stable_direction,
        }

    return learned


def apply_pattern_effects(
    *,
    naive: np.ndarray,
    predicted: np.ndarray,
    fingerprints: list[dict[str, Any]],
    learned_effects: dict[str, dict[str, Any]],
) -> np.ndarray:
    """Apply training-only pattern calibration without replacing row-level prediction."""
    naive_values = np.asarray(naive, dtype=float)
    predicted_values = np.asarray(predicted, dtype=float)
    if len(naive_values) != len(predicted_values) or len(fingerprints) != len(predicted_values):
        raise ValueError("naive, predicted and fingerprints must align.")

    calibrated = predicted_values.copy()
    for index, row in enumerate(fingerprints):
        learned = learned_effects.get(str(row["fingerprint"]))
        if not learned or not bool(learned.get("stable_direction")):
            continue
        calibrated[index] += float(learned["calibration_correction_points"])
    return calibrated


def _feature_column(
    x: np.ndarray,
    feature_names: list[str],
    name: str,
    *,
    default: float = np.nan,
) -> np.ndarray:
    """Return one feature column or a default vector when the feature is unavailable."""
    matrix = np.asarray(x, dtype=float)
    if matrix.ndim != 2:
        raise ValueError("x must be a two-dimensional feature matrix.")
    try:
        position = feature_names.index(name)
    except ValueError:
        return np.full(matrix.shape[0], default, dtype=float)
    return np.asarray(matrix[:, position], dtype=float)


def classify_efficiency_cycles(
    *,
    x: np.ndarray,
    feature_names: list[str],
) -> list[str]:
    """Classify pre-game hot/cold state by phase rather than treating it as one signal.

    The classifier uses only existing pre-game features. It distinguishes an early hot
    run from a player-relative peak that may regress, a visibly cooling hot run, a cold
    decline with shrinking volume, and a cold recovery.
    """
    matrix = np.asarray(x, dtype=float)
    if matrix.ndim != 2 or matrix.shape[1] != len(feature_names):
        raise ValueError("feature_names must match x columns.")

    hand = _feature_column(matrix, feature_names, "pre_last_hand_state", default=0.0)
    last_ts_delta = _feature_column(
        matrix,
        feature_names,
        "pre_last_ts_delta_vs_prior_l10",
    )
    hot_streak = _feature_column(matrix, feature_names, "pre_hot_streak_games", default=0.0)
    avg_hot = _feature_column(matrix, feature_names, "pre_avg_hot_episode_games")
    max_hot = _feature_column(matrix, feature_names, "pre_max_hot_episode_games")

    l3_ts = _feature_column(matrix, feature_names, "pre_l3_ts_proxy")
    l5_ts = _feature_column(matrix, feature_names, "pre_l5_ts_proxy")
    l10_ts = _feature_column(matrix, feature_names, "pre_l10_ts_proxy")
    l3_fga = _feature_column(matrix, feature_names, "pre_l3_fga")
    l5_fga = _feature_column(matrix, feature_names, "pre_l5_fga")
    l10_fga = _feature_column(matrix, feature_names, "pre_l10_fga")
    l3_minutes = _feature_column(matrix, feature_names, "pre_l3_minutes")
    l5_minutes = _feature_column(matrix, feature_names, "pre_l5_minutes")
    l10_minutes = _feature_column(matrix, feature_names, "pre_l10_minutes")

    labels: list[str] = []
    for row in range(matrix.shape[0]):
        avg_episode = avg_hot[row]
        max_episode = max_hot[row]
        maturity_games = max(
            2.0,
            0.8 * avg_episode if np.isfinite(avg_episode) and avg_episode > 0 else 3.0,
        )
        near_max_games = max(
            2.0,
            0.8 * max_episode if np.isfinite(max_episode) and max_episode > 0 else 4.0,
        )

        ts_short_gap = (
            l3_ts[row] - l10_ts[row]
            if np.isfinite(l3_ts[row]) and np.isfinite(l10_ts[row])
            else np.nan
        )
        ts_cooling = bool(
            np.isfinite(l3_ts[row]) and np.isfinite(l5_ts[row]) and l3_ts[row] <= l5_ts[row] - 0.015
        )
        ts_recovering = bool(
            np.isfinite(l3_ts[row]) and np.isfinite(l5_ts[row]) and l3_ts[row] >= l5_ts[row] + 0.015
        )
        ts_extreme = bool(
            (np.isfinite(ts_short_gap) and ts_short_gap >= 0.05)
            or (np.isfinite(last_ts_delta[row]) and last_ts_delta[row] >= 0.10)
        )

        fga_falling = bool(
            (
                np.isfinite(l3_fga[row])
                and np.isfinite(l5_fga[row])
                and l3_fga[row] <= l5_fga[row] - 0.5
            )
            or (
                np.isfinite(l3_fga[row])
                and np.isfinite(l10_fga[row])
                and l3_fga[row] <= l10_fga[row] - 0.75
            )
        )
        minutes_falling = bool(
            (
                np.isfinite(l3_minutes[row])
                and np.isfinite(l5_minutes[row])
                and l3_minutes[row] <= l5_minutes[row] - 1.5
            )
            or (
                np.isfinite(l3_minutes[row])
                and np.isfinite(l10_minutes[row])
                and l3_minutes[row] <= l10_minutes[row] - 2.0
            )
        )
        volume_falling = fga_falling or minutes_falling

        volume_not_rising = bool(
            (
                not np.isfinite(l3_fga[row])
                or not np.isfinite(l5_fga[row])
                or l3_fga[row] <= l5_fga[row] + 0.25
            )
            and (
                not np.isfinite(l3_minutes[row])
                or not np.isfinite(l5_minutes[row])
                or l3_minutes[row] <= l5_minutes[row] + 1.0
            )
        )
        volume_recovering = bool(
            (np.isfinite(l3_fga[row]) and np.isfinite(l5_fga[row]) and l3_fga[row] >= l5_fga[row])
            or (
                np.isfinite(l3_minutes[row])
                and np.isfinite(l5_minutes[row])
                and l3_minutes[row] >= l5_minutes[row]
            )
        )

        if hand[row] > 0:
            personal_avg_hot = (
                avg_episode if np.isfinite(avg_episode) and avg_episode > 0 else 3.0
            )
            personal_max_hot = (
                max_episode if np.isfinite(max_episode) and max_episode > 0 else 5.0
            )
            episode_age_ratio = hot_streak[row] / max(personal_avg_hot, 1.0)
            max_age_ratio = hot_streak[row] / max(personal_max_hot, 1.0)

            personal_efficiency_excess = max(
                last_ts_delta[row] if np.isfinite(last_ts_delta[row]) else -np.inf,
                ts_short_gap if np.isfinite(ts_short_gap) else -np.inf,
            )
            extreme_vs_personal_baseline = bool(
                personal_efficiency_excess >= 0.08
                and (
                    (np.isfinite(ts_short_gap) and ts_short_gap >= 0.04)
                    or (np.isfinite(last_ts_delta[row]) and last_ts_delta[row] >= 0.10)
                )
            )
            mature_for_player = episode_age_ratio >= 1.0
            near_personal_max = max_age_ratio >= 0.75
            early_for_player = episode_age_ratio < 0.75 and max_age_ratio < 0.50

            if ts_cooling and volume_falling:
                labels.append("hot_cooling_decline")
            elif (
                (mature_for_player or near_personal_max)
                and extreme_vs_personal_baseline
                and volume_not_rising
            ):
                labels.append("hot_peak_regression")
            elif early_for_player and (
                (np.isfinite(last_ts_delta[row]) and last_ts_delta[row] > 0)
                or ts_recovering
                or ts_extreme
            ):
                labels.append("hot_start")
            else:
                labels.append("hot_mature")
            continue

        if hand[row] < 0:
            if ts_recovering and volume_recovering:
                labels.append("cold_recovery")
            elif volume_falling:
                labels.append("cold_decline")
            else:
                labels.append("cold_efficiency_only")
            continue

        labels.append("neutral")

    return labels


def learn_efficiency_cycle_effects(
    *,
    actual: np.ndarray,
    predicted: np.ndarray,
    labels: list[str],
    min_occurrences: int = 20,
    prior_strength: float = 20.0,
) -> dict[str, dict[str, Any]]:
    """Learn shrunk post-model modifiers for predefined hot/cold cycle phases."""
    if min_occurrences < 2:
        raise ValueError("min_occurrences must be at least 2.")
    if prior_strength <= 0:
        raise ValueError("prior_strength must be positive.")

    actual_values = np.asarray(actual, dtype=float)
    predicted_values = np.asarray(predicted, dtype=float)
    if len(actual_values) != len(predicted_values) or len(labels) != len(actual_values):
        raise ValueError("actual, predicted and labels must align.")

    residual = actual_values - predicted_values
    positions_by_label: dict[str, list[int]] = defaultdict(list)
    for index, label in enumerate(labels):
        if label != "neutral":
            positions_by_label[str(label)].append(index)

    learned: dict[str, dict[str, Any]] = {}
    for label, positions in positions_by_label.items():
        if len(positions) < min_occurrences:
            continue
        selected = np.asarray(positions, dtype=int)
        mean_residual = float(np.mean(residual[selected]))
        weight = float(len(positions) / (len(positions) + prior_strength))
        learned[label] = {
            "occurrences": len(positions),
            "mean_residual_points": mean_residual,
            "residual_std_points": float(np.std(residual[selected])),
            "shrinkage_weight": weight,
            "modifier_points": mean_residual * weight,
        }
    return learned


def apply_efficiency_cycle_effects(
    *,
    predicted: np.ndarray,
    labels: list[str],
    learned_effects: dict[str, dict[str, Any]],
) -> np.ndarray:
    """Apply training-only hot/cold cycle modifiers to row-level predictions."""
    values = np.asarray(predicted, dtype=float)
    if len(values) != len(labels):
        raise ValueError("predicted and labels must align.")

    adjusted = values.copy()
    for index, label in enumerate(labels):
        learned = learned_effects.get(str(label))
        if learned is None:
            continue
        adjusted[index] += float(learned["modifier_points"])
    return adjusted


def summarize_efficiency_cycles(
    *,
    actual: np.ndarray,
    naive: np.ndarray,
    predicted: np.ndarray,
    labels: list[str],
) -> list[dict[str, Any]]:
    """Summarize realized scoring behavior for each hot/cold cycle phase."""
    actual_values = np.asarray(actual, dtype=float)
    naive_values = np.asarray(naive, dtype=float)
    predicted_values = np.asarray(predicted, dtype=float)
    if not (len(actual_values) == len(naive_values) == len(predicted_values) == len(labels)):
        raise ValueError("actual, naive, predicted and labels must align.")

    actual_delta = actual_values - naive_values
    model_delta = predicted_values - naive_values
    hits = _directional_hits(actual_values, naive_values, predicted_values)

    rows: list[dict[str, Any]] = []
    for label in sorted(set(labels)):
        positions = [index for index, value in enumerate(labels) if value == label]
        if not positions:
            continue
        selected = np.asarray(positions, dtype=int)
        rows.append(
            {
                "state": label,
                "occurrences": len(positions),
                "mean_model_delta_points": float(np.mean(model_delta[selected])),
                "mean_realized_delta_points": float(np.mean(actual_delta[selected])),
                "mean_model_residual_points": float(
                    np.mean(actual_values[selected] - predicted_values[selected])
                ),
                "directional_correction_hit_rate": float(np.mean(hits[selected])),
            }
        )
    rows.sort(key=lambda item: item["occurrences"], reverse=True)
    return rows
