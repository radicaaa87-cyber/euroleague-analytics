"""Official ACB / Liga Endesa data client.

Uses ACB's open live API (api2.acb.com). The bearer token must be supplied
through the ACB_BEARER_TOKEN environment variable and is never committed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests

BASE_URL = "https://api2.acb.com/api/v1/openapilive"
COMPETITION_ID = 1

# Season label -> ACB edition id.
# Verified from public ACB/openacb season configuration.
SEASON_EDITIONS: dict[str, int] = {
    "2016-17": 81,
    "2017-18": 82,
    "2018-19": 83,
    "2019-20": 84,
    "2020-21": 85,
    "2021-22": 86,
    "2022-23": 87,
    "2023-24": 88,
    "2024-25": 89,
    "2025-26": 90,
    "2026-27": 91,
}


class ACBAPIError(RuntimeError):
    """Raised when the ACB API cannot be queried successfully."""


@dataclass(frozen=True)
class ACBClient:
    """Small read-only client for Liga Endesa public data."""

    bearer_token: str | None = None
    timeout: int = 30

    def _authorization(self) -> str:
        token = (self.bearer_token or os.getenv("ACB_BEARER_TOKEN") or "").strip()
        if not token:
            raise ACBAPIError(
                "ACB_BEARER_TOKEN is not configured. "
                "Set it in the environment before pulling ACB data."
            )
        # Accept either the complete Authorization value used by existing
        # ACB scrapers or a raw bearer token.
        if token.lower().startswith("bearer "):
            return token
        return f"Bearer {token}"

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = requests.get(
            f"{BASE_URL}/{path.lstrip('/')}",
            params=params or {},
            headers={
                "Authorization": self._authorization(),
                "Accept": "application/json",
                "User-Agent": "euroleague-analytics-acb/1.0",
            },
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise ACBAPIError(
                f"ACB API returned HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
        return response.json()

    @staticmethod
    def edition_id(season: str) -> int:
        normalized = season.strip().replace("/", "-")
        if normalized not in SEASON_EDITIONS:
            raise ValueError(
                f"Unsupported ACB season {season!r}. "
                f"Known seasons: {', '.join(SEASON_EDITIONS)}"
            )
        return SEASON_EDITIONS[normalized]

    def matchweeks(self, season: str) -> Any:
        """Return all Liga Endesa matchweeks for a season."""
        return self._get(
            "Matchweeks/lite",
            {
                "idCompetition": COMPETITION_ID,
                "idEdition": self.edition_id(season),
            },
        )

    def matches_by_matchweek(self, season: str, matchweek_id: int) -> Any:
        """Return Liga Endesa matches for one ACB matchweek."""
        return self._get(
            "Matches/matchesbymatchweeklite",
            {
                "idCompetition": COMPETITION_ID,
                "idEdition": self.edition_id(season),
                "idMatchweek": matchweek_id,
            },
        )

    def play_by_play(self, match_id: int) -> Any:
        """Return source-order play-by-play for one ACB match."""
        return self._get(
            "PlayByPlay/matchevents",
            {"idMatch": match_id},
        )

    def boxscore(self, season: str, match_id: int) -> Any:
        """Return official ACB player boxscore rows for one match."""
        return self._get(
            "Boxscore/playermatchstatistics",
            {
                "idCompetition": COMPETITION_ID,
                "idEdition": self.edition_id(season),
                "idMatch": match_id,
            },
        )

    def season_matches(self, season: str) -> list[dict[str, Any]]:
        """Pull the full match list for a season by walking its matchweeks."""
        weeks = self.matchweeks(season)
        week_rows = self._rows(weeks)

        matches: list[dict[str, Any]] = []
        seen: set[int | str] = set()

        for week in week_rows:
            week_id = (
                week.get("idMatchweek")
                or week.get("id_matchweek")
                or week.get("id")
            )
            if week_id is None:
                continue

            payload = self.matches_by_matchweek(season, int(week_id))
            for match in self._rows(payload):
                match_id = (
                    match.get("idMatch")
                    or match.get("id_match")
                    or match.get("id")
                )
                key: int | str = match_id if match_id is not None else repr(match)
                if key in seen:
                    continue
                seen.add(key)
                matches.append(match)

        return matches

    @staticmethod
    def _rows(payload: Any) -> list[dict[str, Any]]:
        """Normalize common ACB response envelopes into a row list."""
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]

        if not isinstance(payload, dict):
            return []

        for key in (
            "data",
            "results",
            "items",
            "matchweeks",
            "matches",
            "events",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]

            if isinstance(value, dict):
                for nested_key in ("data", "items", "results"):
                    nested = value.get(nested_key)
                    if isinstance(nested, list):
                        return [row for row in nested if isinstance(row, dict)]

        return []
