from __future__ import annotations

import pytest

from euroleague.travel import (
    TEAM_HOME_LOCATIONS,
    air_distance_km,
    game_venue_team_code,
    travel_context,
    travel_distance_sql,
)


def test_all_current_e2026_team_codes_have_nominal_locations() -> None:
    current_codes = {
        "ASV",
        "BAR",
        "BAS",
        "BES",
        "DUB",
        "HTA",
        "IST",
        "MAD",
        "MIL",
        "MUN",
        "OLY",
        "PAM",
        "PAN",
        "PAR",
        "PRS",
        "RED",
        "TEL",
        "ULK",
        "VIR",
        "ZAL",
    }
    assert current_codes <= set(TEAM_HOME_LOCATIONS)


def test_game_venue_uses_home_team_city() -> None:
    assert game_venue_team_code("BAR", "MAD", True) == "BAR"
    assert game_venue_team_code("BAR", "MAD", False) == "MAD"
    assert game_venue_team_code("BAR", None, False) is None


def test_air_distance_is_symmetric_and_unknown_stays_null() -> None:
    bar_to_mad = air_distance_km("BAR", "MAD")
    mad_to_bar = air_distance_km("MAD", "BAR")

    assert bar_to_mad is not None
    assert bar_to_mad == pytest.approx(mad_to_bar, abs=0.1)
    assert 490 < bar_to_mad < 520
    assert air_distance_km("BAR", "UNKNOWN") is None


def test_travel_context_follows_last_three_game_venues_into_target() -> None:
    recent_games = [
        {"team": "BAR", "opponent": "MAD", "home": False},
        {"team": "BAR", "opponent": "BAR", "home": True},
        {"team": "BAR", "opponent": "MIL", "home": False},
    ]

    result = travel_context(
        recent_games,
        target_team_code="BAR",
        target_opponent_team_code="OLY",
        target_is_home=False,
    )

    assert result["target_venue_team_code"] == "OLY"
    assert result["from_last_game_venue_team_code"] == "MAD"
    assert result["air_km_from_last_game"] is not None
    assert result["air_km_last_3_legs"] is not None
    assert result["legs_with_location"] == 3
    assert result["location_coverage"] == "complete"


def test_travel_sql_returns_null_for_unknown_codes_by_construction() -> None:
    sql = travel_distance_sql("previous_team_code", "current_team_code").lower()

    assert "6371.0088" in sql
    assert "else null end" in sql
    assert "previous_team_code" in sql
    assert "current_team_code" in sql
