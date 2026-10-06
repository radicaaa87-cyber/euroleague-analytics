from __future__ import annotations

import datetime as dt

import pytest

from euroleague.pregame_context import (
    classify_context_text,
    in_pregame_window,
)


def test_context_window_is_strictly_pregame_and_72_hours() -> None:
    tipoff = dt.datetime(2026, 10, 10, 18, 0, tzinfo=dt.UTC)

    assert in_pregame_window(tipoff - dt.timedelta(hours=72), tipoff)
    assert in_pregame_window(tipoff - dt.timedelta(minutes=1), tipoff)
    assert not in_pregame_window(tipoff, tipoff)
    assert not in_pregame_window(tipoff - dt.timedelta(hours=72, seconds=1), tipoff)


def test_context_window_requires_timezone_aware_values() -> None:
    aware = dt.datetime(2026, 10, 10, 18, 0, tzinfo=dt.UTC)
    naive = dt.datetime(2026, 10, 10, 17, 0)

    with pytest.raises(ValueError, match="timezone-aware"):
        in_pregame_window(naive, aware)


def test_injury_out_is_strong_negative_role_signal() -> None:
    result = classify_context_text(
        "Guard ruled out for Thursday EuroLeague game",
        source_name="EuroLeague Basketball",
        publisher_url="https://www.euroleaguebasketball.net/news/example",
    )

    assert result.event_type == "availability_out"
    assert result.role_direction == -1
    assert result.severity == 1.0
    assert result.source_confidence == 0.95
    assert result.role_impact_score == -0.95


def test_return_is_positive_but_weaker_than_confirmed_out() -> None:
    result = classify_context_text(
        "Player returns and is available again after injury",
        source_name="ACB",
        publisher_url="https://www.acb.com/noticia/example",
    )

    assert result.event_type == "return"
    assert result.role_direction == 1
    assert 0 < result.role_impact_score < 0.95


def test_general_article_is_not_forced_into_role_change() -> None:
    result = classify_context_text("Player previews a difficult road game")

    assert result.event_type == "other"
    assert result.role_direction == 0
    assert result.role_impact_score == 0
