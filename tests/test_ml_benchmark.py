from __future__ import annotations

from collections import Counter

import pytest

from euroleague.ml_benchmark import (
    MODEL_FAMILIES,
    candidate_specs,
    model_definition,
)


def test_benchmark_has_four_fixed_candidates_per_family() -> None:
    specs = candidate_specs()
    counts = Counter(spec.family for spec in specs)

    assert counts == {family: 4 for family in MODEL_FAMILIES}
    assert len({spec.candidate_id for spec in specs}) == len(specs)


def test_every_candidate_uses_a_supported_family_and_nonempty_params() -> None:
    for spec in candidate_specs():
        assert spec.family in MODEL_FAMILIES
        assert spec.params


def test_registry_metadata_exists_for_every_family() -> None:
    expected_classes = {
        "hist_gradient_boosting": "HistGradientBoostingRegressor",
        "xgboost": "XGBRegressor",
        "catboost": "CatBoostRegressor",
        "lightgbm": "LGBMRegressor",
    }

    for family, expected_class in expected_classes.items():
        definition = model_definition(family)
        assert definition.family == family
        assert definition.framework
        assert definition.distribution
        assert definition.model_class == expected_class


def test_unknown_model_family_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported model family"):
        model_definition("neural_net")
