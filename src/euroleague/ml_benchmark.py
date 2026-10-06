"""Fixed model-family benchmark for player-points training.

The grids in this module are deliberately small and fixed before the blind
season is scored. E2024 validation chooses both the family and its parameters;
E2025 is reserved for one final blind evaluation of that locked winner.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import version as distribution_version
from typing import Any

MODEL_FAMILIES = (
    "ridge",
    "extra_trees",
    "hist_gradient_boosting",
    "xgboost",
    "catboost",
    "lightgbm",
)


@dataclass(frozen=True)
class ModelCandidate:
    """One pre-declared validation candidate."""

    candidate_id: str
    family: str
    params: dict[str, Any]


@dataclass(frozen=True)
class ModelDefinition:
    """Static registry metadata for one supported model family."""

    family: str
    framework: str
    distribution: str
    model_class: str


_MODEL_DEFINITIONS = {
    "ridge": ModelDefinition(
        family="ridge",
        framework="scikit-learn",
        distribution="scikit-learn",
        model_class="Pipeline[SimpleImputer,StandardScaler,Ridge]",
    ),
    "extra_trees": ModelDefinition(
        family="extra_trees",
        framework="scikit-learn",
        distribution="scikit-learn",
        model_class="Pipeline[SimpleImputer,ExtraTreesRegressor]",
    ),
    "hist_gradient_boosting": ModelDefinition(
        family="hist_gradient_boosting",
        framework="scikit-learn",
        distribution="scikit-learn",
        model_class="HistGradientBoostingRegressor",
    ),
    "xgboost": ModelDefinition(
        family="xgboost",
        framework="xgboost",
        distribution="xgboost",
        model_class="XGBRegressor",
    ),
    "catboost": ModelDefinition(
        family="catboost",
        framework="catboost",
        distribution="catboost",
        model_class="CatBoostRegressor",
    ),
    "lightgbm": ModelDefinition(
        family="lightgbm",
        framework="lightgbm",
        distribution="lightgbm",
        model_class="LGBMRegressor",
    ),
}


def model_definition(family: str) -> ModelDefinition:
    """Return static metadata, rejecting unknown model families."""
    try:
        return _MODEL_DEFINITIONS[family]
    except KeyError as error:
        raise ValueError(f"Unsupported model family: {family!r}.") from error


def runtime_model_identity(family: str) -> dict[str, str]:
    """Return registry-ready identity including the installed package version."""
    definition = model_definition(family)
    return {
        "family": definition.family,
        "framework": definition.framework,
        "framework_version": distribution_version(definition.distribution),
        "model_class": definition.model_class,
    }


def candidate_specs() -> tuple[ModelCandidate, ...]:
    """Return the fixed, ordered validation grid for all six model families."""
    return (
        ModelCandidate(
            "ridge_1",
            "ridge",
            {"alpha": 0.1},
        ),
        ModelCandidate(
            "ridge_2",
            "ridge",
            {"alpha": 1.0},
        ),
        ModelCandidate(
            "ridge_3",
            "ridge",
            {"alpha": 10.0},
        ),
        ModelCandidate(
            "ridge_4",
            "ridge",
            {"alpha": 50.0},
        ),
        ModelCandidate(
            "extra_1",
            "extra_trees",
            {
                "n_estimators": 250,
                "max_depth": 10,
                "min_samples_leaf": 3,
                "max_features": 0.7,
            },
        ),
        ModelCandidate(
            "extra_2",
            "extra_trees",
            {
                "n_estimators": 350,
                "max_depth": 14,
                "min_samples_leaf": 4,
                "max_features": 0.8,
            },
        ),
        ModelCandidate(
            "extra_3",
            "extra_trees",
            {
                "n_estimators": 250,
                "max_depth": None,
                "min_samples_leaf": 5,
                "max_features": 0.6,
            },
        ),
        ModelCandidate(
            "extra_4",
            "extra_trees",
            {
                "n_estimators": 400,
                "max_depth": 12,
                "min_samples_leaf": 2,
                "max_features": 1.0,
            },
        ),
        ModelCandidate(
            "hist_1",
            "hist_gradient_boosting",
            {
                "learning_rate": 0.05,
                "max_iter": 300,
                "max_leaf_nodes": 15,
                "min_samples_leaf": 20,
                "l2_regularization": 1.0,
            },
        ),
        ModelCandidate(
            "hist_2",
            "hist_gradient_boosting",
            {
                "learning_rate": 0.04,
                "max_iter": 400,
                "max_leaf_nodes": 31,
                "min_samples_leaf": 25,
                "l2_regularization": 2.0,
            },
        ),
        ModelCandidate(
            "hist_3",
            "hist_gradient_boosting",
            {
                "learning_rate": 0.06,
                "max_iter": 250,
                "max_leaf_nodes": 10,
                "min_samples_leaf": 15,
                "l2_regularization": 0.5,
            },
        ),
        ModelCandidate(
            "hist_4",
            "hist_gradient_boosting",
            {
                "learning_rate": 0.035,
                "max_iter": 450,
                "max_leaf_nodes": 20,
                "min_samples_leaf": 35,
                "l2_regularization": 3.0,
            },
        ),
        ModelCandidate(
            "xgb_1",
            "xgboost",
            {
                "n_estimators": 400,
                "learning_rate": 0.04,
                "max_depth": 3,
                "min_child_weight": 5.0,
                "subsample": 0.9,
                "colsample_bytree": 0.9,
                "reg_lambda": 2.0,
                "reg_alpha": 0.0,
            },
        ),
        ModelCandidate(
            "xgb_2",
            "xgboost",
            {
                "n_estimators": 550,
                "learning_rate": 0.03,
                "max_depth": 4,
                "min_child_weight": 8.0,
                "subsample": 0.85,
                "colsample_bytree": 0.9,
                "reg_lambda": 3.0,
                "reg_alpha": 0.05,
            },
        ),
        ModelCandidate(
            "xgb_3",
            "xgboost",
            {
                "n_estimators": 300,
                "learning_rate": 0.055,
                "max_depth": 2,
                "min_child_weight": 4.0,
                "subsample": 0.9,
                "colsample_bytree": 1.0,
                "reg_lambda": 1.5,
                "reg_alpha": 0.0,
            },
        ),
        ModelCandidate(
            "xgb_4",
            "xgboost",
            {
                "n_estimators": 450,
                "learning_rate": 0.035,
                "max_depth": 5,
                "min_child_weight": 10.0,
                "subsample": 0.85,
                "colsample_bytree": 0.8,
                "reg_lambda": 4.0,
                "reg_alpha": 0.1,
            },
        ),
        ModelCandidate(
            "cat_1",
            "catboost",
            {
                "iterations": 450,
                "learning_rate": 0.04,
                "depth": 5,
                "l2_leaf_reg": 5.0,
                "random_strength": 1.0,
            },
        ),
        ModelCandidate(
            "cat_2",
            "catboost",
            {
                "iterations": 600,
                "learning_rate": 0.03,
                "depth": 6,
                "l2_leaf_reg": 7.0,
                "random_strength": 1.0,
            },
        ),
        ModelCandidate(
            "cat_3",
            "catboost",
            {
                "iterations": 320,
                "learning_rate": 0.055,
                "depth": 4,
                "l2_leaf_reg": 3.0,
                "random_strength": 0.5,
            },
        ),
        ModelCandidate(
            "cat_4",
            "catboost",
            {
                "iterations": 500,
                "learning_rate": 0.03,
                "depth": 7,
                "l2_leaf_reg": 9.0,
                "random_strength": 1.5,
            },
        ),
        ModelCandidate(
            "lgbm_1",
            "lightgbm",
            {
                "n_estimators": 450,
                "learning_rate": 0.04,
                "num_leaves": 15,
                "min_child_samples": 25,
                "reg_lambda": 2.0,
                "reg_alpha": 0.0,
            },
        ),
        ModelCandidate(
            "lgbm_2",
            "lightgbm",
            {
                "n_estimators": 600,
                "learning_rate": 0.03,
                "num_leaves": 31,
                "min_child_samples": 30,
                "reg_lambda": 3.0,
                "reg_alpha": 0.05,
            },
        ),
        ModelCandidate(
            "lgbm_3",
            "lightgbm",
            {
                "n_estimators": 320,
                "learning_rate": 0.055,
                "num_leaves": 10,
                "min_child_samples": 20,
                "reg_lambda": 1.0,
                "reg_alpha": 0.0,
            },
        ),
        ModelCandidate(
            "lgbm_4",
            "lightgbm",
            {
                "n_estimators": 500,
                "learning_rate": 0.035,
                "num_leaves": 20,
                "min_child_samples": 35,
                "reg_lambda": 4.0,
                "reg_alpha": 0.1,
            },
        ),
    )


def auxiliary_candidate_specs() -> tuple[ModelCandidate, ...]:
    """Return a compact one-per-family grid for auxiliary role targets.

    The final points residual keeps the full validation grid. Auxiliary targets
    only need a stable family representative, so re-running all 24 candidates
    for minutes/FGA/3PA/FTA wastes most of the training time.
    """
    keep_ids = {
        "ridge_3",
        "extra_2",
        "hist_2",
        "xgb_2",
        "cat_2",
        "lgbm_2",
    }
    return tuple(spec for spec in candidate_specs() if spec.candidate_id in keep_ids)


def build_model(family: str, params: dict[str, Any]) -> Any:
    """Construct one estimator; heavy ML libraries are imported only for training."""
    if family == "ridge":
        pipeline = import_module("sklearn.pipeline").Pipeline
        imputer = import_module("sklearn.impute").SimpleImputer
        scaler = import_module("sklearn.preprocessing").StandardScaler
        ridge = import_module("sklearn.linear_model").Ridge
        return pipeline(
            [
                ("imputer", imputer(strategy="median", add_indicator=True)),
                ("scaler", scaler()),
                ("model", ridge(**params)),
            ]
        )

    if family == "extra_trees":
        pipeline = import_module("sklearn.pipeline").Pipeline
        imputer = import_module("sklearn.impute").SimpleImputer
        estimator = import_module("sklearn.ensemble").ExtraTreesRegressor
        return pipeline(
            [
                ("imputer", imputer(strategy="median", add_indicator=True)),
                (
                    "model",
                    estimator(
                        random_state=42,
                        n_jobs=4,
                        **params,
                    ),
                ),
            ]
        )

    if family == "hist_gradient_boosting":
        estimator = import_module("sklearn.ensemble").HistGradientBoostingRegressor
        return estimator(
            loss="squared_error",
            random_state=42,
            early_stopping=False,
            **params,
        )

    if family == "xgboost":
        estimator = import_module("xgboost").XGBRegressor
        return estimator(
            objective="reg:squarederror",
            tree_method="hist",
            random_state=42,
            n_jobs=4,
            verbosity=0,
            **params,
        )

    if family == "catboost":
        estimator = import_module("catboost").CatBoostRegressor
        return estimator(
            loss_function="RMSE",
            random_seed=42,
            verbose=False,
            allow_writing_files=False,
            thread_count=4,
            **params,
        )

    if family == "lightgbm":
        estimator = import_module("lightgbm").LGBMRegressor
        return estimator(
            objective="regression",
            random_state=42,
            n_jobs=4,
            verbosity=-1,
            **params,
        )

    raise ValueError(f"Unsupported model family: {family!r}.")
