"""Benchmark six model families for EuroLeague player points.

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

Ridge, ExtraTrees, HistGradientBoosting, XGBoost, CatBoost and LightGBM all receive the same
feature matrix and the same split. E2025 never changes the selected family or
its parameters.
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
from sklearn.metrics import mean_absolute_error, mean_squared_error

from euroleague.config import DatabaseSettings
from euroleague.feature_provenance import provenance_manifest
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
from euroleague.model_validation import (
    placebo_target_audit,
    rolling_time_audit,
    segment_stability_audit,
    write_preblind_lock,
)
from euroleague.role_projection import role_base_projection

DEFAULT_TRAIN_SEASON = "E2023"
DEFAULT_VALIDATION_SEASON = "E2024"
DEFAULT_TEST_SEASON = "E2025"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark Ridge/ExtraTrees/HistGBR/XGBoost/CatBoost/LightGBM on E2024 validation, "
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
    parser.add_argument(
        "--preblind-lock",
        type=Path,
        help="write the pre-blind dataset/prediction hash manifest here",
    )
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


def _finite_median(values: np.ndarray, fallback: float) -> float:
    finite = values[np.isfinite(values)]
    if len(finite) == 0:
        return fallback
    return float(np.median(finite))


def _residual_quantiles(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    residual = actual - predicted
    quantiles = np.quantile(residual, [0.10, 0.25, 0.50, 0.75, 0.90])
    return {
        "p10": float(quantiles[0]),
        "p25": float(quantiles[1]),
        "p50": float(quantiles[2]),
        "p75": float(quantiles[3]),
        "p90": float(quantiles[4]),
    }


def _volatility_thresholds(values: np.ndarray) -> dict[str, float]:
    finite = values[np.isfinite(values)]
    if len(finite) < 3:
        return {"stable_max": 3.0, "medium_max": 5.0}
    q33, q67 = np.quantile(finite, [1.0 / 3.0, 2.0 / 3.0])
    return {"stable_max": float(q33), "medium_max": float(q67)}


def _volatility_band(value: float, thresholds: dict[str, float]) -> str:
    if not np.isfinite(value):
        return "unknown"
    if value <= thresholds["stable_max"]:
        return "stable"
    if value <= thresholds["medium_max"]:
        return "medium"
    return "volatile"


def _uncertainty_calibration(
    actual: np.ndarray,
    predicted: np.ndarray,
    volatility: np.ndarray,
    thresholds: dict[str, float],
) -> dict[str, Any]:
    global_quantiles = _residual_quantiles(actual, predicted)
    bands: dict[str, dict[str, Any]] = {}
    labels = np.asarray(
        [_volatility_band(float(value), thresholds) for value in volatility],
        dtype=object,
    )
    for band in ("stable", "medium", "volatile"):
        mask = labels == band
        count = int(np.sum(mask))
        quantiles = (
            _residual_quantiles(actual[mask], predicted[mask])
            if count >= 30
            else dict(global_quantiles)
        )
        bands[band] = {
            "rows": count,
            "used_global_fallback": count < 30,
            "residual_quantiles": quantiles,
        }
    return {
        "method": "validation residual quantiles stratified by pre_l10_points_std",
        "thresholds": thresholds,
        "global_residual_quantiles": global_quantiles,
        "bands": bands,
    }


def _apply_uncertainty(
    predicted: np.ndarray,
    volatility: np.ndarray,
    calibration: dict[str, Any],
) -> tuple[list[str], dict[str, np.ndarray]]:
    labels: list[str] = []
    output = {
        key: np.zeros(len(predicted), dtype=float) for key in ("p10", "p25", "p50", "p75", "p90")
    }
    thresholds = calibration["thresholds"]
    for idx, (prediction, value) in enumerate(zip(predicted, volatility, strict=True)):
        band = _volatility_band(float(value), thresholds)
        labels.append(band)
        source_band = band if band in calibration["bands"] else None
        if source_band is None:
            quantiles = calibration["global_residual_quantiles"]
        else:
            quantiles = calibration["bands"][source_band]["residual_quantiles"]
        for key in output:
            output[key][idx] = float(prediction) + float(quantiles[key])
    return labels, output


def _interval_metrics(
    actual: np.ndarray,
    intervals: dict[str, np.ndarray],
) -> dict[str, float]:
    return {
        "p25_p75_coverage": float(
            np.mean((actual >= intervals["p25"]) & (actual <= intervals["p75"]))
        ),
        "p10_p90_coverage": float(
            np.mean((actual >= intervals["p10"]) & (actual <= intervals["p90"]))
        ),
        "mean_p25_p75_width": float(np.mean(intervals["p75"] - intervals["p25"])),
        "mean_p10_p90_width": float(np.mean(intervals["p90"] - intervals["p10"])),
    }


def _group_permutation_importance(
    model: Any,
    x: np.ndarray,
    y: np.ndarray,
    features: list[str],
    provenance: list[dict[str, str]],
    *,
    repeats: int = 5,
) -> list[dict[str, Any]]:
    """Measure whole source families by shuffling their columns together."""
    baseline_mae = float(mean_absolute_error(y, model.predict(x)))
    family_indices: dict[str, list[int]] = {}
    for idx, item in enumerate(provenance):
        family_indices.setdefault(item["source_family"], []).append(idx)

    rng = np.random.default_rng(42)
    results: list[dict[str, Any]] = []
    for family, indices in sorted(family_indices.items()):
        deltas: list[float] = []
        for _ in range(repeats):
            permutation = rng.permutation(len(x))
            shuffled = x.copy()
            shuffled[:, indices] = x[permutation][:, indices]
            shuffled_mae = float(mean_absolute_error(y, model.predict(shuffled)))
            deltas.append(shuffled_mae - baseline_mae)
        results.append(
            {
                "source_family": family,
                "feature_count": len(indices),
                "features": [features[index] for index in indices],
                "mae_increase_mean": float(np.mean(deltas)),
                "mae_increase_std": float(np.std(deltas)),
            }
        )
    return sorted(results, key=lambda item: item["mae_increase_mean"], reverse=True)


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
    uncertainty_band: list[str],
    uncertainty_intervals: dict[str, np.ndarray],
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
        "uncertainty_band",
        "p10_points",
        "p25_points",
        "p50_points",
        "p75_points",
        "p90_points",
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
            uncertainty_label,
            p10,
            p25,
            p50,
            p75,
            p90,
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
            uncertainty_band,
            uncertainty_intervals["p10"],
            uncertainty_intervals["p25"],
            uncertainty_intervals["p50"],
            uncertainty_intervals["p75"],
            uncertainty_intervals["p90"],
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
                    "uncertainty_band": uncertainty_label,
                    "p10_points": round(float(p10), 4),
                    "p25_points": round(float(p25), 4),
                    "p50_points": round(float(p50), 4),
                    "p75_points": round(float(p75), 4),
                    "p90_points": round(float(p90), 4),
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
    selected_validation_prediction: np.ndarray | None = None
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
            selected_validation_prediction = np.asarray(
                validation_prediction,
                dtype=float,
            ).copy()

    assert selected is not None
    assert selected_validation_prediction is not None

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
        "validation_prediction": selected_validation_prediction,
        "test_prediction": test_prediction,
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

    baseline_index = feature_names.index("pre_l10_points")
    l10_baseline_prediction = x[test_mask, baseline_index]

    two_pct_index = feature_names.index("pre_l10_2p_pct")
    three_pct_index = feature_names.index("pre_l10_3p_pct")
    ft_pct_index = feature_names.index("pre_l10_ft_pct")
    efficiency_priors = {
        "two_pct": _finite_median(x[final_train_mask, two_pct_index], 0.53),
        "three_pct": _finite_median(x[final_train_mask, three_pct_index], 0.35),
        "ft_pct": _finite_median(x[final_train_mask, ft_pct_index], 0.78),
    }

    test_two_pct = np.where(
        np.isfinite(x[test_mask, two_pct_index]),
        x[test_mask, two_pct_index],
        efficiency_priors["two_pct"],
    )
    test_three_pct = np.where(
        np.isfinite(x[test_mask, three_pct_index]),
        x[test_mask, three_pct_index],
        efficiency_priors["three_pct"],
    )
    test_ft_pct = np.where(
        np.isfinite(x[test_mask, ft_pct_index]),
        x[test_mask, ft_pct_index],
        efficiency_priors["ft_pct"],
    )

    role_base_rows = [
        role_base_projection(
            predicted_minutes=float(minutes),
            predicted_fga_per_minute=float(fga_rate),
            predicted_three_share=float(three_share),
            predicted_fta_per_minute=float(fta_rate),
            two_pct=float(two_pct),
            three_pct=float(three_pct),
            ft_pct=float(ft_pct),
        )
        for minutes, fga_rate, three_share, fta_rate, two_pct, three_pct, ft_pct in zip(
            predicted_minutes,
            predicted_fga_per_minute,
            predicted_three_share,
            predicted_fta_per_minute,
            test_two_pct,
            test_three_pct,
            test_ft_pct,
            strict=True,
        )
    ]
    role_base_points = np.asarray([row.points for row in role_base_rows], dtype=float)

    volatility_index = feature_names.index("pre_l10_points_std")
    volatility_thresholds = _volatility_thresholds(x[tuning_train_mask, volatility_index])
    uncertainty_calibration = _uncertainty_calibration(
        point_delta_y[validation_mask],
        point_result["validation_prediction"],
        x[validation_mask, volatility_index],
        volatility_thresholds,
    )
    uncertainty_band, uncertainty_intervals = _apply_uncertainty(
        test_prediction,
        x[test_mask, volatility_index],
        uncertainty_calibration,
    )

    row_key_fields = ("season_code", "gamecode", "player_id", "feature_cutoff_time")
    all_row_keys = [tuple(row[index[field]] for field in row_key_fields) for row in rows]
    legal_target_row_keys = [
        key
        for key, selected_row in zip(all_row_keys, final_train_mask, strict=True)
        if selected_row
    ]
    blind_row_keys = [
        key for key, selected_row in zip(all_row_keys, test_mask, strict=True) if selected_row
    ]
    split_manifest = {
        "tuning_train": [args.train_season],
        "validation": [args.validation_season],
        "final_train": [args.train_season, args.validation_season],
        "blind_test": [args.test_season],
    }
    lock_path = args.preblind_lock or args.report.with_name("preblind_lock.json")
    preblind_lock = write_preblind_lock(
        lock_path,
        feature_names=feature_names,
        all_row_keys=all_row_keys,
        matrix=x,
        legal_target_row_keys=legal_target_row_keys,
        legal_target=point_delta_y[final_train_mask],
        blind_row_keys=blind_row_keys,
        blind_predictions={
            "predicted_delta_vs_naive": test_prediction_delta,
            "predicted_points": test_prediction,
            "predicted_minutes": predicted_minutes,
            "predicted_fga_per_minute": predicted_fga_per_minute,
            "predicted_fga": predicted_fga,
            "predicted_three_point_share": predicted_three_share,
            "predicted_3pa": predicted_3pa,
            "predicted_fta_per_minute": predicted_fta_per_minute,
            "predicted_fta": predicted_fta,
            "role_base_points": role_base_points,
            "p10_points": uncertainty_intervals["p10"],
            "p25_points": uncertainty_intervals["p25"],
            "p50_points": uncertainty_intervals["p50"],
            "p75_points": uncertainty_intervals["p75"],
            "p90_points": uncertainty_intervals["p90"],
        },
        model_identity=selected_identity,
        selected_params=selected["params"],
        split=split_manifest,
        created_at=datetime.now(UTC).isoformat(),
        git_commit=os.environ.get("GITHUB_SHA"),
    )
    print(f"preblind_lock={json.dumps(preblind_lock, sort_keys=True)}")

    # Blind outcomes are opened only after the prediction lock above exists.
    test_metrics = _metric_summary(y[test_mask], test_prediction)
    minutes_blind_metrics = _metric_summary(
        minutes_y[test_mask],
        predicted_minutes,
    )
    fga_rate_blind_metrics = _metric_summary(
        fga_per_minute_y[test_mask],
        predicted_fga_per_minute,
    )
    three_share_blind_metrics = _metric_summary(
        three_share_y[test_mask],
        predicted_three_share,
    )
    fta_rate_blind_metrics = _metric_summary(
        fta_per_minute_y[test_mask],
        predicted_fta_per_minute,
    )
    fga_total_metrics = _metric_summary(fga_y[test_mask], predicted_fga)
    three_pa_total_metrics = _metric_summary(three_pa_y[test_mask], predicted_3pa)
    fta_total_metrics = _metric_summary(fta_y[test_mask], predicted_fta)
    l10_baseline_metrics = _metric_summary(y[test_mask], l10_baseline_prediction)
    naive_baseline_metrics = _metric_summary(y[test_mask], test_naive_baseline)
    role_base_metrics = _metric_summary(y[test_mask], role_base_points)
    uncertainty_metrics = _interval_metrics(
        y[test_mask],
        uncertainty_intervals,
    )

    placebo_audit = placebo_target_audit(
        model_factory=build_model,
        family=selected["family"],
        params=selected["params"],
        x=x,
        residual_target=point_delta_y,
        train_mask=final_train_mask,
        test_mask=test_mask,
        real_test_prediction=test_prediction_delta,
    )
    rolling_audit = rolling_time_audit(
        model_factory=build_model,
        family=selected["family"],
        params=selected["params"],
        x=x,
        residual_target=point_delta_y,
        actual_points=y,
        naive_points=naive_baseline,
        timestamps=[row[index["game_tipoff_utc"]] for row in rows],
        eligible_mask=final_train_mask,
    )
    test_rows_for_stability = [
        row for row, selected_row in zip(rows, test_mask, strict=True) if selected_row
    ]
    segment_audit = segment_stability_audit(
        actual=y[test_mask],
        predicted=test_prediction,
        naive=test_naive_baseline,
        segments={
            "player": [row[index["player_id"]] for row in test_rows_for_stability],
            "team": [row[index["team_code"]] for row in test_rows_for_stability],
            "month": [str(row[index["game_date"]])[:7] for row in test_rows_for_stability],
            "starter_state": [
                "starter" if bool(row[index["target_was_starter"]]) else "bench"
                for row in test_rows_for_stability
            ],
            "volatility": uncertainty_band,
        },
    )
    control_statuses = [
        placebo_audit["status"],
        rolling_audit["status"],
        segment_audit["status"],
    ]
    if "fail" in control_statuses:
        controls_status = "fail"
    elif "warn" in control_statuses or "insufficient" in control_statuses:
        controls_status = "warn"
    else:
        controls_status = "pass"
    validation_controls = {
        "status": controls_status,
        "preblind_lock": preblind_lock,
        "placebo_target_audit": placebo_audit,
        "rolling_time_audit": rolling_audit,
        "segment_stability_audit": segment_audit,
    }

    # The blind test has now been scored. Permutation
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
    provenance = provenance_manifest(feature_names)
    provenance_by_feature = {item["feature"]: item for item in provenance}
    ranked_importance = sorted(
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
                importance.importances_mean,
                importance.importances_std,
                strict=True,
            )
        ),
        key=lambda item: item["importance_mean"],
        reverse=True,
    )
    family_totals: dict[str, dict[str, Any]] = {}
    for item in ranked_importance:
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
    grouped_source_importance = _group_permutation_importance(
        final_model,
        importance_x,
        importance_y,
        feature_names,
        provenance,
        repeats=5,
    )

    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "split": split_manifest,
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
        "feature_provenance": provenance,
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
                "blind_test": minutes_blind_metrics,
            },
            "fga_per_minute": {
                "validation_leaderboard": fga_rate_result["validation_leaderboard"],
                "selected_model": {
                    **fga_rate_result["identity"],
                    "candidate_id": fga_rate_result["selected"]["candidate_id"],
                },
                "selected_params": fga_rate_result["selected"]["params"],
                "blind_test": fga_rate_blind_metrics,
            },
            "three_point_share": {
                "validation_leaderboard": three_share_result["validation_leaderboard"],
                "selected_model": {
                    **three_share_result["identity"],
                    "candidate_id": three_share_result["selected"]["candidate_id"],
                },
                "selected_params": three_share_result["selected"]["params"],
                "blind_test": three_share_blind_metrics,
            },
            "fta_per_minute": {
                "validation_leaderboard": fta_rate_result["validation_leaderboard"],
                "selected_model": {
                    **fta_rate_result["identity"],
                    "candidate_id": fta_rate_result["selected"]["candidate_id"],
                },
                "selected_params": fta_rate_result["selected"]["params"],
                "blind_test": fta_rate_blind_metrics,
            },
            "derived_attempts": {
                "fga": fga_total_metrics,
                "3pa": three_pa_total_metrics,
                "fta": fta_total_metrics,
            },
        },
        "prediction_architecture": {
            "naive_base": "pre_naive_points_mean",
            "role_chain": (
                "predicted MIN -> predicted FGA/min -> predicted FGA -> "
                "predicted 3PA share + predicted FTA/min -> ROLE BASE PTS"
            ),
            "role_base_formula": (
                "2 * predicted_2PA * pre_l10_2p_pct + "
                "3 * predicted_3PA * pre_l10_3p_pct + "
                "predicted_FTA * pre_l10_ft_pct"
            ),
            "learned_target": "target_points - pre_naive_points_mean",
            "final_prediction": "pre_naive_points_mean + predicted_delta",
        },
        "efficiency_priors": efficiency_priors,
        "naive_points_baseline": naive_baseline_metrics,
        "role_base_points": role_base_metrics,
        "l10_points_baseline": l10_baseline_metrics,
        "mae_improvement_role_base_vs_naive": float(
            naive_baseline_metrics["mae"] - role_base_metrics["mae"]
        ),
        "mae_improvement_vs_naive": float(naive_baseline_metrics["mae"] - test_metrics["mae"]),
        "mae_improvement_vs_l10": float(l10_baseline_metrics["mae"] - test_metrics["mae"]),
        "uncertainty_calibration": uncertainty_calibration,
        "blind_test_uncertainty": uncertainty_metrics,
        "permutation_importance": ranked_importance,
        "source_family_importance": source_family_importance,
        "grouped_source_permutation_importance": grouped_source_importance,
        "validation_controls": validation_controls,
        "notes": [
            "All six model families use the identical feature matrix and chronological split.",
            "Family and parameter selection use E2024 validation only.",
            "Only the locked E2024 validation winner is scored on the E2025 blind test.",
            "The naive baseline is the simple average of prior games in the current season; "
            "for a season opener it falls back to the player's prior history.",
            "The point model predicts a residual from the leakage-safe simple pre-game "
            "average; final points equal naive baseline plus predicted residual.",
            "All model inputs are pre-game pre_* features plus is_home.",
            "PBP-derived features are aggregated server-side from possessions, lineups and stints.",
            "The transparent role chain forecasts minutes first, then FGA/min, "
            "3PA share and FTA/min; those forecasts produce expected FGA/3PA/FTA totals.",
            "ROLE BASE converts expected attempts to points with leakage-safe L10 2P/3P/FT "
            "efficiency, using priors learned only from final-train pre-game features when "
            "a player lacks enough historical attempts.",
            "Role-volatility features separate recurring variance from one-game contextual shocks.",
            "A temporal leakage gate requires every feature cutoff to precede tipoff.",
            "Adding E2025 must leave every E2023/E2024 model feature byte-for-byte equal.",
            "A pre-blind lock hashes the feature matrix, legal E2023/E2024 targets and all "
            "E2025 predictions before any E2025 outcome metric is calculated.",
            "A shuffled-target placebo, expanding rolling-time checks and segment stability "
            "audit are mandatory diagnostics around the locked blind test.",
            "Prediction ranges are calibrated only from validation residuals; "
            "the blind-test outcomes never set P10/P25/P50/P75/P90 interval widths.",
            "Uncertainty is stratified by leakage-safe pre_l10_points_std so stable and "
            "volatile scorers do not automatically receive the same interval.",
            "Feature provenance is stored with the artifact so importance can be read "
            "by source family rather than as anonymous numeric columns.",
            "Grouped source permutation importance shuffles every feature in one source family "
            "together on the already-scored blind sample; it is diagnostic only and never "
            "changes model selection.",
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
                "fga_model": fga_rate_result["model"],
                "fga_model_target": "fga_per_minute",
                "fga_per_minute_model": fga_rate_result["model"],
                "three_point_share_model": three_share_result["model"],
                "fta_per_minute_model": fta_rate_result["model"],
                "role_base_efficiency_features": {
                    "two_pct": "pre_l10_2p_pct",
                    "three_pct": "pre_l10_3p_pct",
                    "ft_pct": "pre_l10_ft_pct",
                },
                "role_base_efficiency_priors": efficiency_priors,
                "model_family": selected["family"],
                "model_identity": selected_identity,
                "features": feature_names,
                "feature_provenance": provenance,
                "uncertainty_calibration": uncertainty_calibration,
                "grouped_source_permutation_importance": grouped_source_importance,
                "selected_params": selected["params"],
                "minutes_selected_model": minutes_result["identity"],
                "minutes_selected_params": minutes_result["selected"]["params"],
                "fga_selected_model": fga_rate_result["identity"],
                "fga_selected_params": fga_rate_result["selected"]["params"],
                "fga_selected_target": "fga_per_minute",
                "three_point_share_selected_model": three_share_result["identity"],
                "three_point_share_selected_params": three_share_result["selected"]["params"],
                "fta_per_minute_selected_model": fta_rate_result["identity"],
                "fta_per_minute_selected_params": fta_rate_result["selected"]["params"],
                "minutes_basis": args.minutes_basis,
                "trained_seasons": [args.train_season, args.validation_season],
                "blind_test_season": args.test_season,
                "leakage_audit": leakage_audit,
                "validation_controls": validation_controls,
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
        predicted_minutes,
        fga_y[test_mask],
        predicted_fga,
        three_pa_y[test_mask],
        predicted_3pa,
        fta_y[test_mask],
        predicted_fta,
        role_base_points,
        uncertainty_band,
        uncertainty_intervals,
    )

    print(json.dumps(report["rows"], sort_keys=True))
    print(json.dumps(report["validation_leaderboard"], sort_keys=True))
    print(json.dumps(report["blind_test"], sort_keys=True))
    print(
        "minutes_blind_test="
        + json.dumps(report["auxiliary_targets"]["minutes"]["blind_test"], sort_keys=True)
    )
    print(
        "derived_attempts_blind_test="
        + json.dumps(report["auxiliary_targets"]["derived_attempts"], sort_keys=True)
    )
    print("role_base_blind_test=" + json.dumps(report["role_base_points"], sort_keys=True))
    print("uncertainty_blind_test=" + json.dumps(report["blind_test_uncertainty"], sort_keys=True))
    print(f"selected_model={json.dumps(report['selected_model'], sort_keys=True)}")
    print(f"selected_params={json.dumps(selected['params'], sort_keys=True)}")
    print(f"validation_controls={json.dumps(validation_controls, sort_keys=True)}")
    print(f"features={len(feature_names)}")
    if validation_controls["status"] == "fail":
        print("MODEL VALIDATION CONTROLS FAILED: candidate will not be registered.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
