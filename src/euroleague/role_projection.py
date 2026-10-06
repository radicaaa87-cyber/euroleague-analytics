"""Transparent role-based scoring projection helpers.

These helpers convert pre-game auxiliary forecasts into an interpretable points
baseline. They never inspect the target game's result.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleBaseProjection:
    """One minutes -> attempts -> efficiency projection."""

    minutes: float
    fga_per_minute: float
    fga: float
    three_share: float
    three_pa: float
    two_pa: float
    fta_per_minute: float
    fta: float
    two_pct: float
    three_pct: float
    ft_pct: float
    points: float


def _clip(value: float, lower: float, upper: float) -> float:
    return min(max(float(value), lower), upper)


def role_base_projection(
    *,
    predicted_minutes: float,
    predicted_fga_per_minute: float,
    predicted_three_share: float,
    predicted_fta_per_minute: float,
    two_pct: float,
    three_pct: float,
    ft_pct: float,
) -> RoleBaseProjection:
    """Build an interpretable scoring baseline from pre-game forecasts."""
    minutes = _clip(predicted_minutes, 0.0, 50.0)
    fga_per_minute = _clip(predicted_fga_per_minute, 0.0, 1.5)
    three_share = _clip(predicted_three_share, 0.0, 1.0)
    fta_per_minute = _clip(predicted_fta_per_minute, 0.0, 1.5)

    fga = minutes * fga_per_minute
    three_pa = fga * three_share
    two_pa = max(fga - three_pa, 0.0)
    fta = minutes * fta_per_minute

    two_pct = _clip(two_pct, 0.0, 1.0)
    three_pct = _clip(three_pct, 0.0, 1.0)
    ft_pct = _clip(ft_pct, 0.0, 1.0)

    points = (
        2.0 * two_pa * two_pct
        + 3.0 * three_pa * three_pct
        + fta * ft_pct
    )
    return RoleBaseProjection(
        minutes=minutes,
        fga_per_minute=fga_per_minute,
        fga=fga,
        three_share=three_share,
        three_pa=three_pa,
        two_pa=two_pa,
        fta_per_minute=fta_per_minute,
        fta=fta,
        two_pct=two_pct,
        three_pct=three_pct,
        ft_pct=ft_pct,
        points=points,
    )
