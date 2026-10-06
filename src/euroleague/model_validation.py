"""Independent validation controls for player-points machine learning.

These helpers do not add predictive features. They audit whether a trained model
behaves like a real chronological pre-game model rather than a leaky or unstable
backtest.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np


def _normalise(value: Any) -> Any:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _hash_rows(rows: Sequence[Sequence[Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        encoded = json.dumps(
            [_normalise(value) for value in row],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(encoded)
        digest.update(b"\n")
    return digest.hexdigest()


def feature_matrix_sha256(
    feature_names: Sequence[str],
    row_keys: Sequence[Sequence[Any]],
    matrix: np.ndarray,
) -> str:
    """Hash row identity, feature schema and exact pre-game numeric matrix."""
    digest = hashlib.sha256()
    digest.update(json.dumps(list(feature_names), separators=(",", ":")).encode("utf-8"))
    digest.update(b"\n")
    if len(row_keys) != len(matrix):
        raise ValueError("row_keys and matrix must have identical lengths.")
    for key, values in zip(row_keys, matrix, strict=True):
        digest.update(
            json.dumps(
                [_normalise(value) for value in key],
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"|")
        digest.update(
            json.dumps(
                [_normalise(float(value)) for value in values],
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def target_sha256(
    row_keys: Sequence[Sequence[Any]],
    values: np.ndarray,
) -> str:
    """Hash only the targets that are legally open before blind scoring."""
    if len(row_keys) != len(values):
        raise ValueError("row_keys and target values must have identical lengths.")
    return _hash_rows(
        [(*key, _normalise(float(value))) for key, value in zip(row_keys, values, strict=True)]
    )


def prediction_sha256(
    row_keys: Sequence[Sequence[Any]],
    predictions: Mapping[str, np.ndarray],
) -> str:
    """Hash a named bundle of blind predictions before blind outcomes are scored."""
    names = sorted(predictions)
    lengths = {len(predictions[name]) for name in names}
    if lengths != {len(row_keys)}:
        raise ValueError("Every prediction vector must match row_keys.")
    digest = hashlib.sha256()
    digest.update(json.dumps(names, separators=(",", ":")).encode("utf-8"))
    digest.update(b"\n")
    for index, key in enumerate(row_keys):
        row = [*key]
        row.extend(_normalise(float(predictions[name][index])) for name in names)
        digest.update(
            json.dumps(
                [_normalise(value) for value in row],
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def write_preblind_lock(
    path: Path,
    *,
    feature_names: Sequence[str],
    all_row_keys: Sequence[Sequence[Any]],
    matrix: np.ndarray,
    legal_target_row_keys: Sequence[Sequence[Any]],
    legal_target: np.ndarray,
    blind_row_keys: Sequence[Sequence[Any]],
    blind_predictions: Mapping[str, np.ndarray],
    model_identity: Mapping[str, Any],
    selected_params: Mapping[str, Any],
    split: Mapping[str, Any],
    created_at: str,
    git_commit: str | None,
) -> dict[str, Any]:
    """Persist an immutable-content manifest before blind outcomes are scored."""
    manifest = {
        "created_at": created_at,
        "git_commit": git_commit,
        "split": dict(split),
        "feature_count": len(feature_names),
        "all_rows": len(all_row_keys),
        "legal_target_rows": len(legal_target_row_keys),
        "blind_rows": len(blind_row_keys),
        "feature_matrix_sha256": feature_matrix_sha256(feature_names, all_row_keys, matrix),
        "legal_train_validation_target_sha256": target_sha256(
            legal_target_row_keys,
            legal_target,
        ),
        "blind_prediction_sha256": prediction_sha256(
            blind_row_keys,
            blind_predictions,
        ),
        "model_identity": dict(model_identity),
        "selected_params": dict(selected_params),
        "blind_prediction_fields": sorted(blind_predictions),
        "blind_targets_opened": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _mae(actual: np.ndarray, predicted: np.ndarray) -> float:
    actual_array = np.asarray(actual, dtype=float)
    predicted_array = np.asarray(predicted, dtype=float)
    return float(np.mean(np.abs(actual_array - predicted_array)))


def placebo_target_audit(
    *,
    model_factory: Callable[[str, dict[str, Any]], Any],
    family: str,
    params: dict[str, Any],
    x: np.ndarray,
    residual_target: np.ndarray,
    train_mask: np.ndarray,
    test_mask: np.ndarray,
    real_test_prediction: np.ndarray,
    seed: int = 42,
) -> dict[str, Any]:
    """Shuffle training targets; a placebo model must not retain real blind signal."""
    rng = np.random.default_rng(seed)
    shuffled = np.asarray(residual_target[train_mask], dtype=float).copy()
    rng.shuffle(shuffled)

    placebo_model = model_factory(family, params)
    placebo_model.fit(x[train_mask], shuffled)
    placebo_prediction = np.asarray(placebo_model.predict(x[test_mask]), dtype=float)
    actual = np.asarray(residual_target[test_mask], dtype=float)
    real_prediction = np.asarray(real_test_prediction, dtype=float)

    placebo_mae = _mae(actual, placebo_prediction)
    real_mae = _mae(actual, real_prediction)
    zero_delta_mae = _mae(actual, np.zeros_like(actual))

    if len(actual) >= 3 and np.std(placebo_prediction) > 0 and np.std(actual) > 0:
        correlation = float(np.corrcoef(placebo_prediction, actual)[0, 1])
    else:
        correlation = 0.0

    suspicious_reasons: list[str] = []
    warnings: list[str] = []
    if placebo_mae < real_mae - 0.25:
        suspicious_reasons.append("shuffled-target placebo materially beat the real model")
    if abs(correlation) >= 0.20 and placebo_mae <= real_mae + 0.25:
        suspicious_reasons.append(
            "placebo kept high blind correlation while matching the real model error"
        )
    if placebo_mae < zero_delta_mae - 0.10:
        warnings.append("placebo slightly beat the zero-residual naive baseline")
    if abs(correlation) >= 0.10:
        warnings.append(
            "placebo blind correlation exceeded 0.10; inspect together with placebo MAE"
        )

    status = "fail" if suspicious_reasons else ("warn" if warnings else "pass")
    return {
        "status": status,
        "seed": seed,
        "rows": len(actual),
        "real_residual_mae": real_mae,
        "placebo_residual_mae": placebo_mae,
        "zero_delta_residual_mae": zero_delta_mae,
        "placebo_blind_correlation": correlation,
        "suspicious_reasons": suspicious_reasons,
        "warnings": warnings,
    }


def rolling_time_audit(
    *,
    model_factory: Callable[[str, dict[str, Any]], Any],
    family: str,
    params: dict[str, Any],
    x: np.ndarray,
    residual_target: np.ndarray,
    actual_points: np.ndarray,
    naive_points: np.ndarray,
    timestamps: Sequence[Any],
    eligible_mask: np.ndarray,
) -> dict[str, Any]:
    """Run expanding-window checks using only pre-blind seasons."""
    eligible = np.flatnonzero(eligible_mask)
    if len(eligible) < 400:
        return {"status": "insufficient", "folds": [], "reason": "fewer than 400 pre-blind rows"}

    ordered = sorted(eligible, key=lambda idx: (timestamps[idx], int(idx)))
    unique_times = sorted({timestamps[idx] for idx in ordered})
    if len(unique_times) < 12:
        return {"status": "insufficient", "folds": [], "reason": "fewer than 12 time points"}

    boundaries = [
        unique_times[int((len(unique_times) - 1) * fraction)]
        for fraction in (0.50, 2.0 / 3.0, 5.0 / 6.0)
    ]
    fold_ends = [boundaries[1], boundaries[2], None]
    folds: list[dict[str, Any]] = []

    for fold_number, (start, end) in enumerate(zip(boundaries, fold_ends, strict=True), start=1):
        train_indices = np.asarray(
            [idx for idx in ordered if timestamps[idx] < start],
            dtype=int,
        )
        if end is None:
            eval_indices = np.asarray(
                [idx for idx in ordered if timestamps[idx] >= start],
                dtype=int,
            )
        else:
            eval_indices = np.asarray(
                [idx for idx in ordered if start <= timestamps[idx] < end],
                dtype=int,
            )
        if len(train_indices) < 250 or len(eval_indices) < 75:
            continue

        model = model_factory(family, params)
        model.fit(x[train_indices], residual_target[train_indices])
        delta_prediction = np.asarray(model.predict(x[eval_indices]), dtype=float)
        point_prediction = naive_points[eval_indices] + delta_prediction

        model_mae = _mae(actual_points[eval_indices], point_prediction)
        naive_mae = _mae(actual_points[eval_indices], naive_points[eval_indices])
        folds.append(
            {
                "fold": fold_number,
                "train_rows": len(train_indices),
                "eval_rows": len(eval_indices),
                "eval_start": _normalise(start),
                "eval_end": _normalise(end),
                "model_mae": model_mae,
                "naive_mae": naive_mae,
                "mae_improvement_vs_naive": naive_mae - model_mae,
            }
        )

    if len(folds) < 2:
        return {"status": "insufficient", "folds": folds, "reason": "fewer than two valid folds"}

    improvements = np.asarray(
        [fold["mae_improvement_vs_naive"] for fold in folds],
        dtype=float,
    )
    positive = int(np.sum(improvements > 0))
    median = float(np.median(improvements))
    if median < -0.25:
        status = "fail"
    elif median <= 0 or positive < math.ceil(len(folds) / 2):
        status = "warn"
    else:
        status = "pass"
    return {
        "status": status,
        "folds": folds,
        "positive_folds": positive,
        "fold_count": len(folds),
        "median_mae_improvement_vs_naive": median,
    }


def _segment_rows(
    *,
    labels: Sequence[Any],
    actual: np.ndarray,
    predicted: np.ndarray,
    naive: np.ndarray,
    min_rows: int,
) -> list[dict[str, Any]]:
    groups: dict[str, list[int]] = {}
    for idx, value in enumerate(labels):
        groups.setdefault(str(value), []).append(idx)

    output: list[dict[str, Any]] = []
    for label, indices in groups.items():
        if len(indices) < min_rows:
            continue
        positions = np.asarray(indices, dtype=int)
        model_mae = _mae(actual[positions], predicted[positions])
        naive_mae = _mae(actual[positions], naive[positions])
        output.append(
            {
                "segment": label,
                "rows": len(indices),
                "model_mae": model_mae,
                "naive_mae": naive_mae,
                "mae_improvement_vs_naive": naive_mae - model_mae,
                "mean_error": float(np.mean(predicted[positions] - actual[positions])),
            }
        )
    return sorted(output, key=lambda item: item["mae_improvement_vs_naive"])


def segment_stability_audit(
    *,
    actual: np.ndarray,
    predicted: np.ndarray,
    naive: np.ndarray,
    segments: Mapping[str, Sequence[Any]],
) -> dict[str, Any]:
    """Expose where an apparently good global score hides weak subgroups."""
    minimums = {
        "player": 8,
        "team": 30,
        "month": 30,
        "starter_state": 40,
        "volatility": 30,
    }
    by_dimension: dict[str, list[dict[str, Any]]] = {}
    warnings: list[dict[str, Any]] = []

    for dimension, labels in segments.items():
        rows = _segment_rows(
            labels=labels,
            actual=actual,
            predicted=predicted,
            naive=naive,
            min_rows=minimums.get(dimension, 30),
        )
        by_dimension[dimension] = rows
        if dimension != "player":
            warnings.extend(
                {"dimension": dimension, **item}
                for item in rows
                if item["mae_improvement_vs_naive"] < -0.75
            )

    status = "warn" if warnings else "pass"
    return {
        "status": status,
        "global_model_mae": _mae(actual, predicted),
        "global_naive_mae": _mae(actual, naive),
        "dimensions": by_dimension,
        "material_degradation_segments": sorted(
            warnings,
            key=lambda item: item["mae_improvement_vs_naive"],
        )[:20],
    }
