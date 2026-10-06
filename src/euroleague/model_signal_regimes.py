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

    if (
        "matchup" in name
        or "opponent_same_position" in name
        or "opponent_rotation" in name
    ):
        return "matchup_opponent"

    if (
        name.startswith("pre_team_")
        or name.startswith("pre_opponent_l5_")
        or "pace" in name
        or "possessions" in name
    ) and not name.startswith("pre_role2_"):
        return "pace_environment"

    if (
        "top_pair" in name
        or "top_triple" in name
        or "top_lineup" in name
        or "lineup" in name
    ):
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

    return {
        domain: tuple(grouped[domain])
        for domain in SIGNAL_DOMAIN_ORDER
        if grouped[domain]
    }


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
        positions = [
            index
            for index, row in enumerate(fingerprints)
            if row["signal_tier"] == tier
        ]
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
