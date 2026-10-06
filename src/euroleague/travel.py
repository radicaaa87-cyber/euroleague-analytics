"""Pre-game travel-load helpers for player-points modelling.

Distances are great-circle ("air") distances between the nominal home cities
associated with EuroLeague team codes. They are deliberately a conservative
travel proxy: they do not claim to reproduce an airline itinerary, hotel move,
or a temporary neutral/home venue. Unknown team codes return null instead of
being guessed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise
from math import asin, cos, radians, sin, sqrt
from typing import Any


@dataclass(frozen=True)
class TeamHomeLocation:
    city: str
    latitude: float
    longitude: float


# Warehouse team codes observed across E2024-E2026. City-centre coordinates are
# stable enough for travel-load modelling while avoiding false arena precision.
TEAM_HOME_LOCATIONS: dict[str, TeamHomeLocation] = {
    "ASV": TeamHomeLocation("Villeurbanne/Lyon", 45.7640, 4.8357),
    "BAR": TeamHomeLocation("Barcelona", 41.3874, 2.1686),
    "BAS": TeamHomeLocation("Vitoria-Gasteiz", 42.8467, -2.6727),
    "BER": TeamHomeLocation("Berlin", 52.5200, 13.4050),
    "BES": TeamHomeLocation("Istanbul", 41.0082, 28.9784),
    "DUB": TeamHomeLocation("Dubai", 25.2048, 55.2708),
    "HTA": TeamHomeLocation("Tel Aviv", 32.0853, 34.7818),
    "IST": TeamHomeLocation("Istanbul", 41.0082, 28.9784),
    "MAD": TeamHomeLocation("Madrid", 40.4168, -3.7038),
    "MCO": TeamHomeLocation("Monaco", 43.7384, 7.4246),
    "MIL": TeamHomeLocation("Milan", 45.4642, 9.1900),
    "MUN": TeamHomeLocation("Munich", 48.1351, 11.5820),
    "OLY": TeamHomeLocation("Piraeus", 37.9429, 23.6469),
    "PAM": TeamHomeLocation("Valencia", 39.4699, -0.3763),
    "PAN": TeamHomeLocation("Athens", 37.9838, 23.7275),
    "PAR": TeamHomeLocation("Belgrade", 44.7866, 20.4489),
    "PRS": TeamHomeLocation("Paris", 48.8566, 2.3522),
    "RED": TeamHomeLocation("Belgrade", 44.7866, 20.4489),
    "TEL": TeamHomeLocation("Tel Aviv", 32.0853, 34.7818),
    "ULK": TeamHomeLocation("Istanbul", 41.0082, 28.9784),
    "VIR": TeamHomeLocation("Bologna", 44.4949, 11.3426),
    "ZAL": TeamHomeLocation("Kaunas", 54.8985, 23.9036),
}


def game_venue_team_code(
    team_code: str | None,
    opponent_team_code: str | None,
    is_home: bool | None,
) -> str | None:
    """Return the team code whose nominal home city hosts the game."""
    if not team_code or not opponent_team_code or is_home is None:
        return None
    return team_code if is_home else opponent_team_code


def air_distance_km(from_team_code: str | None, to_team_code: str | None) -> float | None:
    """Great-circle distance between two nominal EuroLeague home cities."""
    if not from_team_code or not to_team_code:
        return None
    origin = TEAM_HOME_LOCATIONS.get(from_team_code)
    destination = TEAM_HOME_LOCATIONS.get(to_team_code)
    if origin is None or destination is None:
        return None
    if from_team_code == to_team_code:
        return 0.0

    earth_radius_km = 6371.0088
    lat1 = radians(origin.latitude)
    lat2 = radians(destination.latitude)
    dlat = lat2 - lat1
    dlon = radians(destination.longitude - origin.longitude)
    hav = sin(dlat / 2.0) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2.0) ** 2
    return round(2.0 * earth_radius_km * asin(sqrt(hav)), 1)


def travel_context(
    recent_games: Iterable[dict[str, Any]] | None,
    *,
    target_team_code: str | None,
    target_opponent_team_code: str | None,
    target_is_home: bool | None,
) -> dict[str, Any]:
    """Summarize the route into the target game from recent EL game venues."""
    target_venue = game_venue_team_code(
        target_team_code,
        target_opponent_team_code,
        target_is_home,
    )
    games = list(recent_games or [])
    if target_venue is None:
        return {
            "target_venue_team_code": None,
            "from_last_game_venue_team_code": None,
            "air_km_from_last_game": None,
            "air_km_last_3_legs": None,
            "legs_with_location": 0,
            "location_coverage": "missing_target",
        }

    venue_codes: list[str] = []
    for game in games[:3]:
        venue = game_venue_team_code(
            str(game.get("team")) if game.get("team") else None,
            str(game.get("opponent")) if game.get("opponent") else None,
            game.get("home") if isinstance(game.get("home"), bool) else None,
        )
        if venue is not None:
            venue_codes.append(venue)

    last_venue = venue_codes[0] if venue_codes else None
    from_last = air_distance_km(last_venue, target_venue)

    route = [*reversed(venue_codes), target_venue]
    total = 0.0
    covered = 0
    for origin, destination in pairwise(route):
        distance = air_distance_km(origin, destination)
        if distance is None:
            continue
        total += distance
        covered += 1

    target_known = target_venue in TEAM_HOME_LOCATIONS
    coverage = "complete" if target_known and covered == max(len(route) - 1, 0) else "partial"
    if not target_known:
        coverage = "unknown_target_team"

    return {
        "target_venue_team_code": target_venue,
        "from_last_game_venue_team_code": last_venue,
        "air_km_from_last_game": from_last,
        "air_km_last_3_legs": round(total, 1) if covered else None,
        "legs_with_location": covered,
        "location_coverage": coverage,
    }


def _coordinate_case_sql(team_expression: str, attribute: str) -> str:
    """Return an internal CASE expression for a known coordinate component."""
    if attribute not in {"latitude", "longitude"}:
        raise ValueError("attribute must be latitude or longitude")
    parts = ["case " + team_expression]
    for code, location in sorted(TEAM_HOME_LOCATIONS.items()):
        value = getattr(location, attribute)
        parts.append(f" when '{code}' then {value:.6f}")
    parts.append(" else null end")
    return "".join(parts)


def travel_distance_sql(from_team_expression: str, to_team_expression: str) -> str:
    """PostgreSQL expression for great-circle distance between two team codes."""
    from_lat = _coordinate_case_sql(from_team_expression, "latitude")
    from_lon = _coordinate_case_sql(from_team_expression, "longitude")
    to_lat = _coordinate_case_sql(to_team_expression, "latitude")
    to_lon = _coordinate_case_sql(to_team_expression, "longitude")
    return f"""
    case
        when ({from_lat}) is null or ({from_lon}) is null
          or ({to_lat}) is null or ({to_lon}) is null
            then null
        else 6371.0088 * 2.0 * asin(
            sqrt(
                power(sin(radians(({to_lat}) - ({from_lat})) / 2.0), 2)
                + cos(radians({from_lat}))
                * cos(radians({to_lat}))
                * power(sin(radians(({to_lon}) - ({from_lon})) / 2.0), 2)
            )
        )
    end
    """
