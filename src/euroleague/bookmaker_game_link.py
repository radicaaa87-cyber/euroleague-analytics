"""Conservative bookmaker offer -> historical EuroLeague game linking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable


@dataclass(frozen=True)
class HistoricalGameCandidate:
    athlete_id: str
    season_code: str
    gamecode: int
    game_date: date
    team_code: str


@dataclass(frozen=True)
class GameMatch:
    season_code: str
    gamecode: int
    team_code: str
    method: str
    confidence: float


def match_offer_to_game(
    *,
    athlete_id: str,
    offer_date: date,
    candidates: Iterable[HistoricalGameCandidate],
) -> GameMatch | None:
    """Return only an unambiguous exact-date historical game for one athlete."""
    exact = [
        candidate
        for candidate in candidates
        if candidate.athlete_id == athlete_id and candidate.game_date == offer_date
    ]
    unique_games = {
        (candidate.season_code, candidate.gamecode, candidate.team_code)
        for candidate in exact
    }
    if len(unique_games) != 1:
        return None

    season_code, gamecode, team_code = next(iter(unique_games))
    return GameMatch(
        season_code=season_code,
        gamecode=gamecode,
        team_code=team_code,
        method="athlete_exact_date",
        confidence=1.0,
    )
