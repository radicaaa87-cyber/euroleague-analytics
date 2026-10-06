"""Tests for the independent player-points model validation controls."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import numpy as np

from euroleague.model_validation import (
    feature_matrix_sha256,
    placebo_target_audit,
    rolling_time_audit,
    segment_stability_audit,
    write_preblind_lock,
)


class LinearModel:
    def __init__(self) -> None:
        self.intercept = 0.0
        self.slope = 0.0

    def fit(self, x: np.ndarray, y: np.ndarray) -> LinearModel:
        design = np.column_stack((np.ones(len(x)), x[:, 0]))
        beta, *_ = np.linalg.lstsq(design, y, rcond=None)
        self.intercept = float(beta[0])
        self.slope = float(beta[1])
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        return self.intercept + self.slope * x[:, 0]


def _linear_factory(family: str, params: dict) -> LinearModel:
    del family, params
    return LinearModel()


def test_feature_matrix_hash_changes_with_one_feature_value() -> None:
    keys = [("E2023", 1, "P1"), ("E2023", 2, "P1")]
    matrix = np.asarray([[1.0, 2.0], [3.0, np.nan]])

    first = feature_matrix_sha256(["a", "b"], keys, matrix)
    changed = matrix.copy()
    changed[1, 0] = 3.001
    second = feature_matrix_sha256(["a", "b"], keys, changed)

    assert len(first) == 64
    assert first != second


def test_preblind_lock_contains_no_blind_target_hash(tmp_path) -> None:
    path = tmp_path / "preblind_lock.json"
    matrix = np.asarray([[1.0], [2.0], [3.0]])
    keys = [
        ("E2023", 1, "P1"),
        ("E2024", 2, "P1"),
        ("E2025", 3, "P1"),
    ]

    manifest = write_preblind_lock(
        path,
        feature_names=["pre_x"],
        all_row_keys=keys,
        matrix=matrix,
        legal_target_row_keys=keys[:2],
        legal_target=np.asarray([0.2, -0.1]),
        blind_row_keys=keys[2:],
        blind_predictions={"predicted_points": np.asarray([12.5])},
        model_identity={"family": "test"},
        selected_params={"depth": 2},
        split={"blind_test": ["E2025"]},
        created_at="2026-10-06T00:00:00+00:00",
        git_commit="abc",
    )

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted == manifest
    assert manifest["blind_targets_opened"] is False
    assert "blind_target_sha256" not in manifest
    assert len(manifest["blind_prediction_sha256"]) == 64


def test_placebo_shuffle_does_not_retain_true_signal() -> None:
    x = np.arange(240, dtype=float).reshape(-1, 1)
    residual = 0.05 * x[:, 0]
    train_mask = np.zeros(240, dtype=bool)
    train_mask[:180] = True
    test_mask = ~train_mask
    real_prediction = residual[test_mask].copy()

    audit = placebo_target_audit(
        model_factory=_linear_factory,
        family="test",
        params={},
        x=x,
        residual_target=residual,
        train_mask=train_mask,
        test_mask=test_mask,
        real_test_prediction=real_prediction,
        seed=42,
    )

    assert audit["status"] in {"pass", "warn"}
    assert audit["real_residual_mae"] < audit["placebo_residual_mae"]
    assert audit["status"] != "fail"


def test_rolling_time_audit_rewards_chronological_signal() -> None:
    rows = 900
    x = np.arange(rows, dtype=float).reshape(-1, 1) / 100.0
    residual = 0.8 * x[:, 0] + 0.2
    naive = np.full(rows, 10.0)
    actual = naive + residual
    start = datetime(2023, 1, 1, tzinfo=UTC)
    timestamps = [start + timedelta(days=index) for index in range(rows)]
    eligible = np.ones(rows, dtype=bool)

    audit = rolling_time_audit(
        model_factory=_linear_factory,
        family="test",
        params={},
        x=x,
        residual_target=residual,
        actual_points=actual,
        naive_points=naive,
        timestamps=timestamps,
        eligible_mask=eligible,
    )

    assert audit["status"] == "pass"
    assert audit["fold_count"] >= 2
    assert audit["median_mae_improvement_vs_naive"] > 0


def test_segment_stability_flags_material_hidden_degradation() -> None:
    actual = np.asarray([10.0] * 120 + [20.0] * 120)
    naive = np.asarray([11.0] * 120 + [19.0] * 120)
    predicted = np.asarray([10.2] * 120 + [22.0] * 120)

    audit = segment_stability_audit(
        actual=actual,
        predicted=predicted,
        naive=naive,
        segments={
            "team": ["GOOD"] * 120 + ["BAD"] * 120,
            "starter_state": ["starter"] * 240,
        },
    )

    assert audit["status"] == "warn"
    assert any(
        row["segment"] == "BAD"
        for row in audit["material_degradation_segments"]
    )
