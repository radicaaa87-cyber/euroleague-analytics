"""Selecting and loading the games a live season has newly played.

The fetcher and the loader already agree on what "played" means. What nothing
owned until now is the second question a live season asks every day: of the
games the schedule marks played, which ones is the warehouse missing?

Getting that wrong has two failure shapes and they are not symmetrical. Missing
a new game is visible - the season is short and somebody notices. Re-selecting
a game the warehouse already holds is not visible: the raw loader would replace
rows that derived rows were built from, and the guard that refuses it is the
only thing standing between a live season and silently invalidated lineups.

These tests fix the selection rule before the pipeline that depends on it.
"""

from __future__ import annotations

import json

import pytest

import euroleague.live as live_module
from euroleague.live import (
    LiveRunSummary,
    assert_new_games_cached,
    assert_new_games_safe,
    run_live_pipeline,
    select_new_games,
)
from euroleague.load import DerivedRowsExistError


def _game(gamecode: int, *, played: bool = True) -> dict:
    return {"gameCode": gamecode, "played": played}


# ---------------------------------------------------------------------------
# Which games are new
# ---------------------------------------------------------------------------


def test_only_played_games_the_warehouse_lacks_are_selected() -> None:
    """Break caught: the daily run re-loads a game it already holds."""
    schedule = [_game(1), _game(2), _game(3)]

    assert [int(game["gameCode"]) for game in select_new_games(schedule, {1})] == [2, 3]


def test_an_unplayed_game_is_never_selected() -> None:
    """Break caught: a future fixture is treated as a gap and hunted in the cache."""
    schedule = [_game(1), _game(2, played=False)]

    assert [int(game["gameCode"]) for game in select_new_games(schedule, set())] == [1]


def test_a_game_without_a_played_key_is_not_assumed_played() -> None:
    """Break caught: the live selector is more generous than the fetcher."""
    assert select_new_games([{"gameCode": 7}], set()) == []


def test_the_played_flag_is_matched_strictly_like_the_fetcher_matches_it() -> None:
    """Break caught: a truthy string starts a fetch for a game nobody played."""
    for truthy in ("true", "True", 1, "1"):
        assert select_new_games([{"gameCode": 4, "played": truthy}], set()) == []


def test_selection_is_in_gamecode_order() -> None:
    """Break caught: games load in schedule order, so a failure is unreproducible."""
    schedule = [_game(30), _game(4), _game(17)]

    assert [int(game["gameCode"]) for game in select_new_games(schedule, set())] == [4, 17, 30]


def test_a_season_with_nothing_new_selects_nothing() -> None:
    """Break caught: an ordinary quiet day is treated as an error."""
    assert select_new_games([_game(1), _game(2)], {1, 2}) == []


def test_a_season_with_nothing_played_yet_selects_nothing() -> None:
    """Break caught: E2026 before 2026-09-24 cannot flow through the pipeline."""
    assert select_new_games([_game(n, played=False) for n in range(1, 381)], set()) == []


def test_zero_game_live_run_loads_schedule_dimensions_and_cached_rosters(
    monkeypatch, tmp_path
) -> None:
    """Break caught: pre-season succeeds while persisting no teams or roster rows."""
    from euroleague.cache import ResponseCache
    from euroleague.derived import DimensionRows

    cache = ResponseCache(tmp_path)
    season_dir = tmp_path / "E2026"
    season_dir.mkdir(parents=True)
    cache.schedule_path("E2026").write_text(
        json.dumps({"data": [_game(code, played=False) for code in range(1, 381)]}),
        encoding="utf-8",
    )
    cache.roster_path("E2026").write_text('{"data":[],"total":0}', encoding="utf-8")
    calls: list[str] = []

    monkeypatch.setattr(live_module, "loaded_gamecodes", lambda connection, season: set())
    monkeypatch.setattr(
        live_module,
        "build_dimensions",
        lambda consumed_cache, season: DimensionRows(
            (), (("AAA",),), ((season, "AAA", "E", "Alpha"),)
        ),
    )
    monkeypatch.setattr(
        live_module,
        "load_dimensions",
        lambda connection, dimensions, season: calls.append("dimensions") or {},
    )
    monkeypatch.setattr(
        live_module,
        "load_cached_roster",
        lambda connection, consumed_cache, season: calls.append("roster") or 203,
    )
    monkeypatch.setattr(live_module, "sync_season_schedule", lambda *args: 380)
    monkeypatch.setattr(live_module, "record_season_progress", lambda *args: None)

    summary = run_live_pipeline(object(), cache, "E2026", progress=lambda line: None)

    assert calls == ["dimensions", "roster"]
    assert summary.played == 0
    assert summary.roster_registrations == 203
    assert "roster_registrations=203" in summary.as_log_line()


def test_a_warehouse_holding_a_game_the_schedule_no_longer_marks_played_is_left_alone() -> None:
    """Break caught: a schedule revision silently deletes a loaded game."""
    # The selector adds; it never removes. A game vanishing from the played
    # list is a source revision, which Decision 7 handles per game and which
    # this daily path must not quietly act on.
    assert select_new_games([_game(1, played=False)], {1}) == []


# ---------------------------------------------------------------------------
# The cache must hold every selected game before anything is written
# ---------------------------------------------------------------------------


def test_a_selected_game_missing_from_the_cache_is_a_hard_failure(fixture_cache) -> None:
    """Break caught: a played game with no response loads as an empty game."""
    with pytest.raises(FileNotFoundError) as failure:
        assert_new_games_cached(fixture_cache, "E2024", [_game(999_999)])

    assert "999999" in str(failure.value)


def test_cache_completeness_is_checked_for_every_game_before_any_is_loaded(
    fixture_cache, fixture_gamecodes
) -> None:
    """Break caught: game 9 is found missing after games 1-8 are already written."""
    good = fixture_gamecodes[0]

    with pytest.raises(FileNotFoundError) as failure:
        assert_new_games_cached(fixture_cache, "E2024", [_game(good), _game(999_999)])

    # The message must name the missing game, not merely count them, or the
    # operator has to go looking for which one it was.
    assert "999999" in str(failure.value)


def test_a_fully_cached_selection_passes_the_check(live_cache, fixture_gamecodes) -> None:
    """Break caught: the completeness check rejects a cache that is in fact complete."""
    cache = live_cache("E2024", staged=fixture_gamecodes, played=fixture_gamecodes)

    assert_new_games_cached(cache, "E2024", [_game(code) for code in fixture_gamecodes])


# ---------------------------------------------------------------------------
# The derived-rows guard stays scoped to the games being written
# ---------------------------------------------------------------------------


def test_loading_refuses_a_selected_game_that_already_has_derived_rows(loader_connection) -> None:
    """Break caught: the add path becomes an undocumented replacement path."""
    connection = loader_connection(derived_rows=3)

    with pytest.raises(DerivedRowsExistError):
        assert_new_games_safe(connection, "E2026", [11, 12])


def test_loading_allows_new_games_beside_games_that_already_have_derived_rows(
    loader_connection,
) -> None:
    """Break caught: week two refuses because week one was derived."""
    connection = loader_connection(derived_rows=0)

    assert_new_games_safe(connection, "E2026", [11, 12])

    # The guard must ask about the selected games only. A season-wide question
    # is always yes after the first week and would stop the season dead.
    asked = [query for query, _ in connection.executions if "count(*)" in query]
    assert asked, "the guard must actually query the database"
    assert all("gamecode = any(" in query for query in asked)


def test_an_empty_selection_asks_the_database_nothing(loader_connection) -> None:
    """Break caught: a quiet day still opens a transaction and vacuums."""
    connection = loader_connection(derived_rows=99)

    assert_new_games_safe(connection, "E2026", [])

    assert connection.executions == []


# ---------------------------------------------------------------------------
# The run summary is what the workflow log shows a human
# ---------------------------------------------------------------------------


def test_a_summary_with_no_new_games_reports_zero_rather_than_staying_silent() -> None:
    """Break caught: a run that did nothing looks identical to a run that worked."""
    summary = LiveRunSummary(
        season_code="E2026", scheduled=380, played=0, already_loaded=0, newly_loaded=()
    )

    line = summary.as_log_line()
    assert "scheduled=380" in line
    assert "played=0" in line
    assert "new=0" in line


def test_a_summary_names_the_games_it_loaded() -> None:
    """Break caught: the log says how many games loaded but never which."""
    summary = LiveRunSummary(
        season_code="E2026", scheduled=380, played=12, already_loaded=10, newly_loaded=(11, 12)
    )

    line = summary.as_log_line()
    assert "new=2" in line
    assert "11" in line and "12" in line


def test_a_summary_never_carries_a_credential() -> None:
    """Break caught: a diagnostic line prints the connection string into a public log."""
    summary = LiveRunSummary(
        season_code="E2026", scheduled=380, played=1, already_loaded=0, newly_loaded=(1,)
    )

    assert "://" not in summary.as_log_line()


# ---------------------------------------------------------------------------
# A live schedule contains games that have not happened yet
# ---------------------------------------------------------------------------


def test_dimensions_build_from_a_schedule_holding_unplayed_games(
    live_cache, fixture_gamecodes
) -> None:
    """Break caught: the derived build reads a Boxscore for a game nobody played.

    This is the shape every E2026 schedule has from 2026-09-24: 380 games
    listed, a handful played. A builder that walks the schedule rather than the
    played games dies on the first unplayed fixture, and it dies on day one.
    """
    from euroleague.derived import build_dimensions

    played = fixture_gamecodes[:3]
    unplayed = fixture_gamecodes[3:6]
    cache = live_cache("E2024", staged=played, played=played)
    # The schedule lists the unplayed games too, and their responses are absent
    # from the cache because nobody has played them.
    schedule_path = cache.schedule_path("E2024")
    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    schedule["data"].extend({"gameCode": code, "played": False} for code in unplayed)
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")

    dimensions = build_dimensions(cache, "E2024")

    assert dimensions.players, "the build produced no players at all"


def test_a_successful_live_load_records_the_exact_consumed_source_versions(monkeypatch) -> None:
    """Break caught: a newly loaded game has no durable applied-source marker."""
    cache = type(
        "Cache",
        (),
        {"read_schedule_json": lambda self, season: {"data": [{"gameCode": 7, "played": True}]}},
    )()
    recorded: list[tuple[object, object, str, tuple[int, ...]]] = []
    connection = object()

    monkeypatch.setattr(live_module, "loaded_gamecodes", lambda connection, season: set())
    monkeypatch.setattr(live_module, "sync_season_schedule", lambda *args: 1)
    monkeypatch.setattr(live_module, "record_season_progress", lambda *args: None)
    monkeypatch.setattr(live_module, "load_new_raw_games", lambda *args, **kwargs: {})
    monkeypatch.setattr(live_module, "derive_new_games", lambda *args, **kwargs: {})
    monkeypatch.setattr(live_module, "assert_live_games_gated", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        live_module,
        "record_cached_game_sources",
        lambda conn, consumed_cache, season, gamecodes: recorded.append(
            (conn, consumed_cache, season, tuple(gamecodes))
        ),
    )

    run_live_pipeline(connection, cache, "E2026", progress=lambda line: None)

    assert recorded == [(connection, cache, "E2026", (7,))]


def test_a_new_live_game_consumes_points_before_its_source_marker_can_advance(
    monkeypatch,
) -> None:
    """Break caught: Points is marked applied even though raw_shot stayed empty."""
    calls: list[tuple] = []
    schedule_game = {
        "gameCode": 7,
        "played": True,
        "season": {"competitionCode": "E"},
    }
    cache = type(
        "Cache",
        (),
        {"read_json": lambda self, season, endpoint, gamecode: {"points": "body"}},
    )()

    monkeypatch.setattr(live_module, "assert_new_games_cached", lambda *args: None)
    monkeypatch.setattr(live_module, "assert_new_games_safe", lambda *args: None)
    parsed = type(
        "Parsed",
        (),
        {"game": type("Game", (), {"competition_code": "E"})()},
    )()
    monkeypatch.setattr(live_module, "parse_cached_game", lambda *args: parsed)
    monkeypatch.setattr(
        live_module,
        "load_game",
        lambda *args: {"events_parsed": 4, "raw_boxscore_player": 10},
    )
    monkeypatch.setattr(
        live_module,
        "parse_shots",
        lambda season, gamecode, competition, payload: (
            calls.append((season, gamecode, competition, payload)) or ("shot",)
        ),
    )
    monkeypatch.setattr(
        live_module,
        "load_shots_for_game",
        lambda connection, season, gamecode, shots: len(shots),
    )

    counts = live_module.load_new_raw_games(
        object(), cache, "E2026", [schedule_game], progress=lambda line: None
    )

    assert counts["raw_shot"] == 1
    assert calls == [("E2026", 7, "E", {"points": "body"})]
