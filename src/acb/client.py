"""Current ACB / Liga Endesa API client used by live.acb.com."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import requests

SEASONDATA_BASE = "https://api2.acb.com/api/seasondata"
MATCHDATA_BASE = "https://api2.acb.com/api/matchdata"
COMPETITION_ID = 1
MAX_CONSECUTIVE_WEEK_GAPS = 80
MAX_TOTAL_WEEKS_WALKED = 300

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
    pass


@dataclass
class ACBClient:
    api_key: str | None = None
    timeout: int = 20

    def _key(self) -> str:
        key = (self.api_key or os.getenv("ACB_API_KEY") or "").strip()
        if not key:
            raise ACBAPIError(
                "ACB_API_KEY is not configured. Use the X-Apikey value from live.acb.com."
            )
        return key

    def _get(self, base: str, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{base}/{path.lstrip('/')}"
        headers = {
            "X-Apikey": self._key(),
            "Accept": "application/json",
            "Referer": "https://live.acb.com/",
            "Origin": "https://live.acb.com",
            "User-Agent": "euroleague-analytics-acb/2.1",
        }

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                r = requests.get(
                    url,
                    params=params or {},
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(min(2**attempt, 2))
                    continue
                raise ACBAPIError(f"ACB API request failed after retries: {exc}") from exc

            if r.status_code == 400:
                raise ValueError(r.text[:500])
            if r.status_code == 200:
                return r.json()

            if r.status_code == 429 or 500 <= r.status_code <= 599:
                last_error = ACBAPIError(f"ACB API HTTP {r.status_code}: {r.text[:500]}")
                if attempt < 2:
                    retry_after = r.headers.get("Retry-After")
                    try:
                        wait = float(retry_after) if retry_after else min(2**attempt, 2)
                    except ValueError:
                        wait = min(2**attempt, 2)
                    time.sleep(wait)
                    continue

            raise ACBAPIError(f"ACB API HTTP {r.status_code}: {r.text[:500]}")

        raise ACBAPIError(f"ACB API failed after retries: {last_error}")

    @staticmethod
    def edition_id(season: str) -> int:
        season = season.strip().replace("/", "-")
        if season not in SEASON_EDITIONS:
            raise ValueError(f"Unsupported ACB season {season!r}")
        return SEASON_EDITIONS[season]

    @staticmethod
    def _start_year(season: str) -> int:
        return int(season.strip().replace("/", "-").split("-", 1)[0])

    def _matches_page(self, season: str, week_id: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "competitionId": COMPETITION_ID,
            "editionId": self.edition_id(season),
            "isRoundSelected": "false",
        }
        if week_id is not None:
            params["weekId"] = week_id
        data = self._get(SEASONDATA_BASE, "Competition/matches", params)
        if not isinstance(data, dict):
            raise ACBAPIError("Unexpected Competition/matches response")
        return data

    def season_matches(self, season: str) -> list[dict[str, Any]]:
        """Walk weekIds backwards because ACB week ids are not contiguous."""
        start = self._start_year(season)
        date_min, date_max = f"{start}-07-01", f"{start + 1}-08-31"

        anchor = self._matches_page(season)
        week_id = (anchor.get("selectedFilters") or {}).get("week")
        if week_id is None:
            raise ACBAPIError("No selectedFilters.week in ACB calendar response")

        found: dict[str, dict[str, Any]] = {}
        anchor_week = int(week_id)
        week_ids = list(range(anchor_week, anchor_week - MAX_TOTAL_WEEKS_WALKED, -1))
        workers = max(1, min(int(os.getenv("ACB_CALENDAR_WORKERS", "6")), 10))
        ok_pages = 0
        server_errors: list[str] = []

        def fetch_week(wid: int) -> tuple[int, dict[str, Any] | None, str | None]:
            try:
                return wid, self._matches_page(season, wid), None
            except ValueError:
                return wid, None, None
            except ACBAPIError as exc:
                return wid, None, str(exc)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(fetch_week, wid) for wid in week_ids]
            for future in as_completed(futures):
                _, page, error = future.result()
                if error:
                    server_errors.append(error)
                    continue
                if page is None:
                    continue
                ok_pages += 1
                for match in page.get("matches", []):
                    if not isinstance(match, dict):
                        continue
                    match_id = match.get("id")
                    match_date = str(match.get("startDateTime") or "")[:10]
                    status = str(match.get("matchStatus") or "").upper()
                    if match_id is None or not (date_min <= match_date <= date_max):
                        continue
                    if status != "FINALIZED":
                        continue
                    found[str(match_id)] = match

        if ok_pages == 0 and server_errors:
            raise ACBAPIError("ACB calendar unavailable: all reachable weeks failed")

        return sorted(
            found.values(), key=lambda x: (str(x.get("startDateTime") or ""), int(x.get("id") or 0))
        )

    def boxscore(self, season: str, match_id: int) -> Any:
        del season
        return self._get(MATCHDATA_BASE, "Result/boxscores", {"matchId": match_id})

    def play_by_play(self, match_id: int) -> Any:
        return self._get(MATCHDATA_BASE, "PlayByPlay/play-by-play", {"matchId": match_id})

    def shots(self, match_id: int) -> Any:
        return self._get(MATCHDATA_BASE, "MatchShots/match-shots", {"matchId": match_id})

    def advanced_stats(self, match_id: int) -> Any:
        return self._get(
            MATCHDATA_BASE, "AdvancedStats/match-advanced-stats", {"matchId": match_id}
        )
