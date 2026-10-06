"""Validation-only model discovery for EuroLeague player points.

This phase intentionally never queries the blind E2025 season. It trains on E2023,
selects model families/configurations on E2024, and reports which pre-game signal
families carry validation value. Only after review should a separate blind run be
allowed to open E2025 outcomes.
"""

from __future__ import annotations

import argparse
import csv
import faulthandler
import json
import math
import os
import pickle
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.inspection import permutation_importance
from train_player_points_ml import (
    _as_float,
    _fetch_dataset,
    _finite_median,
    _group_permutation_importance,
    _metric_summary,
    _season_mask,
)

from euroleague.feature_provenance import provenance_manifest
from euroleague.leakage import assert_feature_cutoffs_before_tipoff, assert_prefix_invariance
from euroleague.ml_benchmark import build_model, candidate_specs, runtime_model_identity
from euroleague.model_signal_regimes import (
    ablation_signal_contributions,
    apply_pattern_effects,
    build_signal_fingerprints,
    learn_pattern_effects,
    signal_domain_columns,
    summarize_pattern_stability,
    summarize_repeating_patterns,
    summarize_signal_tiers,
)
from euroleague.model_training import model_feature_columns
from euroleague.model_validation import (
    placebo_target_audit,
    rolling_time_audit,
    segment_stability_audit,
)
from euroleague.role_projection import role_base_projection

MIN_TRAIN_FEATURE_COVERAGE = 0.05
POINT_RESIDUAL_SHRINKAGE_GRID = (0.25, 0.5, 0.75, 1.0)
POINT_STABILITY_FOLDS = 3
ENGINE_FEATURE_PREFIXES = {
    "role2": "pre_role2_",
    "rotation": "pre_rotation_",
    "transition": "pre_transition_",
}

REGIME_GATE_FLOOR_GRID = (1.0, 0.65, 0.35)
MATCHUP_SHRINKAGE_GRID = (0.0, 0.25, 0.5, 0.75)

PROGRESS_HEARTBEAT_SECONDS = 60
PROGRESS_STALL_WARNING_SECONDS = 300
PROGRESS_HARD_TIMEOUT_SECONDS = 1200

MODEL10_PATTERN_OOF_BLOCKS = 4
MODEL10_PATTERN_MIN_OCCURRENCES = 20
MODEL10_PATTERN_PRIOR_STRENGTH = 20.0

_PROGRESS_LOCK = threading.Lock()
_PROGRESS_PHASE = "startup"
_PROGRESS_STARTED = time.monotonic()


def _set_progress(phase: str) -> None:
    global _PROGRESS_PHASE, _PROGRESS_STARTED

    now = time.monotonic()
    with _PROGRESS_LOCK:
        previous = _PROGRESS_PHASE
        previous_started = _PROGRESS_STARTED
        _PROGRESS_PHASE = phase
        _PROGRESS_STARTED = now

    print(
        f"TRAIN_PHASE_END phase={previous} elapsed_seconds={now - previous_started:.1f}",
        flush=True,
    )
    print(f"TRAIN_PHASE_START phase={phase}", flush=True)


def _start_progress_watchdog() -> threading.Event:
    stop = threading.Event()

    def worker() -> None:
        last_dump_key: tuple[str, int] | None = None
        while not stop.wait(PROGRESS_HEARTBEAT_SECONDS):
            with _PROGRESS_LOCK:
                phase = _PROGRESS_PHASE
                started = _PROGRESS_STARTED
            elapsed = time.monotonic() - started
            print(
                f"TRAIN_HEARTBEAT phase={phase} elapsed_seconds={elapsed:.1f}",
                flush=True,
            )

            if elapsed >= PROGRESS_STALL_WARNING_SECONDS:
                bucket = int(elapsed // PROGRESS_STALL_WARNING_SECONDS)
                dump_key = (phase, bucket)
                if dump_key != last_dump_key:
                    print(
                        f"TRAIN_STALL_WARNING phase={phase} elapsed_seconds={elapsed:.1f}",
                        flush=True,
                    )
                    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
                    last_dump_key = dump_key

            if elapsed >= PROGRESS_HARD_TIMEOUT_SECONDS:
                print(
                    "TRAIN_HARD_TIMEOUT "
                    f"phase={phase} elapsed_seconds={elapsed:.1f} "
                    f"limit_seconds={PROGRESS_HARD_TIMEOUT_SECONDS}",
                    flush=True,
                )
                os._exit(124)

    thread = threading.Thread(
        target=worker,
        name="training-progress-watchdog",
        daemon=True,
    )
    thread.start()
    return stop


REGIME_FEATURE_CANDIDATES = (
    "pre_minutes_trend_l3_vs_l10",
    "pre_fga_trend_l3_vs_l10",
    "pre_last_minutes_delta_vs_l10",
    "pre_last_fga_delta_vs_l10",
    "pre_l5_starter_rate",
    "pre_l10_starter_rate",
    "pre_role2_fga_per_100_trend_l3_vs_l10",
    "pre_role2_l5_option_rank",
    "pre_role2_l5_primary_option_rate",
    "pre_role2_l5_top2_option_rate",
    "pre_role2_l5_max_teammate_off_fga_uplift",
    "pre_rotation_first_stint_trend_l3_vs_l10",
    "pre_rotation_l5_first_exit_elapsed_minutes",
    "pre_rotation_l5_close_game_rate",
    "pre_rotation_closing_share_trend_l3_vs_l10",
)
MATCHUP_FEATURE_CANDIDATES = (
    "pre_opponent_l5_def_rating",
    "pre_opponent_l5_possessions",
    "pre_opponent_l5_off_rating",
    "pre_team_l5_possessions",
    "is_home",
    "pre_l10_3pa_share",
    "pre_l10_fga_per_minute",
    "pre_l10_2p_pct",
    "pre_l10_3p_pct",
    "pre_l10_ft_pct",
    "pre_l5_fta",
    "pre_l5_3pa",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Train on E2023, select on E2024, and report signal importance without "
            "querying or scoring E2025."
        )
    )
    parser.add_argument("--train-season", default="E2023")
    parser.add_argument("--validation-season", default="E2024")
    parser.add_argument("--blind-season", default="E2025")
    parser.add_argument(
        "--context-season",
        action="append",
        default=[],
        help=(
            "Historical context season used only to build pre-game features. "
            "Repeat if needed; context rows are never train/validation targets."
        ),
    )
    parser.add_argument(
        "--minutes-basis",
        default="official",
        choices=("official", "corrected", "raw"),
    )
    parser.add_argument("--min-history-games", type=int, default=3)
    parser.add_argument(
        "--enabled-engine",
        action="append",
        choices=tuple(ENGINE_FEATURE_PREFIXES),
        default=[],
        help=(
            "Enable one optional feature engine. Repeat to compose ablation layers; "
            "default is the legacy baseline with all optional engines excluded."
        ),
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    return parser


def _screen_training_features(
    x: np.ndarray,
    feature_names: list[str],
    train_mask: np.ndarray,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """Drop only features that cannot be learned safely from E2023 itself."""
    train_x = x[train_mask]
    minimum_finite = max(3, math.ceil(len(train_x) * MIN_TRAIN_FEATURE_COVERAGE))
    keep = np.ones(len(feature_names), dtype=bool)
    dropped: list[dict[str, Any]] = []

    for position, feature in enumerate(feature_names):
        values = train_x[:, position]
        finite = values[np.isfinite(values)]
        reason: str | None = None
        if len(finite) < minimum_finite:
            reason = "insufficient_e2023_coverage"
        elif float(np.max(finite) - np.min(finite)) <= 1e-12:
            reason = "constant_in_e2023"

        if reason is not None:
            keep[position] = False
            dropped.append(
                {
                    "feature": feature,
                    "reason": reason,
                    "finite_rows": len(finite),
                    "train_rows": len(train_x),
                }
            )

    return keep, dropped


def _validation_fold_positions(
    timestamps: list[Any],
    validation_mask: np.ndarray,
    *,
    folds: int,
) -> list[np.ndarray]:
    validation_indices = np.flatnonzero(validation_mask)
    positions = list(range(len(validation_indices)))
    positions.sort(
        key=lambda position: (
            str(timestamps[int(validation_indices[position])]),
            int(validation_indices[position]),
        )
    )
    return [
        np.asarray(chunk, dtype=int)
        for chunk in np.array_split(np.asarray(positions, dtype=int), folds)
        if len(chunk)
    ]


def _fit_regime_gate(
    x: np.ndarray,
    feature_names: list[str],
    residual_target: np.ndarray,
    train_mask: np.ndarray,
    validation_mask: np.ndarray,
) -> dict[str, Any]:
    """Learn on E2023 when the historical points baseline is likely stale."""
    positions = [
        feature_names.index(name) for name in REGIME_FEATURE_CANDIDATES if name in feature_names
    ]
    names = [feature_names[position] for position in positions]
    if not positions:
        return {
            "model": None,
            "features": [],
            "validation_score": np.ones(int(np.sum(validation_mask)), dtype=float),
            "train_q25_abs_residual": None,
            "train_q75_abs_residual": None,
        }

    gate_model = build_model("ridge", {"alpha": 10.0})
    gate_target = np.abs(residual_target)
    gate_model.fit(x[train_mask][:, positions], gate_target[train_mask])
    raw_validation = np.asarray(
        gate_model.predict(x[validation_mask][:, positions]),
        dtype=float,
    )
    q25, q75 = np.quantile(gate_target[train_mask], [0.25, 0.75])
    width = max(float(q75 - q25), 1e-6)
    score = np.clip((raw_validation - q25) / width, 0.0, 1.0)
    return {
        "model": gate_model,
        "features": names,
        "validation_score": score,
        "train_q25_abs_residual": float(q25),
        "train_q75_abs_residual": float(q75),
        "validation_score_mean": float(np.mean(score)),
        "validation_high_regime_share": float(np.mean(score >= 0.75)),
    }


def _fit_matchup_adjustment(
    x: np.ndarray,
    feature_names: list[str],
    residual_target: np.ndarray,
    train_mask: np.ndarray,
    validation_mask: np.ndarray,
) -> dict[str, Any]:
    """Learn a separate opponent/style residual adjustment using E2023 only."""
    positions = [
        feature_names.index(name) for name in MATCHUP_FEATURE_CANDIDATES if name in feature_names
    ]
    names = [feature_names[position] for position in positions]
    if not positions:
        return {
            "model": None,
            "features": [],
            "validation_delta": np.zeros(int(np.sum(validation_mask)), dtype=float),
        }

    matchup_model = build_model("ridge", {"alpha": 10.0})
    matchup_model.fit(x[train_mask][:, positions], residual_target[train_mask])
    validation_delta = np.asarray(
        matchup_model.predict(x[validation_mask][:, positions]),
        dtype=float,
    )
    return {
        "model": matchup_model,
        "features": names,
        "validation_delta": validation_delta,
        "validation_delta_mean": float(np.mean(validation_delta)),
        "validation_delta_abs_mean": float(np.mean(np.abs(validation_delta))),
    }


def _benchmark_point_residual_stable(
    x: np.ndarray,
    residual_target: np.ndarray,
    actual_points: np.ndarray,
    naive_points: np.ndarray,
    train_mask: np.ndarray,
    validation_mask: np.ndarray,
    timestamps: list[Any],
    regime_score: np.ndarray,
    matchup_delta: np.ndarray,
) -> dict[str, Any]:
    """Select a stable residual model with learned regime and matchup adjustments."""
    validation_actual = actual_points[validation_mask]
    validation_naive = naive_points[validation_mask]
    naive_metrics = _metric_summary(validation_actual, validation_naive)
    fold_positions = _validation_fold_positions(
        timestamps,
        validation_mask,
        folds=POINT_STABILITY_FOLDS,
    )

    candidate_results: list[dict[str, Any]] = []
    best_by_family: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
    selected: dict[str, Any] | None = None
    selected_model: Any | None = None
    selected_raw_delta: np.ndarray | None = None
    selected_delta: np.ndarray | None = None
    selected_gate: np.ndarray | None = None
    selected_key: tuple[Any, ...] | None = None

    for spec in candidate_specs():
        _set_progress(f"points_candidate:{spec.candidate_id}:{spec.family}:fit")
        model = build_model(spec.family, spec.params)
        model.fit(x[train_mask], residual_target[train_mask])
        raw_delta = np.asarray(model.predict(x[validation_mask]), dtype=float)
        _set_progress(f"points_candidate:{spec.candidate_id}:{spec.family}:grid")

        for shrinkage in POINT_RESIDUAL_SHRINKAGE_GRID:
            for gate_floor in REGIME_GATE_FLOOR_GRID:
                gate = gate_floor + (1.0 - gate_floor) * regime_score
                for matchup_shrinkage in MATCHUP_SHRINKAGE_GRID:
                    delta = raw_delta * shrinkage * gate + matchup_delta * matchup_shrinkage
                    prediction = validation_naive + delta
                    metrics = _metric_summary(validation_actual, prediction)
                    fold_metrics: list[dict[str, Any]] = []
                    improvements: list[float] = []

                    for fold_number, positions in enumerate(fold_positions, start=1):
                        model_mae = float(
                            np.mean(np.abs(validation_actual[positions] - prediction[positions]))
                        )
                        naive_mae = float(
                            np.mean(
                                np.abs(validation_actual[positions] - validation_naive[positions])
                            )
                        )
                        improvement = naive_mae - model_mae
                        improvements.append(improvement)
                        fold_metrics.append(
                            {
                                "fold": fold_number,
                                "rows": len(positions),
                                "model_mae": model_mae,
                                "naive_mae": naive_mae,
                                "mae_improvement_vs_naive": improvement,
                            }
                        )

                    positive_folds = sum(value > 0.0 for value in improvements)
                    median_improvement = float(np.median(improvements))
                    worst_improvement = float(min(improvements))
                    overall_improvement = float(naive_metrics["mae"] - metrics["mae"])
                    stability_gate_passed = (
                        overall_improvement > 0.0
                        and positive_folds >= 2
                        and median_improvement > 0.0
                    )
                    result = {
                        "candidate_id": spec.candidate_id,
                        "family": spec.family,
                        "params": spec.params,
                        "residual_shrinkage": shrinkage,
                        "regime_gate_floor": gate_floor,
                        "matchup_shrinkage": matchup_shrinkage,
                        "validation": metrics,
                        "overall_mae_improvement_vs_naive": overall_improvement,
                        "positive_folds": positive_folds,
                        "median_fold_mae_improvement_vs_naive": median_improvement,
                        "worst_fold_mae_improvement_vs_naive": worst_improvement,
                        "temporal_folds": fold_metrics,
                        "stability_gate_passed": stability_gate_passed,
                    }
                    candidate_results.append(result)

                    rank_key = (
                        int(stability_gate_passed),
                        positive_folds,
                        median_improvement,
                        overall_improvement,
                        -metrics["mae"],
                        -abs(shrinkage - 0.5),
                        -matchup_shrinkage,
                        gate_floor,
                    )
                    family_best = best_by_family.get(spec.family)
                    if family_best is None or rank_key > family_best[0]:
                        best_by_family[spec.family] = (rank_key, result)

                    if selected_key is None or rank_key > selected_key:
                        selected_key = rank_key
                        selected = result
                        selected_model = model
                        selected_raw_delta = raw_delta.copy()
                        selected_delta = delta.copy()
                        selected_gate = gate.copy()

    assert selected is not None
    assert selected_model is not None
    assert selected_raw_delta is not None
    assert selected_delta is not None
    assert selected_gate is not None

    leaderboard = [
        {
            "family": family,
            "candidate_id": result["candidate_id"],
            "params": result["params"],
            "residual_shrinkage": result["residual_shrinkage"],
            "regime_gate_floor": result["regime_gate_floor"],
            "matchup_shrinkage": result["matchup_shrinkage"],
            "validation": result["validation"],
            "overall_mae_improvement_vs_naive": result["overall_mae_improvement_vs_naive"],
            "positive_folds": result["positive_folds"],
            "median_fold_mae_improvement_vs_naive": result["median_fold_mae_improvement_vs_naive"],
            "worst_fold_mae_improvement_vs_naive": result["worst_fold_mae_improvement_vs_naive"],
            "stability_gate_passed": result["stability_gate_passed"],
        }
        for family, (_, result) in best_by_family.items()
    ]
    leaderboard.sort(
        key=lambda item: (
            int(item["stability_gate_passed"]),
            item["positive_folds"],
            item["median_fold_mae_improvement_vs_naive"],
            item["overall_mae_improvement_vs_naive"],
            -item["validation"]["mae"],
        ),
        reverse=True,
    )

    return {
        "candidate_results": candidate_results,
        "validation_leaderboard": leaderboard,
        "selected": selected,
        "identity": runtime_model_identity(selected["family"]),
        "model": selected_model,
        "raw_validation_prediction": selected_raw_delta,
        "validation_prediction": selected_delta,
        "selected_regime_gate": selected_gate,
        "selection_policy": {
            "name": "regime_matchup_temporal_stability_v3",
            "folds": POINT_STABILITY_FOLDS,
            "residual_shrinkage_grid": list(POINT_RESIDUAL_SHRINKAGE_GRID),
            "regime_gate_floor_grid": list(REGIME_GATE_FLOOR_GRID),
            "matchup_shrinkage_grid": list(MATCHUP_SHRINKAGE_GRID),
            "gate": (
                "overall MAE beats naive, at least 2/3 E2024 chronological folds "
                "beat naive, and median fold improvement is positive"
            ),
        },
    }


def _training_signal_oof_history(
    *,
    x: np.ndarray,
    y: np.ndarray,
    naive: np.ndarray,
    residual_target: np.ndarray,
    feature_names: list[str],
    train_mask: np.ndarray,
    timestamps: list[Any],
    selected: dict[str, Any],
) -> dict[str, Any]:
    """Build chronological E2023 out-of-fold signal history for pattern calibration."""
    train_indices = np.flatnonzero(train_mask)
    ordered = sorted(
        (int(index) for index in train_indices),
        key=lambda index: (str(timestamps[index]), index),
    )
    chunks = [
        np.asarray(chunk, dtype=int)
        for chunk in np.array_split(np.asarray(ordered, dtype=int), MODEL10_PATTERN_OOF_BLOCKS)
        if len(chunk)
    ]
    if len(chunks) < 2:
        return {
            "indices": np.asarray([], dtype=int),
            "actual": np.asarray([], dtype=float),
            "naive": np.asarray([], dtype=float),
            "predicted": np.asarray([], dtype=float),
            "fingerprints": [],
            "folds": [],
        }

    all_indices: list[int] = []
    all_actual: list[float] = []
    all_naive: list[float] = []
    all_predicted: list[float] = []
    all_fingerprints: list[dict[str, Any]] = []
    fold_rows: list[dict[str, Any]] = []

    for fold_number in range(1, len(chunks)):
        history_indices = np.concatenate(chunks[:fold_number])
        evaluation_indices = chunks[fold_number]
        history_mask = np.zeros(len(x), dtype=bool)
        evaluation_mask = np.zeros(len(x), dtype=bool)
        history_mask[history_indices] = True
        evaluation_mask[evaluation_indices] = True

        model = build_model(selected["family"], selected["params"])
        model.fit(x[history_mask], residual_target[history_mask])
        raw_delta = np.asarray(model.predict(x[evaluation_mask]), dtype=float)

        gate_result = _fit_regime_gate(
            x,
            feature_names,
            residual_target,
            history_mask,
            evaluation_mask,
        )
        gate_score = np.asarray(gate_result["validation_score"], dtype=float)
        gate = selected["regime_gate_floor"] + (
            1.0 - selected["regime_gate_floor"]
        ) * gate_score

        matchup_result = _fit_matchup_adjustment(
            x,
            feature_names,
            residual_target,
            history_mask,
            evaluation_mask,
        )
        matchup_delta = np.asarray(matchup_result["validation_delta"], dtype=float)
        delta = (
            raw_delta * selected["residual_shrinkage"] * gate
            + matchup_delta * selected["matchup_shrinkage"]
        )
        prediction = naive[evaluation_mask] + delta

        contributions = ablation_signal_contributions(
            model=model,
            reference_x=x[history_mask],
            evaluation_x=x[evaluation_mask],
            feature_names=feature_names,
            residual_scale=selected["residual_shrinkage"],
            row_gate=gate,
        )
        matchup_signal = matchup_delta * selected["matchup_shrinkage"]
        if "matchup_opponent" in contributions:
            contributions["matchup_opponent"] = (
                contributions["matchup_opponent"] + matchup_signal
            )
        else:
            contributions["matchup_opponent"] = matchup_signal

        fingerprints = build_signal_fingerprints(
            contributions,
            delta,
            contribution_floor_points=0.25,
        )

        all_indices.extend(int(index) for index in evaluation_indices)
        all_actual.extend(float(value) for value in y[evaluation_mask])
        all_naive.extend(float(value) for value in naive[evaluation_mask])
        all_predicted.extend(float(value) for value in prediction)
        all_fingerprints.extend(fingerprints)
        fold_rows.append(
            {
                "fold": fold_number,
                "history_rows": len(history_indices),
                "evaluation_rows": len(evaluation_indices),
            }
        )

    return {
        "indices": np.asarray(all_indices, dtype=int),
        "actual": np.asarray(all_actual, dtype=float),
        "naive": np.asarray(all_naive, dtype=float),
        "predicted": np.asarray(all_predicted, dtype=float),
        "fingerprints": all_fingerprints,
        "folds": fold_rows,
    }


def _benchmark_validation_target(
    x: np.ndarray,
    y: np.ndarray,
    train_mask: np.ndarray,
    validation_mask: np.ndarray,
    *,
    target_name: str,
) -> dict[str, Any]:
    candidate_results: list[dict[str, Any]] = []
    best_by_family: dict[str, dict[str, Any]] = {}
    selected: dict[str, Any] | None = None
    selected_model: Any | None = None
    selected_prediction: np.ndarray | None = None
    selected_mae = math.inf

    for spec in candidate_specs():
        _set_progress(f"{target_name}_candidate:{spec.candidate_id}:{spec.family}:fit")
        model = build_model(spec.family, spec.params)
        model.fit(x[train_mask], y[train_mask])
        prediction = np.asarray(model.predict(x[validation_mask]), dtype=float)
        metrics = _metric_summary(y[validation_mask], prediction)
        result = {
            "candidate_id": spec.candidate_id,
            "family": spec.family,
            "params": spec.params,
            "validation": metrics,
        }
        candidate_results.append(result)

        family_best = best_by_family.get(spec.family)
        if family_best is None or metrics["mae"] < family_best["validation"]["mae"]:
            best_by_family[spec.family] = result

        if metrics["mae"] < selected_mae:
            selected_mae = metrics["mae"]
            selected = result
            selected_model = model
            selected_prediction = prediction.copy()

    assert selected is not None
    assert selected_model is not None
    assert selected_prediction is not None

    leaderboard = sorted(
        (
            {
                "family": family,
                "candidate_id": result["candidate_id"],
                "params": result["params"],
                "validation": result["validation"],
            }
            for family, result in best_by_family.items()
        ),
        key=lambda item: item["validation"]["mae"],
    )
    return {
        "candidate_results": candidate_results,
        "validation_leaderboard": leaderboard,
        "selected": selected,
        "identity": runtime_model_identity(selected["family"]),
        "model": selected_model,
        "validation_prediction": selected_prediction,
    }


def _rank_importance(
    model: Any,
    x: np.ndarray,
    target: np.ndarray,
    feature_names: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if len(target) > 2500:
        rng = np.random.default_rng(42)
        indices = np.sort(rng.choice(len(target), size=2500, replace=False))
        importance_x = x[indices]
        importance_y = target[indices]
    else:
        importance_x = x
        importance_y = target

    raw = permutation_importance(
        model,
        importance_x,
        importance_y,
        scoring="neg_mean_absolute_error",
        n_repeats=5,
        random_state=42,
        n_jobs=1,
    )
    provenance = provenance_manifest(feature_names)
    provenance_by_feature = {item["feature"]: item for item in provenance}
    ranked = sorted(
        (
            {
                "feature": feature,
                "source_family": provenance_by_feature[feature]["source_family"],
                "source_surface": provenance_by_feature[feature]["source_surface"],
                "importance_mean": float(mean),
                "importance_std": float(std),
            }
            for feature, mean, std in zip(
                feature_names,
                raw.importances_mean,
                raw.importances_std,
                strict=True,
            )
        ),
        key=lambda item: item["importance_mean"],
        reverse=True,
    )

    family_totals: dict[str, dict[str, Any]] = {}
    for item in ranked:
        family = item["source_family"]
        bucket = family_totals.setdefault(
            family,
            {
                "source_family": family,
                "feature_count": 0,
                "importance_sum": 0.0,
                "positive_importance_sum": 0.0,
                "top_features": [],
            },
        )
        bucket["feature_count"] += 1
        bucket["importance_sum"] += item["importance_mean"]
        bucket["positive_importance_sum"] += max(item["importance_mean"], 0.0)
        if len(bucket["top_features"]) < 5:
            bucket["top_features"].append(item["feature"])

    source_family_importance = sorted(
        family_totals.values(),
        key=lambda item: item["positive_importance_sum"],
        reverse=True,
    )
    grouped = _group_permutation_importance(
        model,
        importance_x,
        importance_y,
        feature_names,
        provenance,
        repeats=5,
    )
    return ranked, source_family_importance, grouped


def _write_validation_predictions(
    path: Path,
    rows: list[tuple[Any, ...]],
    index: dict[str, int],
    validation_mask: np.ndarray,
    actual: np.ndarray,
    naive: np.ndarray,
    predicted: np.ndarray,
    calibrated_predicted: np.ndarray,
    signal_fingerprints: list[dict[str, Any]],
    learned_pattern_effects: dict[str, dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "season_code",
        "gamecode",
        "game_date",
        "player_id",
        "player_name",
        "team_code",
        "opponent_team_code",
    )
    validation_rows = [row for row, selected in zip(rows, validation_mask, strict=True) if selected]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                *fields,
                "actual_points",
                "naive_points",
                "predicted_points",
                "calibrated_predicted_points",
                "pattern_calibration_correction_points",
                "prediction_error",
                "absolute_error",
                "signal_tier",
                "supporting_signal_count",
                "situation_fingerprint",
                "supporting_domains",
                "opposing_domains",
            ],
        )
        writer.writeheader()
        for row, y_true, baseline, y_pred, calibrated_y_pred, signal_row in zip(
            validation_rows,
            actual,
            naive,
            predicted,
            calibrated_predicted,
            signal_fingerprints,
            strict=True,
        ):
            learned = learned_pattern_effects.get(str(signal_row["fingerprint"]))
            correction = (
                float(learned["calibration_correction_points"])
                if learned and bool(learned.get("stable_direction"))
                else 0.0
            )
            writer.writerow(
                {
                    **{field: row[index[field]] for field in fields},
                    "actual_points": round(float(y_true), 4),
                    "naive_points": round(float(baseline), 4),
                    "predicted_points": round(float(y_pred), 4),
                    "calibrated_predicted_points": round(float(calibrated_y_pred), 4),
                    "pattern_calibration_correction_points": round(correction, 4),
                    "prediction_error": round(float(y_pred - y_true), 4),
                    "absolute_error": round(float(abs(y_pred - y_true)), 4),
                    "signal_tier": signal_row["signal_tier"],
                    "supporting_signal_count": signal_row["supporting_signal_count"],
                    "situation_fingerprint": signal_row["fingerprint"],
                    "supporting_domains": "|".join(signal_row["supporting_domains"]),
                    "opposing_domains": "|".join(signal_row["opposing_domains"]),
                }
            )


def main(argv: list[str] | None = None) -> int:
    watchdog_stop = _start_progress_watchdog()
    _set_progress("argument_validation")
    args = _parser().parse_args(argv)
    if len({args.train_season, args.validation_season, args.blind_season}) != 3:
        raise ValueError("train, validation and blind seasons must be different.")

    # Critical blind firewall: E2025 is named in metadata only and is NOT queried here.
    context_seasons = tuple(dict.fromkeys(args.context_season))
    protected_seasons = {args.train_season, args.validation_season, args.blind_season}
    overlap = sorted(set(context_seasons) & protected_seasons)
    if overlap:
        raise ValueError(
            f"context seasons must differ from train/validation/blind seasons: {overlap}"
        )

    seasons = [*context_seasons, args.train_season, args.validation_season]
    _set_progress("dataset_full")
    columns, rows = _fetch_dataset(seasons, args.minutes_basis, args.min_history_games)
    if not rows:
        raise RuntimeError("Validation training query returned no rows.")

    _set_progress("dataset_train_prefix")
    train_columns, train_rows = _fetch_dataset(
        [*context_seasons, args.train_season],
        args.minutes_basis,
        args.min_history_games,
    )
    _set_progress("leakage_audits")
    cutoff_audit = assert_feature_cutoffs_before_tipoff(columns, rows)
    prefix_audit = assert_prefix_invariance(train_columns, train_rows, columns, rows)
    leakage_audit = {
        "feature_cutoff": cutoff_audit,
        "train_prefix_invariance": prefix_audit,
        "queried_seasons": seasons,
        "context_seasons": list(context_seasons),
        "blind_season_queried": False,
    }

    _set_progress("feature_matrix")
    feature_names = model_feature_columns(columns)
    enabled_engines = tuple(dict.fromkeys(args.enabled_engine))
    all_engine_prefixes = tuple(ENGINE_FEATURE_PREFIXES.values())
    enabled_engine_prefixes = tuple(ENGINE_FEATURE_PREFIXES[name] for name in enabled_engines)
    feature_names = [
        feature
        for feature in feature_names
        if not feature.startswith(all_engine_prefixes)
        or feature.startswith(enabled_engine_prefixes)
    ]
    if not feature_names:
        raise RuntimeError("No legal pre-game feature columns were produced.")
    index = {name: position for position, name in enumerate(columns)}
    feature_indices = [index[name] for name in feature_names]

    x = np.asarray(
        [[_as_float(row[position]) for position in feature_indices] for row in rows],
        dtype=float,
    )
    y = np.asarray([float(row[index["target_points"]]) for row in rows], dtype=float)
    naive = np.asarray(
        [float(row[index["pre_naive_points_mean"]]) for row in rows],
        dtype=float,
    )
    if not np.all(np.isfinite(naive)):
        raise RuntimeError("Naive baseline contains non-finite values.")

    seasons_array = np.asarray([str(row[index["season_code"]]) for row in rows], dtype=object)
    train_mask = _season_mask(seasons_array, {args.train_season})
    validation_mask = _season_mask(seasons_array, {args.validation_season})
    if not np.any(train_mask) or not np.any(validation_mask):
        raise RuntimeError("Training and validation splits must both contain rows.")

    # Model 2.0 screens features using E2023 only. E2024 is never consulted
    # to decide whether a feature exists or varies, which avoids validation leakage.
    train_feature_mask, dropped_feature_details = _screen_training_features(
        x,
        feature_names,
        train_mask,
    )
    dropped_untrainable_features = [item["feature"] for item in dropped_feature_details]
    if dropped_untrainable_features:
        x = x[:, train_feature_mask]
        feature_names = [
            feature for feature, keep in zip(feature_names, train_feature_mask, strict=True) if keep
        ]

    point_delta = y - naive
    timestamps = [row[index["game_tipoff_utc"]] for row in rows]
    _set_progress("regime_gate")
    regime_gate = _fit_regime_gate(
        x,
        feature_names,
        point_delta,
        train_mask,
        validation_mask,
    )
    _set_progress("matchup_adjustment")
    matchup_adjustment = _fit_matchup_adjustment(
        x,
        feature_names,
        point_delta,
        train_mask,
        validation_mask,
    )
    _set_progress("points_benchmark")
    point_result = _benchmark_point_residual_stable(
        x,
        point_delta,
        y,
        naive,
        train_mask,
        validation_mask,
        timestamps,
        regime_gate["validation_score"],
        matchup_adjustment["validation_delta"],
    )
    validation_delta = point_result["validation_prediction"]
    validation_naive = naive[validation_mask]
    validation_prediction = validation_naive + validation_delta

    _set_progress("situation_signal_attribution")
    signal_contributions = ablation_signal_contributions(
        model=point_result["model"],
        reference_x=x[train_mask],
        evaluation_x=x[validation_mask],
        feature_names=feature_names,
        residual_scale=point_result["selected"]["residual_shrinkage"],
        row_gate=point_result["selected_regime_gate"],
    )
    matchup_signal = (
        np.asarray(matchup_adjustment["validation_delta"], dtype=float)
        * point_result["selected"]["matchup_shrinkage"]
    )
    if "matchup_opponent" in signal_contributions:
        signal_contributions["matchup_opponent"] = (
            signal_contributions["matchup_opponent"] + matchup_signal
        )
    else:
        signal_contributions["matchup_opponent"] = matchup_signal

    signal_fingerprints = build_signal_fingerprints(
        signal_contributions,
        validation_delta,
        contribution_floor_points=0.25,
    )
    signal_tier_summary = summarize_signal_tiers(
        actual=y[validation_mask],
        naive=validation_naive,
        predicted=validation_prediction,
        fingerprints=signal_fingerprints,
    )
    repeating_signal_patterns = summarize_repeating_patterns(
        actual=y[validation_mask],
        naive=validation_naive,
        predicted=validation_prediction,
        fingerprints=signal_fingerprints,
        min_occurrences=3,
    )
    validation_rows = [row for row, selected in zip(rows, validation_mask, strict=True) if selected]
    pattern_stability = summarize_pattern_stability(
        actual=y[validation_mask],
        naive=validation_naive,
        predicted=validation_prediction,
        fingerprints=signal_fingerprints,
        groups={
            "game": [
                f"{row[index['season_code']]}:{row[index['gamecode']]}"
                for row in validation_rows
            ],
            "player": [str(row[index["player_id"]]) for row in validation_rows],
            "team": [str(row[index["team_code"]]) for row in validation_rows],
            "month": [str(row[index["game_date"]])[:7] for row in validation_rows],
        },
        min_occurrences=MODEL10_PATTERN_MIN_OCCURRENCES,
    )

    _set_progress("model10_training_pattern_oof")
    training_signal_oof = _training_signal_oof_history(
        x=x,
        y=y,
        naive=naive,
        residual_target=point_delta,
        feature_names=feature_names,
        train_mask=train_mask,
        timestamps=timestamps,
        selected=point_result["selected"],
    )
    learned_pattern_effects = learn_pattern_effects(
        actual=training_signal_oof["actual"],
        naive=training_signal_oof["naive"],
        predicted=training_signal_oof["predicted"],
        fingerprints=training_signal_oof["fingerprints"],
        min_occurrences=MODEL10_PATTERN_MIN_OCCURRENCES,
        prior_strength=MODEL10_PATTERN_PRIOR_STRENGTH,
    )
    calibrated_validation_prediction = apply_pattern_effects(
        naive=validation_naive,
        predicted=validation_prediction,
        fingerprints=signal_fingerprints,
        learned_effects=learned_pattern_effects,
    )
    calibrated_validation_metrics = _metric_summary(
        y[validation_mask],
        calibrated_validation_prediction,
    )

    minutes_y = np.asarray([float(row[index["target_minutes"]]) for row in rows], dtype=float)
    fga_y = np.asarray([float(row[index["target_fga"]]) for row in rows], dtype=float)
    three_pa_y = np.asarray([float(row[index["target_3pa"]]) for row in rows], dtype=float)
    fta_y = np.asarray([float(row[index["target_fta"]]) for row in rows], dtype=float)

    fga_per_minute = np.divide(
        fga_y,
        minutes_y,
        out=np.zeros_like(fga_y),
        where=minutes_y > 0,
    )
    three_share = np.divide(
        three_pa_y,
        fga_y,
        out=np.zeros_like(three_pa_y),
        where=fga_y > 0,
    )
    fta_per_minute = np.divide(
        fta_y,
        minutes_y,
        out=np.zeros_like(fta_y),
        where=minutes_y > 0,
    )

    minutes_result = _benchmark_validation_target(
        x,
        minutes_y,
        train_mask,
        validation_mask,
        target_name="minutes",
    )
    fga_result = _benchmark_validation_target(
        x,
        fga_per_minute,
        train_mask,
        validation_mask,
        target_name="fga_rate",
    )
    three_result = _benchmark_validation_target(
        x,
        three_share,
        train_mask,
        validation_mask,
        target_name="three_share",
    )
    fta_result = _benchmark_validation_target(
        x,
        fta_per_minute,
        train_mask,
        validation_mask,
        target_name="fta_rate",
    )

    predicted_minutes = np.clip(minutes_result["validation_prediction"], 0.0, 50.0)
    predicted_fga_rate = np.clip(fga_result["validation_prediction"], 0.0, 1.5)
    predicted_three_share = np.clip(three_result["validation_prediction"], 0.0, 1.0)
    predicted_fta_rate = np.clip(fta_result["validation_prediction"], 0.0, 1.5)
    predicted_fga = predicted_minutes * predicted_fga_rate
    predicted_3pa = predicted_fga * predicted_three_share
    predicted_fta = predicted_minutes * predicted_fta_rate

    two_pct_index = feature_names.index("pre_l10_2p_pct")
    three_pct_index = feature_names.index("pre_l10_3p_pct")
    ft_pct_index = feature_names.index("pre_l10_ft_pct")
    priors = {
        "two_pct": _finite_median(x[train_mask, two_pct_index], 0.53),
        "three_pct": _finite_median(x[train_mask, three_pct_index], 0.35),
        "ft_pct": _finite_median(x[train_mask, ft_pct_index], 0.78),
    }
    validation_two_pct = np.where(
        np.isfinite(x[validation_mask, two_pct_index]),
        x[validation_mask, two_pct_index],
        priors["two_pct"],
    )
    validation_three_pct = np.where(
        np.isfinite(x[validation_mask, three_pct_index]),
        x[validation_mask, three_pct_index],
        priors["three_pct"],
    )
    validation_ft_pct = np.where(
        np.isfinite(x[validation_mask, ft_pct_index]),
        x[validation_mask, ft_pct_index],
        priors["ft_pct"],
    )
    role_base = np.asarray(
        [
            role_base_projection(
                predicted_minutes=float(minutes),
                predicted_fga_per_minute=float(fga_rate),
                predicted_three_share=float(three_rate),
                predicted_fta_per_minute=float(fta_rate),
                two_pct=float(two_pct),
                three_pct=float(three_pct),
                ft_pct=float(ft_pct),
            ).points
            for minutes, fga_rate, three_rate, fta_rate, two_pct, three_pct, ft_pct in zip(
                predicted_minutes,
                predicted_fga_rate,
                predicted_three_share,
                predicted_fta_rate,
                validation_two_pct,
                validation_three_pct,
                validation_ft_pct,
                strict=True,
            )
        ],
        dtype=float,
    )

    _set_progress("feature_importance")
    ranked_importance, source_family_importance, grouped_source_importance = _rank_importance(
        point_result["model"],
        x[validation_mask],
        point_delta[validation_mask],
        feature_names,
    )

    _set_progress("placebo_audit")
    placebo = placebo_target_audit(
        model_factory=build_model,
        family=point_result["selected"]["family"],
        params=point_result["selected"]["params"],
        x=x,
        residual_target=point_delta,
        train_mask=train_mask,
        test_mask=validation_mask,
        real_test_prediction=point_result["raw_validation_prediction"],
    )
    _set_progress("rolling_time_audit")
    rolling = rolling_time_audit(
        model_factory=build_model,
        family=point_result["selected"]["family"],
        params=point_result["selected"]["params"],
        x=x,
        residual_target=point_delta,
        actual_points=y,
        naive_points=naive,
        timestamps=timestamps,
        eligible_mask=train_mask | validation_mask,
    )
    volatility_index = feature_names.index("pre_l10_points_std")
    volatility_values = x[validation_mask, volatility_index]
    finite_volatility = volatility_values[np.isfinite(volatility_values)]
    if len(finite_volatility) >= 3:
        q33, q67 = np.quantile(finite_volatility, [1.0 / 3.0, 2.0 / 3.0])
    else:
        q33, q67 = 3.0, 5.0
    volatility_labels = [
        "unknown"
        if not np.isfinite(value)
        else ("stable" if value <= q33 else ("medium" if value <= q67 else "volatile"))
        for value in volatility_values
    ]
    _set_progress("segment_stability_audit")
    segment = segment_stability_audit(
        actual=y[validation_mask],
        predicted=validation_prediction,
        naive=validation_naive,
        segments={
            "player": [row[index["player_id"]] for row in validation_rows],
            "team": [row[index["team_code"]] for row in validation_rows],
            "month": [str(row[index["game_date"]])[:7] for row in validation_rows],
            "starter_state": [
                "starter" if bool(row[index["target_was_starter"]]) else "bench"
                for row in validation_rows
            ],
            "volatility": volatility_labels,
        },
    )

    # The legacy rolling audit scores the ungated residual model only. The final
    # architecture's temporal gate is the selected 3-fold result above.
    rolling["scope"] = "ungated_residual_diagnostic"
    statuses = [placebo["status"], segment["status"]]
    if not point_result["selected"]["stability_gate_passed"]:
        statuses.append("warn")
    controls_status = (
        "fail"
        if "fail" in statuses
        else ("warn" if any(status in {"warn", "insufficient"} for status in statuses) else "pass")
    )

    validation_metrics = _metric_summary(y[validation_mask], validation_prediction)
    naive_metrics = _metric_summary(y[validation_mask], validation_naive)
    role_base_metrics = _metric_summary(y[validation_mask], role_base)

    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "phase": "validation_signal_discovery",
        "blind_test_opened": False,
        "blind_test_season": args.blind_season,
        "queried_seasons": seasons,
        "context_seasons": list(context_seasons),
        "split": {
            "train": [args.train_season],
            "validation": [args.validation_season],
            "blind_reserved": [args.blind_season],
        },
        "rows": {
            "all": len(rows),
            "train": int(np.sum(train_mask)),
            "validation": int(np.sum(validation_mask)),
        },
        "feature_count": len(feature_names),
        "features": feature_names,
        "enabled_engines": list(enabled_engines),
        "engine_ablation": {
            "available": list(ENGINE_FEATURE_PREFIXES),
            "enabled": list(enabled_engines),
            "policy": (
                "Optional engines are excluded by default and enabled cumulatively "
                "so each layer can be measured against the previous model."
            ),
        },
        "dropped_untrainable_features": dropped_untrainable_features,
        "dropped_untrainable_feature_count": len(dropped_untrainable_features),
        "dropped_feature_details": dropped_feature_details,
        "training_feature_screen": {
            "minimum_e2023_finite_coverage": MIN_TRAIN_FEATURE_COVERAGE,
            "rule": "drop only sparse or constant features using E2023 training data",
        },
        "feature_provenance": provenance_manifest(feature_names),
        "leakage_audit": leakage_audit,
        "candidate_results": point_result["candidate_results"],
        "validation_leaderboard": point_result["validation_leaderboard"],
        "selected_model": {
            **point_result["identity"],
            "candidate_id": point_result["selected"]["candidate_id"],
            "residual_shrinkage": point_result["selected"]["residual_shrinkage"],
            "regime_gate_floor": point_result["selected"]["regime_gate_floor"],
            "matchup_shrinkage": point_result["selected"]["matchup_shrinkage"],
            "stability_gate_passed": point_result["selected"]["stability_gate_passed"],
        },
        "selected_params": point_result["selected"]["params"],
        "selection_policy": point_result["selection_policy"],
        "regime_gate": {
            "features": regime_gate["features"],
            "train_q25_abs_residual": regime_gate["train_q25_abs_residual"],
            "train_q75_abs_residual": regime_gate["train_q75_abs_residual"],
            "validation_score_mean": regime_gate.get("validation_score_mean"),
            "validation_high_regime_share": regime_gate.get("validation_high_regime_share"),
            "selected_floor": point_result["selected"]["regime_gate_floor"],
        },
        "matchup_adjustment": {
            "features": matchup_adjustment["features"],
            "validation_delta_mean": matchup_adjustment.get("validation_delta_mean"),
            "validation_delta_abs_mean": matchup_adjustment.get("validation_delta_abs_mean"),
            "selected_shrinkage": point_result["selected"]["matchup_shrinkage"],
        },
        "model10_situation_signals": {
            "status": "MODEL10_1_TRAIN_ONLY_PATTERN_CALIBRATION",
            "contribution_floor_points": 0.25,
            "domains": {
                domain: [feature_names[position] for position in positions]
                for domain, positions in signal_domain_columns(feature_names).items()
            },
            "tier_summary": signal_tier_summary,
            "repeating_patterns_min_occurrences": 3,
            "repeating_patterns": repeating_signal_patterns,
            "pattern_stability_min_occurrences": MODEL10_PATTERN_MIN_OCCURRENCES,
            "pattern_stability": pattern_stability,
            "training_only_pattern_calibration": {
                "source_season": args.train_season,
                "method": "chronological_out_of_fold",
                "oof_blocks": MODEL10_PATTERN_OOF_BLOCKS,
                "oof_rows": len(training_signal_oof["indices"]),
                "folds": training_signal_oof["folds"],
                "min_occurrences": MODEL10_PATTERN_MIN_OCCURRENCES,
                "prior_strength": MODEL10_PATTERN_PRIOR_STRENGTH,
                "learned_pattern_count": len(learned_pattern_effects),
                "learned_effects": learned_pattern_effects,
                "validation_metrics_after_calibration": calibrated_validation_metrics,
                "validation_mae_change_vs_uncalibrated": (
                    calibrated_validation_metrics["mae"] - validation_metrics["mae"]
                ),
                "policy": (
                    "Pattern magnitude corrections are learned only from chronological "
                    "out-of-fold rows in the training season. E2024 outcomes never set "
                    "a calibration magnitude."
                ),
            },
            "interpretation": (
                "Signal tiers count independent basketball situation domains whose "
                "ablation contribution supports the model correction. Repeated-pattern "
                "hit rates measure correction direction versus the naive projection; "
                "they are not bookmaker-line betting hit rates."
            ),
        },
        "validation_temporal_stability": {
            "positive_folds": point_result["selected"]["positive_folds"],
            "median_fold_mae_improvement_vs_naive": point_result["selected"][
                "median_fold_mae_improvement_vs_naive"
            ],
            "worst_fold_mae_improvement_vs_naive": point_result["selected"][
                "worst_fold_mae_improvement_vs_naive"
            ],
            "folds": point_result["selected"]["temporal_folds"],
        },
        "validation_points": validation_metrics,
        "naive_points_baseline": naive_metrics,
        "role_base_points": role_base_metrics,
        "auxiliary_targets": {
            "minutes": {
                "validation_leaderboard": minutes_result["validation_leaderboard"],
                "selected_model": {
                    **minutes_result["identity"],
                    "candidate_id": minutes_result["selected"]["candidate_id"],
                },
                "validation": _metric_summary(
                    minutes_y[validation_mask],
                    predicted_minutes,
                ),
            },
            "fga_per_minute": {
                "validation_leaderboard": fga_result["validation_leaderboard"],
                "selected_model": {
                    **fga_result["identity"],
                    "candidate_id": fga_result["selected"]["candidate_id"],
                },
                "validation": _metric_summary(
                    fga_per_minute[validation_mask],
                    predicted_fga_rate,
                ),
            },
            "three_point_share": {
                "validation_leaderboard": three_result["validation_leaderboard"],
                "selected_model": {
                    **three_result["identity"],
                    "candidate_id": three_result["selected"]["candidate_id"],
                },
                "validation": _metric_summary(
                    three_share[validation_mask],
                    predicted_three_share,
                ),
            },
            "fta_per_minute": {
                "validation_leaderboard": fta_result["validation_leaderboard"],
                "selected_model": {
                    **fta_result["identity"],
                    "candidate_id": fta_result["selected"]["candidate_id"],
                },
                "validation": _metric_summary(
                    fta_per_minute[validation_mask],
                    predicted_fta_rate,
                ),
            },
            "derived_attempts": {
                "fga": _metric_summary(fga_y[validation_mask], predicted_fga),
                "3pa": _metric_summary(three_pa_y[validation_mask], predicted_3pa),
                "fta": _metric_summary(fta_y[validation_mask], predicted_fta),
            },
        },
        "feature_importance": ranked_importance,
        "source_family_importance": source_family_importance,
        "grouped_source_permutation_importance": grouped_source_importance,
        "validation_controls": {
            "status": controls_status,
            "placebo_target_audit": placebo,
            "rolling_time_audit": rolling,
            "segment_stability_audit": segment,
        },
        "blind_policy": {
            "status": "BLOCKED_PENDING_SIGNAL_REVIEW",
            "rule": (
                "Do not query or score E2025 until the validation signals, stability, "
                "and candidate choice are reviewed and explicitly locked."
            ),
        },
    }

    _set_progress("write_artifacts")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.model.parent.mkdir(parents=True, exist_ok=True)
    with args.model.open("wb") as handle:
        pickle.dump(
            {
                "phase": "validation_signal_discovery",
                "blind_test_opened": False,
                "model": point_result["model"],
                "model_family": point_result["selected"]["family"],
                "model_identity": point_result["identity"],
                "selected_params": point_result["selected"]["params"],
                "residual_shrinkage": point_result["selected"]["residual_shrinkage"],
                "regime_gate_floor": point_result["selected"]["regime_gate_floor"],
                "matchup_shrinkage": point_result["selected"]["matchup_shrinkage"],
                "pattern_calibration_effects": learned_pattern_effects,
                "pattern_calibration_min_occurrences": MODEL10_PATTERN_MIN_OCCURRENCES,
                "pattern_calibration_prior_strength": MODEL10_PATTERN_PRIOR_STRENGTH,
                "regime_gate_model": regime_gate["model"],
                "regime_gate_features": regime_gate["features"],
                "regime_gate_q25_abs_residual": regime_gate["train_q25_abs_residual"],
                "regime_gate_q75_abs_residual": regime_gate["train_q75_abs_residual"],
                "matchup_model": matchup_adjustment["model"],
                "matchup_features": matchup_adjustment["features"],
                "selection_policy": point_result["selection_policy"],
                "features": feature_names,
                "enabled_engines": list(enabled_engines),
                "trained_seasons": [args.train_season],
                "validation_season": args.validation_season,
                "blind_reserved_season": args.blind_season,
                "git_commit": os.environ.get("GITHUB_SHA"),
            },
            handle,
            protocol=pickle.HIGHEST_PROTOCOL,
        )

    _write_validation_predictions(
        args.predictions,
        rows,
        index,
        validation_mask,
        y[validation_mask],
        validation_naive,
        validation_prediction,
        calibrated_validation_prediction,
        signal_fingerprints,
        learned_pattern_effects,
    )

    print(f"rows={json.dumps(report['rows'], sort_keys=True)}")
    print(
        "dropped_untrainable_features=" + json.dumps(dropped_untrainable_features, sort_keys=True)
    )
    print(f"enabled_engines={json.dumps(list(enabled_engines), sort_keys=True)}")
    print(f"selected_model={json.dumps(report['selected_model'], sort_keys=True)}")
    print(f"regime_gate={json.dumps(report['regime_gate'], sort_keys=True)}")
    print("matchup_adjustment=" + json.dumps(report["matchup_adjustment"], sort_keys=True))
    print(f"validation_points={json.dumps(validation_metrics, sort_keys=True)}")
    print(
        "model10_pattern_calibration="
        + json.dumps(
            {
                "oof_rows": len(training_signal_oof["indices"]),
                "learned_pattern_count": len(learned_pattern_effects),
                "validation_metrics_after_calibration": calibrated_validation_metrics,
            },
            sort_keys=True,
        )
    )
    print("source_family_importance=" + json.dumps(source_family_importance[:10], sort_keys=True))
    print("grouped_source_importance=" + json.dumps(grouped_source_importance[:10], sort_keys=True))
    print(f"validation_controls={json.dumps(report['validation_controls'], sort_keys=True)}")
    print("blind_test_opened=false", flush=True)
    _set_progress("complete")
    watchdog_stop.set()
    return 0 if controls_status != "fail" else 2


if __name__ == "__main__":
    raise SystemExit(main())
