"""Upload one trained model artifact and register it as a candidate."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import psycopg

from euroleague.config import DatabaseSettings, StorageSettings
from euroleague.model_registry import (
    MODEL_ARTIFACT_BUCKET,
    ModelArtifactStorage,
    artifact_path,
    file_sha256,
    register_candidate,
    validate_model_identity,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Persist one trained model in private Storage and model_registry."
    )
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--family", default="player_points")
    parser.add_argument("--version", required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--artifact-bucket", default=MODEL_ARTIFACT_BUCKET)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    validate_model_identity(args.family, args.version, args.git_commit)

    report = json.loads(args.report.read_text(encoding="utf-8"))
    model_bytes = args.model.read_bytes()
    checksum = file_sha256(args.model)
    path = artifact_path(args.family, args.version)
    selected_model = report["selected_model"]

    storage_settings = replace(
        StorageSettings.from_env(),
        bucket=args.artifact_bucket,
    )
    storage = ModelArtifactStorage(storage_settings)
    storage.assert_private_bucket()
    storage.upload_immutable(path, model_bytes, checksum)

    split = report["split"]
    metrics = {
        "blind_test": report["blind_test"],
        "naive_points_baseline": report.get("naive_points_baseline"),
        "role_base_points": report.get("role_base_points"),
        "l10_points_baseline": report["l10_points_baseline"],
        "mae_improvement_role_base_vs_naive": report.get(
            "mae_improvement_role_base_vs_naive"
        ),
        "mae_improvement_vs_naive": report.get("mae_improvement_vs_naive"),
        "mae_improvement_vs_l10": report["mae_improvement_vs_l10"],
    }
    training_metadata = {
        "split": split,
        "minutes_basis": report["minutes_basis"],
        "min_history_games": report["min_history_games"],
        "rows": report["rows"],
        "feature_count": report["feature_count"],
        "leakage_audit": report["leakage_audit"],
        "candidate_results": report["candidate_results"],
        "validation_leaderboard": report["validation_leaderboard"],
        "selected_model": selected_model,
        "prediction_architecture": report.get("prediction_architecture", {}),
        "efficiency_priors": report.get("efficiency_priors", {}),
        "auxiliary_targets": report.get("auxiliary_targets", {}),
    }

    database_settings = DatabaseSettings.from_env()
    with psycopg.connect(
        database_settings.url(),
        autocommit=True,
        prepare_threshold=None,
    ) as connection:
        status = register_candidate(
            connection,
            model_family=args.family,
            version=args.version,
            artifact_bucket=args.artifact_bucket,
            artifact_path_value=path,
            artifact_sha256=checksum,
            artifact_size_bytes=len(model_bytes),
            git_commit=args.git_commit,
            framework=selected_model["framework"],
            framework_version=selected_model["framework_version"],
            model_class=selected_model["model_class"],
            trained_at=report["created_at"],
            train_seasons=list(split["final_train"]),
            validation_seasons=list(split["validation"]),
            blind_test_seasons=list(split["blind_test"]),
            feature_names=list(report["features"]),
            selected_params=dict(report["selected_params"]),
            metrics=metrics,
            training_metadata=training_metadata,
        )

    print(
        json.dumps(
            {
                "model_family": args.family,
                "version": args.version,
                "status": status,
                "artifact_bucket": args.artifact_bucket,
                "artifact_path": path,
                "artifact_sha256": checksum,
                "artifact_size_bytes": len(model_bytes),
                "estimator_family": selected_model["family"],
                "model_class": selected_model["model_class"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
