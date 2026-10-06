from __future__ import annotations

import pytest

from euroleague.role_projection import role_base_projection


def test_role_base_projection_follows_minutes_attempts_efficiency_chain() -> None:
    result = role_base_projection(
        predicted_minutes=30.0,
        predicted_fga_per_minute=11.0 / 30.0,
        predicted_three_share=5.0 / 11.0,
        predicted_fta_per_minute=4.0 / 30.0,
        two_pct=0.50,
        three_pct=0.38,
        ft_pct=0.88,
    )

    assert result.minutes == 30.0
    assert result.fga == pytest.approx(11.0)
    assert result.three_pa == pytest.approx(5.0)
    assert result.two_pa == pytest.approx(6.0)
    assert result.fta == pytest.approx(4.0)
    assert result.points == pytest.approx(15.22)


def test_role_base_projection_clips_impossible_auxiliary_outputs() -> None:
    result = role_base_projection(
        predicted_minutes=55.0,
        predicted_fga_per_minute=-0.2,
        predicted_three_share=1.4,
        predicted_fta_per_minute=-1.0,
        two_pct=1.2,
        three_pct=-0.1,
        ft_pct=2.0,
    )

    assert result.minutes == 50.0
    assert result.fga == 0.0
    assert result.three_share == 1.0
    assert result.fta == 0.0
    assert result.two_pct == 1.0
    assert result.three_pct == 0.0
    assert result.ft_pct == 1.0
    assert result.points == 0.0
