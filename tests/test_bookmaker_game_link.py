"""Tests for conservative bookmaker offer -> EuroLeague game linking."""

from __future__ import annotations

from datetime import date

from euroleague.bookmaker_game_link import HistoricalGameCandidate, match_offer_to_game


def test_exact_date_unique_candidate_links() -> None:
    candidates = [
        HistoricalGameCandidate(
            athlete_id="a1",
            season_code="E2024",
            gamecode=77,
            game_date=date(2024, 10, 16),
            team_code="BAR",
        )
    ]

    match = match_offer_to_game(
        athlete_id="a1",
        offer_date=date(2024, 10, 16),
        candidates=candidates,
    )

    assert match is not None
    assert match.season_code == "E2024"
    assert match.gamecode == 77
    assert match.method == "athlete_exact_date"
    assert match.confidence == 1.0


def test_same_athlete_wrong_date_does_not_link() -> None:
    candidates = [
        HistoricalGameCandidate(
            athlete_id="a1",
            season_code="E2024",
            gamecode=77,
            game_date=date(2024, 10, 16),
            team_code="BAR",
        )
    ]

    assert (
        match_offer_to_game(
            athlete_id="a1",
            offer_date=date(2024, 10, 17),
            candidates=candidates,
        )
        is None
    )


def test_ambiguous_exact_date_does_not_link() -> None:
    candidates = [
        HistoricalGameCandidate(
            athlete_id="a1",
            season_code="E2024",
            gamecode=77,
            game_date=date(2024, 10, 16),
            team_code="BAR",
        ),
        HistoricalGameCandidate(
            athlete_id="a1",
            season_code="E2024",
            gamecode=78,
            game_date=date(2024, 10, 16),
            team_code="BAR",
        ),
    ]

    assert (
        match_offer_to_game(
            athlete_id="a1",
            offer_date=date(2024, 10, 16),
            candidates=candidates,
        )
        is None
    )
