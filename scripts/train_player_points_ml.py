"""Benchmark four tree-boosting families for EuroLeague player points.

The point model is residual by construction: every row starts from a leakage-safe
pregame simple scoring average, the model learns the delta from that baseline,
and the final prediction is baseline + predicted delta. This keeps the model
anchored to the player's established scoring level while allowing role, volume,
matchup, availability, fatigue and other pre-game signals to move it.

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
from euroleague.leakage import (
    assert_feature_cutoffs_before_tipoff,
    assert_prefix_invariance,
)
from euroleague.mcp.db import connect
from euroleague.ml_benchmark import (
    build_model,
    candidate_specs,
    runtime_model_identity,
)
from euroleague.model_training import model_feature_columns, training_dataset_sql
from euroleague.role_projection import role_base_projection

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
    naive_baseline: np.ndarray,
    predicted_delta: np.ndarray,
    predicted: np.ndarray,
    actual_minutes: np.ndarray,
    predicted_minutes: np.ndarray,
    actual_fga: np.ndarray,
    predicted_fga: np.ndarray,
    actual_3pa: np.ndarray,
    predicted_3pa: np.ndarray,
    actual_fta: np.ndarray,
    predicted_fta: np.ndarray,
    role_base_points: np.ndarray,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "season_code",
        "gamecode",
        "game_tipoff_utc",
        "feature_cutoff_time",
        "game_date",
        "player_id",
        "player_name",
        "team_code",
        "opponent_team_code",
        "actual_minutes",
        "predicted_minutes",
        "actual_fga",
        "predicted_fga",
        "actual_3pa",
        "predicted_3pa",
        "actual_fta",
        "predicted_fta",
        "role_base_points",
        "role_base_delta_vs_naive",
        "naive_baseline_points",
        "actual_delta_vs_naive",
        "predicted_delta_vs_naive",
        "actual_points",
        "predicted_points",
        "prediction_error",
        "absolute_error",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for (
            meta,
            y_true,
            baseline,
            delta_pred,
            y_pred,
            m_true,
            m_pred,
            f_true,
            f_pred,
            t_true,
            t_pred,
            ft_true,
            ft_pred,
            role_base,
        ) in zip(
            metadata,
            actual,
            naive_baseline,
            predicted_delta,
            predicted,
            actual_minutes,
            predicted_minutes,
            actual_fga,
            predicted_fga,
            actual_3pa,
            predicted_3pa,
            actual_fta,
            predicted_fta,
            role_base_points,
            strict=True,
        ):
            writer.writerow(
                {
                    **meta,
                    "actual_minutes": round(float(m_true), 4),
                    "predicted_minutes": round(float(m_pred), 4),
                    "actual_fga": round(float(f_true), 4),
                    "predicted_fga": round(float(f_pred), 4),
                    "actual_3pa": round(float(t_true), 4),
                    "predicted_3pa": round(float(t_pred), 4),
                    "actual_fta": round(float(ft_true), 4),
                    "predicted_fta": round(float(ft_pred), 4),
                    "role_base_points": round(float(role_base), 4),
                    "role_base_delta_vs_naive": round(float(role_base - baseline), 4),
                    "naive_baseline_points": round(float(baseline), 4),
                    "actual_delta_vs_naive": round(float(y_true - baseline), 4),
                    "predicted_delta_vs_naive": round(float(delta_pred), 4),
                    "actual_points": round(float(y_true), 4),
                    "predicted_points": round(float(y_pred), 4),
                    "prediction_error": round(float(y_pred - y_true), 4),
                    "absolute_error": round(float(abs(y_pred - y_true)), 4),
                }
            )


def _benchmark_target(
    x: np.ndarray,
    y: np.ndarray,
    tuning_train_mask: np.ndarray,
    validation_mask: np.ndarray,
    final_train_mask: np.ndarray,
    test_mask: np.ndarray,
) -> dict[str, Any]:
    """Select one model family on validation and blind-score it once."""
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
    identity = runtime_model_identity(selected["family"])
    final_model = build_model(selected["family"], selected["params"])
    final_model.fit(x[final_train_mask], y[final_train_mask])
    test_prediction = final_model.predict(x[test_mask])

    return {
        "candidate_results": candidate_results,
        "validation_leaderboard": leaderboard,
        "selected": selected,
        "identity": identity,
        "model": final_model,
        "test_prediction": test_prediction,
        "blind_test": _metric_summary(y[test_mask], test_prediction),
    }


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

    baseline_columns, baseline_rows = _fetch_dataset(
        [args.train_season, args.validation_season],
        args.minutes_basis,
        args.min_history_games,
    )
    if not baseline_rows:
        raise RuntimeError("Leakage baseline query returned no rows.")

    cutoff_audit = assert_feature_cutoffs_before_tipoff(columns, rows)
    prefix_audit = assert_prefix_invariance(
        baseline_columns,
        baseline_rows,
        columns,
        rows,
    )
    leakage_audit = {
        "feature_cutoff": cutoff_audit,
        "future_prefix_invariance": prefix_audit,
        "baseline_seasons": [args.train_season, args.validation_season],
        "future_added_season": args.test_season,
    }
    print(f"leakage_audit={json.dumps(leakage_audit, sort_keys=True)}")

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
    naive_baseline = np.asarray(
        [float(row[index["pre_naive_points_mean"]]) for row in rows],
        dtype=float,
    )
    if not np.all(np.isfinite(naive_baseline)):
        raise RuntimeError("Naive baseline contains non-finite values.")
    point_delta_y = y - naive_baseline
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

    point_result = _benchmark_target(
        x,
        point_delta_y,
        tuning_train_mask,
        validation_mask,
        final_train_mask,
        test_mask,
    )
    candidate_results = point_result["candidate_results"]
    validation_leaderboard = point_result["validation_leaderboard"]
    selected = point_result["selected"]
    selected_identity = point_result["identity"]
    final_model = point_result["model"]
    test_prediction_delta = point_result["test_prediction"]
    test_naive_baseline = naive_baseline[test_mask]
    test_prediction = test_naive_baseline + test_prediction_delta
    test_metrics = _metric_summary(y[test_mask], test_prediction)

    minutes_y = np.asarray(
        [float(row[index["target_minutes"]]) for row in rows],
        dtype=float,
    )
    fga_y = np.asarray(
        [float(row[index["target_fga"]]) for row in rows],
        dtype=float,
    )
    three_pa_y = np.asarray(
        [float(row[index["target_3pa"]]) for row in rows],
        dtype=float,
    )
    fta_y = np.asarray(
        [float(row[index["target_fta"]]) for row in rows],
        dtype=float,
    )

    fga_per_minute_y = np.divide(
        fga_y,
        minutes_y,
        out=np.zeros_like(fga_y),
        where=minutes_y > 0,
    )
    three_share_y = np.divide(
        three_pa_y,
        fga_y,
        out=np.zeros_like(three_pa_y),
        where=fga_y > 0,
    )
    fta_per_minute_y = np.divide(
        fta_y,
        minutes_y,
        out=np.zeros_like(fta_y),
        where=minutes_y > 0,
    )

    minutes_result = _benchmark_target(
        x,
        minutes_y,
        tuning_train_mask,
        validation_mask,
        final_train_mask,
        test_mask,
    )
    fga_rate_result = _benchmark_target(
        x,
        fga_per_minute_y,
        tuning_train_mask,
        validation_mask,
        final_train_mask,
        test_mask,
    )
    three_share_result = _benchmark_target(
        x,
        three_share_y,
        tuning_train_mask,
        validation_mask,
        final_train_mask,
        test_mask,
    )
    fta_rate_result = _benchmark_target(
        x,
        fta_per_minute_y,
        tuning_train_mask,
        validation_mask,
        final_train_mask,
        test_mask,
    )

    predicted_minutes = np.clip(minutes_result["test_prediction"], 0.0, 50.0)
    predicted_fga_per_minute = np.clip(
        fga_rate_result["test_prediction"],
        0.0,
        1.5,
    )
    predicted_three_share = np.clip(
        three_share_result["test_prediction"],
        0.0,
        1.0,
    )
    predicted_fta_per_minute = np.clip(
        fta_rate_result["test_prediction"],
        0.0,
        1.5,
    )
    predicted_fga = predicted_minutes * predicted_fga_per_minute
    predicted_3pa = predicted_fga * predicted_three_share
    predicted_fta = predicted_minutes * predicted_fta_per_minute

    fga_total_metrics = _metric_summary(fga_y[test_mask], predicted_fga)
    three_pa_total_metrics = _metric_summary(three_pa_y[test_mask], predicted_3pa)
    fta_total_metrics = _metric_summary(fta_y[test_mask], predicted_fta)

    baseline_index = feature_names.index("pre_l10_points")
    l10_baseline_prediction = x[test_mask, baseline_index]
    l10_baseline_metrics = _metric_summary(y[test_mask], l10_baseline_prediction)
    naive_baseline_metrics = _metric_summary(y[test_mask], test_naive_baseline)

    # The blind test has already been scored at this point. Permutation
    # importance is diagnostic only and cannot change family or parameter selection.
    test_x = x[test_mask]
    test_y = point_delta_y[test_mask]
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
        "leakage_audit": leakage_audit,
        "candidate_results": candidate_results,
        "validation_leaderboard": validation_leaderboard,
        "selected_model": {
            **selected_identity,
            "candidate_id": selected["candidate_id"],
        },
        "selected_params": selected["params"],
        "blind_test": test_metrics,
        "auxiliary_targets": {
            "minutes": {
                "validation_leaderboard": minutes_result["validation_leaderboard"],
                "selected_model": {
                    **minutes_result["identity"],
                    "candidate_id": minutes_result["selected"]["candidate_id"],
                },
                "selected_params": minutes_result["selected"]["params"],
                "blind_test": minutes_result["blind_test"],
            },
            "fga": {
                "validation_leaderboard": fga_result["validation_leaderboard"],
                "selected_model": {
                    **fga_result["identity"],
                    "candidate_id": fga_result["selected"]["candidate_id"],
                },
                "selected_params": fga_result["selected"]["params"],
                "blind_test": fga_result["blind_test"],
            },
        },
        "prediction_architecture": {
            "base": "pre_naive_points_mean",
            "learned_target": "target_points - pre_naive_points_mean",
            "final_prediction": "pre_naive_points_mean + predicted_delta",
        },
        "naive_points_baseline": naive_baseline_metrics,
        "l10_points_baseline": l10_baseline_metrics,
        "mae_improvement_vs_naive": float(
            naive_baseline_metrics["mae"] - test_metrics["mae"]
        ),
        "mae_improvement_vs_l10": float(
            l10_baseline_metrics["mae"] - test_metrics["mae"]
        ),
        "permutation_importance": ranked_importance,
        "notes": [
            "All four model families use the identical feature matrix and chronological split.",
            "Family and parameter selection use E2024 validation only.",
            "Only the locked E2024 validation winner is scored on the E2025 blind test.",
            "The naive baseline is the simple average of prior games in the current season; "
            "for a season opener it falls back to the player's prior history.",
            "The point model predicts a residual from the leakage-safe simple pre-game "
            "average; final points equal naive baseline plus predicted residual.",
            "All model inputs are pre-game pre_* features plus is_home.",
            "PBP-derived features are aggregated server-side from possessions, lineups and stints.",
            "Minutes and FGA are independently forecast as auxiliary role and volume targets.",
            "Role-volatility features separate recurring variance from one-game contextual shocks.",
            "A temporal leakage gate requires every feature cutoff to precede tipoff.",
            "Adding E2025 must leave every E2023/E2024 model feature byte-for-byte equal.",
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
                "point_model_target": "delta_vs_naive",
                "naive_baseline_feature": "pre_naive_points_mean",
                "minutes_model": minutes_result["model"],
                "fga_model": fga_result["model"],
                "model_family": selected["family"],
                "model_identity": selected_identity,
                "features": feature_names,
                "selected_params": selected["params"],
                "minutes_selected_model": minutes_result["identity"],
                "minutes_selected_params": minutes_result["selected"]["params"],
                "fga_selected_model": fga_result["identity"],
                "fga_selected_params": fga_result["selected"]["params"],
                "minutes_basis": args.minutes_basis,
                "trained_seasons": [args.train_season, args.validation_season],
                "blind_test_season": args.test_season,
                "leakage_audit": leakage_audit,
            },
            handle,
            protocol=pickle.HIGHEST_PROTOCOL,
        )

    metadata_fields = [
        "season_code",
        "gamecode",
        "game_tipoff_utc",
        "feature_cutoff_time",
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
        test_naive_baseline,
        test_prediction_delta,
        test_prediction,
        minutes_y[test_mask],
        minutes_result["test_prediction"],
        fga_y[test_mask],
        fga_result["test_prediction"],
    )

    print(json.dumps(report["rows"], sort_keys=True))
    print(json.dumps(report["validation_leaderboard"], sort_keys=True))
    print(json.dumps(report["blind_test"], sort_keys=True))
    print(
        "minutes_blind_test="
        + json.dumps(report["auxiliary_targets"]["minutes"]["blind_test"], sort_keys=True)
    )
    print(
        "fga_blind_test="
        + json.dumps(report["auxiliary_targets"]["fga"]["blind_test"], sort_keys=True)
    )
    print(f"selected_model={json.dumps(report['selected_model'], sort_keys=True)}")
    print(f"selected_params={json.dumps(selected['params'], sort_keys=True)}")
    print(f"features={len(feature_names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
