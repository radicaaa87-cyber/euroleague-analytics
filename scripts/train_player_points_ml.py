"""Benchmark four tree-boosting families for EuroLeague player points.

The script reads the hosted warehouse directly through its read-only database
connection. It never pages play-by-play through ChatGPT/MCP.

Locked split:
    E2023 (2023/24) -> tuning train
    E2024 (2024/25) -> validation and model-family selection
    E2023 + E2024   -> final train for the locked validation winner
    E2025 (2025/26) -> one blind test of that locked winner

HistGradientBoosting, XGBoost, CatBoost and LightGBM all receive the same
feature matrix and the same split. E2025 never changes the selected family or
its parameters.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pickle
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.inspection import permutation_importance
from sklearn.metrics import mean_absolute_error, mean_squared_error

from euroleague.config import DatabaseSettings
from euroleague.mcp.db import connect
from euroleague.ml_benchmark import (
    build_model,
    candidate_specs,
    runtime_model_identity,
)
from euroleague.model_training import model_feature_columns, training_dataset_sql

DEFAULT_TRAIN_SEASON = "E2023"
DEFAULT_VALIDATION_SEASON = "E2024"
DEFAULT_TEST_SEASON = "E2025"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark HistGBR/XGBoost/CatBoost/LightGBM on E2024 validation, "
            "then blind-test the locked winner on E2025."
        )
    )
    parser.add_argument("--train-season", default=DEFAULT_TRAIN_SEASON)
    parser.add_argument("--validation-season", default=DEFAULT_VALIDATION_SEASON)
    parser.add_argument("--test-season", default=DEFAULT_TEST_SEASON)
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


def _as_float(value: Any) -> float:
    if value is None:
        return math.nan
    if isinstance(value, bool):
        return float(value)
    return float(value)


def _metric_summary(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    absolute = np.abs(actual - predicted)
    return {
        "mae": float(mean_absolute_error(actual, predicted)),
        "rmse": float(math.sqrt(mean_squared_error(actual, predicted))),
        "within_2_points": float(np.mean(absolute <= 2.0)),
        "within_3_points": float(np.mean(absolute <= 3.0)),
        "within_4_points": float(np.mean(absolute <= 4.0)),
        "mean_error": float(np.mean(predicted - actual)),
    }


def _fetch_dataset(
    seasons: list[str],
    minutes_basis: str,
    min_history_games: int,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    settings = DatabaseSettings.from_env()
    connection = connect(settings)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                training_dataset_sql(minutes_basis),
                (seasons, min_history_games),
            )
            columns = [description[0] for description in cursor.description]
            rows = cursor.fetchall()
    finally:
        connection.close()
    return columns, rows


def _season_mask(values: np.ndarray, seasons: set[str]) -> np.ndarray:
    return np.array([value in seasons for value in values], dtype=bool)


def _write_predictions(
    path: Path,
    metadata: list[dict[str, Any]],
    actual: np.ndarray,
    predicted: np.ndarray,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "season_code",
        "gamecode",
        "game_date",
        "player_id",
        "player_name",
        "team_code",
        "opponent_team_code",
        "actual_points",
        "predicted_points",
        "prediction_error",
        "absolute_error",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for meta, y_true, y_pred in zip(metadata, actual, predicted, strict=True):
            writer.writerow(
                {
                    **meta,
                    "actual_points": round(float(y_true), 4),
                    "predicted_points": round(float(y_pred), 4),
                    "prediction_error": round(float(y_pred - y_true), 4),
                    "absolute_error": round(float(abs(y_pred - y_true)), 4),
                }
            )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    seasons = [args.train_season, args.validation_season, args.test_season]
    if len(set(seasons)) != 3:
        raise ValueError("train, validation and test seasons must be different.")

    columns, rows = _fetch_dataset(
        seasons,
        args.minutes_basis,
        args.min_history_games,
    )
    if not rows:
        raise RuntimeError("Training query returned no rows.")

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
    season_values = np.asarray([str(row[index["season_code"]]) for row in rows], dtype=object)

    tuning_train_mask = _season_mask(season_values, {args.train_season})
    validation_mask = _season_mask(season_values, {args.validation_season})
    final_train_mask = _season_mask(
        season_values,
        {args.train_season, args.validation_season},
    )
    test_mask = _season_mask(season_values, {args.test_season})

    for label, mask in (
        ("tuning train", tuning_train_mask),
        ("validation", validation_mask),
        ("final train", final_train_mask),
        ("blind test", test_mask),
    ):
        if not np.any(mask):
            raise RuntimeError(f"No rows available for {label} split.")

    candidate_results: list[dict[str, Any]] = []
    best_by_family: dict[str, dict[str, Any]] = {}
    selected: dict[str, Any] | None = None
    selected_mae = math.inf

    for spec in candidate_specs():
        model = build_model(spec.family, spec.params)
        model.fit(x[tuning_train_mask], y[tuning_train_mask])
        validation_prediction = model.predict(x[validation_mask])
        metrics = _metric_summary(y[validation_mask], validation_prediction)
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

    assert selected is not None

    validation_leaderboard = sorted(
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

    selected_identity = runtime_model_identity(selected["family"])
    final_model = build_model(selected["family"], selected["params"])
    final_model.fit(x[final_train_mask], y[final_train_mask])
    test_prediction = final_model.predict(x[test_mask])
    test_metrics = _metric_summary(y[test_mask], test_prediction)

    baseline_index = feature_names.index("pre_l10_points")
    baseline_prediction = x[test_mask, baseline_index]
    baseline_metrics = _metric_summary(y[test_mask], baseline_prediction)

    # The blind test has already been scored at this point. Permutation
    # importance is diagnostic only and cannot change family or parameter selection.
    test_x = x[test_mask]
    test_y = y[test_mask]
    if len(test_y) > 2500:
        rng = np.random.default_rng(42)
        importance_indices = np.sort(rng.choice(len(test_y), size=2500, replace=False))
        importance_x = test_x[importance_indices]
        importance_y = test_y[importance_indices]
    else:
        importance_x = test_x
        importance_y = test_y

    importance = permutation_importance(
        final_model,
        importance_x,
        importance_y,
        scoring="neg_mean_absolute_error",
        n_repeats=5,
        random_state=42,
        n_jobs=1,
    )
    ranked_importance = sorted(
        (
            {
                "feature": feature,
                "importance_mean": float(mean),
                "importance_std": float(std),
            }
            for feature, mean, std in zip(
                feature_names,
                importance.importances_mean,
                importance.importances_std,
                strict=True,
            )
        ),
        key=lambda item: item["importance_mean"],
        reverse=True,
    )

    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "split": {
            "tuning_train": [args.train_season],
            "validation": [args.validation_season],
            "final_train": [args.train_season, args.validation_season],
            "blind_test": [args.test_season],
        },
        "minutes_basis": args.minutes_basis,
        "min_history_games": args.min_history_games,
        "rows": {
            "all": len(rows),
            "tuning_train": int(np.sum(tuning_train_mask)),
            "validation": int(np.sum(validation_mask)),
            "final_train": int(np.sum(final_train_mask)),
            "blind_test": int(np.sum(test_mask)),
        },
        "feature_count": len(feature_names),
        "features": feature_names,
        "candidate_results": candidate_results,
        "validation_leaderboard": validation_leaderboard,
        "selected_model": {
            **selected_identity,
            "candidate_id": selected["candidate_id"],
        },
        "selected_params": selected["params"],
        "blind_test": test_metrics,
        "l10_points_baseline": baseline_metrics,
        "mae_improvement_vs_l10": float(baseline_metrics["mae"] - test_metrics["mae"]),
        "permutation_importance": ranked_importance,
        "notes": [
            "All four model families use the identical feature matrix and chronological split.",
            "Family and parameter selection use E2024 validation only.",
            "Only the locked E2024 validation winner is scored on the E2025 blind test.",
            "All model inputs are pre-game pre_* features plus is_home.",
            "PBP-derived features are aggregated server-side from possessions, lineups and stints.",
            "Bookmaker lines are excluded; betting EDGE is evaluated later by "
            "joining locked predictions.",
        ],
    }

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    args.model.parent.mkdir(parents=True, exist_ok=True)
    with args.model.open("wb") as handle:
        pickle.dump(
            {
                "model": final_model,
                "model_family": selected["family"],
                "model_identity": selected_identity,
                "features": feature_names,
                "selected_params": selected["params"],
                "minutes_basis": args.minutes_basis,
                "trained_seasons": [args.train_season, args.validation_season],
                "blind_test_season": args.test_season,
            },
            handle,
            protocol=pickle.HIGHEST_PROTOCOL,
        )

    metadata_fields = [
        "season_code",
        "gamecode",
        "game_date",
        "player_id",
        "player_name",
        "team_code",
        "opponent_team_code",
    ]
    test_rows = [row for row, selected_row in zip(rows, test_mask, strict=True) if selected_row]
    metadata = [{field: row[index[field]] for field in metadata_fields} for row in test_rows]
    _write_predictions(
        args.predictions,
        metadata,
        y[test_mask],
        test_prediction,
    )

    print(json.dumps(report["rows"], sort_keys=True))
    print(json.dumps(report["validation_leaderboard"], sort_keys=True))
    print(json.dumps(report["blind_test"], sort_keys=True))
    print(f"selected_model={json.dumps(report['selected_model'], sort_keys=True)}")
    print(f"selected_params={json.dumps(selected['params'], sort_keys=True)}")
    print(f"features={len(feature_names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
