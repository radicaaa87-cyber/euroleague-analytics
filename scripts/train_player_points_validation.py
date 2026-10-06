"""Validation-only model discovery for EuroLeague player points.

This phase intentionally never queries the blind E2025 season. It trains on E2023,
selects model families/configurations on E2024, and reports which pre-game signal
families carry validation value. Only after review should a separate blind run be
allowed to open E2025 outcomes.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import pickle
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.inspection import permutation_importance

from euroleague.feature_provenance import provenance_manifest
from euroleague.leakage import assert_feature_cutoffs_before_tipoff, assert_prefix_invariance
from euroleague.ml_benchmark import build_model, candidate_specs, runtime_model_identity
from euroleague.model_training import model_feature_columns
from euroleague.model_validation import (
    placebo_target_audit,
    rolling_time_audit,
    segment_stability_audit,
)
from euroleague.role_projection import role_base_projection
from train_player_points_ml import (
    _as_float,
    _fetch_dataset,
    _finite_median,
    _group_permutation_importance,
    _metric_summary,
    _season_mask,
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
        "--minutes-basis",
        default="official",
        choices=("official", "corrected", "raw"),
    )
    parser.add_argument("--min-history-games", type=int, default=3)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    return parser


def _benchmark_validation_target(
    x: np.ndarray,
    y: np.ndarray,
    train_mask: np.ndarray,
    validation_mask: np.ndarray,
) -> dict[str, Any]:
    candidate_results: list[dict[str, Any]] = []
    best_by_family: dict[str, dict[str, Any]] = {}
    selected: dict[str, Any] | None = None
    selected_model: Any | None = None
    selected_prediction: np.ndarray | None = None
    selected_mae = math.inf

    for spec in candidate_specs():
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
    validation_rows = [
        row for row, selected in zip(rows, validation_mask, strict=True) if selected
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                *fields,
                "actual_points",
                "naive_points",
                "predicted_points",
                "prediction_error",
                "absolute_error",
            ],
        )
        writer.writeheader()
        for row, y_true, baseline, y_pred in zip(
            validation_rows,
            actual,
            naive,
            predicted,
            strict=True,
        ):
            writer.writerow(
                {
                    **{field: row[index[field]] for field in fields},
                    "actual_points": round(float(y_true), 4),
                    "naive_points": round(float(baseline), 4),
                    "predicted_points": round(float(y_pred), 4),
                    "prediction_error": round(float(y_pred - y_true), 4),
                    "absolute_error": round(float(abs(y_pred - y_true)), 4),
                }
            )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if len({args.train_season, args.validation_season, args.blind_season}) != 3:
        raise ValueError("train, validation and blind seasons must be different.")

    # Critical blind firewall: E2025 is named in metadata only and is NOT queried here.
    seasons = [args.train_season, args.validation_season]
    columns, rows = _fetch_dataset(seasons, args.minutes_basis, args.min_history_games)
    if not rows:
        raise RuntimeError("Validation training query returned no rows.")

    train_columns, train_rows = _fetch_dataset(
        [args.train_season],
        args.minutes_basis,
        args.min_history_games,
    )
    cutoff_audit = assert_feature_cutoffs_before_tipoff(columns, rows)
    prefix_audit = assert_prefix_invariance(train_columns, train_rows, columns, rows)
    leakage_audit = {
        "feature_cutoff": cutoff_audit,
        "train_prefix_invariance": prefix_audit,
        "queried_seasons": seasons,
        "blind_season_queried": False,
    }

    feature_names = model_feature_columns(columns)
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

    # A feature with zero observed values in E2023 cannot be learned without
    # looking forward into validation. Drop only those columns, using the
    # training season alone, and report them as coverage gaps rather than
    # treating them as unimportant signals.
    train_has_value = np.any(np.isfinite(x[train_mask]), axis=0)
    dropped_untrainable_features = [
        feature for feature, keep in zip(feature_names, train_has_value, strict=True) if not keep
    ]
    if dropped_untrainable_features:
        x = x[:, train_has_value]
        feature_names = [
            feature for feature, keep in zip(feature_names, train_has_value, strict=True) if keep
        ]

    point_delta = y - naive
    point_result = _benchmark_validation_target(x, point_delta, train_mask, validation_mask)
    validation_delta = point_result["validation_prediction"]
    validation_naive = naive[validation_mask]
    validation_prediction = validation_naive + validation_delta

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

    minutes_result = _benchmark_validation_target(x, minutes_y, train_mask, validation_mask)
    fga_result = _benchmark_validation_target(x, fga_per_minute, train_mask, validation_mask)
    three_result = _benchmark_validation_target(x, three_share, train_mask, validation_mask)
    fta_result = _benchmark_validation_target(x, fta_per_minute, train_mask, validation_mask)

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

    ranked_importance, source_family_importance, grouped_source_importance = _rank_importance(
        point_result["model"],
        x[validation_mask],
        point_delta[validation_mask],
        feature_names,
    )

    placebo = placebo_target_audit(
        model_factory=build_model,
        family=point_result["selected"]["family"],
        params=point_result["selected"]["params"],
        x=x,
        residual_target=point_delta,
        train_mask=train_mask,
        test_mask=validation_mask,
        real_test_prediction=validation_delta,
    )
    rolling = rolling_time_audit(
        model_factory=build_model,
        family=point_result["selected"]["family"],
        params=point_result["selected"]["params"],
        x=x,
        residual_target=point_delta,
        actual_points=y,
        naive_points=naive,
        timestamps=[row[index["game_tipoff_utc"]] for row in rows],
        eligible_mask=train_mask | validation_mask,
    )
    validation_rows = [
        row for row, selected in zip(rows, validation_mask, strict=True) if selected
    ]
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

    statuses = [placebo["status"], rolling["status"], segment["status"]]
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
        "dropped_untrainable_features": dropped_untrainable_features,
        "dropped_untrainable_feature_count": len(dropped_untrainable_features),
        "feature_provenance": provenance_manifest(feature_names),
        "leakage_audit": leakage_audit,
        "candidate_results": point_result["candidate_results"],
        "validation_leaderboard": point_result["validation_leaderboard"],
        "selected_model": {
            **point_result["identity"],
            "candidate_id": point_result["selected"]["candidate_id"],
        },
        "selected_params": point_result["selected"]["params"],
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
                "features": feature_names,
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
    )

    print(f"rows={json.dumps(report['rows'], sort_keys=True)}")
    print(
        "dropped_untrainable_features="
        + json.dumps(dropped_untrainable_features, sort_keys=True)
    )
    print(f"selected_model={json.dumps(report['selected_model'], sort_keys=True)}")
    print(f"validation_points={json.dumps(validation_metrics, sort_keys=True)}")
    print(
        "source_family_importance="
        + json.dumps(source_family_importance[:10], sort_keys=True)
    )
    print(
        "grouped_source_importance="
        + json.dumps(grouped_source_importance[:10], sort_keys=True)
    )
    print(f"validation_controls={json.dumps(report['validation_controls'], sort_keys=True)}")
    print("blind_test_opened=false")
    return 0 if controls_status != "fail" else 2


if __name__ == "__main__":
    raise SystemExit(main())
