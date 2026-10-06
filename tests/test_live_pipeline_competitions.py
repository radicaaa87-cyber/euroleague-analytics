"""Tests proving competition metadata propagates through the offline live core.

WHAT THESE TESTS PROVE.
1. `record_season_progress` correctly propagates competition code 'SC' for SC2026,
   'U' for U2025, and 'E' for E2026 into the database upsert statement.
2. `build_dimensions` sets the derived `competition_code` on `team_season` rows.
3. `build_game_events` sets `competition_code` on `game_event` rows.
4. `build_remaining_rows` preserves `season_code` across lineups, stints,
   possessions, player-game minutes, and game quality on relabelled fixtures.
5. `run_live_pipeline` routes SC2026 and U2025 through its orchestration without
   hard-coding `E`.

WHAT THESE TESTS DO NOT PROVE. The synthetic caches reuse E2024 Boxscore and
PlaybyPlay bodies and change only their schedule metadata, so they do not prove
that real EuroCup or SuperCup payloads parse or satisfy lineup invariants. The
orchestration test replaces every database write, derived build, and live gate
with a recording stub; it proves call scope and order, not warehouse behaviour.
No test here reaches the public API, Supabase, or a live workflow.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import euroleague.live as live_module
from euroleague.cache import ResponseCache
from euroleague.derived import build_dimensions, build_game_events, build_remaining_rows
from euroleague.live import run_live_pipeline


def _make_synthetic_competition_cache(
    root_dir: Path,
    fixture_cache: ResponseCache,
    season_code: str,
    competition_code: str,
    gamecode: int = 1,
) -> ResponseCache:
    """Build a valid single-game cache for a given competition season code."""
    season_root = root_dir / season_code
    cache = ResponseCache(root_dir)

    for endpoint in ("Boxscore", "PlaybyPlay"):
        target = season_root / endpoint / f"{gamecode}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(fixture_cache.path_for("E2024", endpoint, gamecode), target)

    points_path = season_root / "Points" / f"{gamecode}.json"
    points_path.parent.mkdir(parents=True, exist_ok=True)
    points_path.write_text(json.dumps({"Rows": [{"NUM_ANOT": 1}]}), encoding="utf-8")

    # Read the real Boxscore to extract team codes
    boxscore = json.loads(
        (season_root / "Boxscore" / f"{gamecode}.json").read_text(encoding="utf-8")
    )
    teams = boxscore.get("Stats") or []
    local_code = (
        teams[0].get("PlayersStats", [{}])[0].get("Team", "BER") if len(teams) > 0 else "BER"
    )
    road_code = (
        teams[1].get("PlayersStats", [{}])[0].get("Team", "PAN") if len(teams) > 1 else "PAN"
    )

    schedule_data = {
        "data": [
            {
                "gameCode": gamecode,
                "played": True,
                "season": {"competitionCode": competition_code, "code": season_code},
                "phaseType": {"code": "RS", "name": "Regular Season"},
                "round": 1,
                "roundName": "Round 1",
                "gameStatus": "Played",
                "local": {"club": {"code": local_code, "name": f"Club {local_code}"}, "score": 85},
                "road": {"club": {"code": road_code, "name": f"Club {road_code}"}, "score": 80},
            }
        ]
    }
    schedule_path = season_root / "schedule.json"
    schedule_path.parent.mkdir(parents=True, exist_ok=True)
    schedule_path.write_text(json.dumps(schedule_data), encoding="utf-8")

    return cache


@pytest.mark.parametrize(
    ("season_code", "competition_code"),
    [
        ("SC2026", "SC"),
        ("U2025", "U"),
        ("E2026", "E"),
    ],
)
def test_derived_layer_scopes_and_invariants_for_competitions(
    tmp_path: Path,
    fixture_cache: ResponseCache,
    season_code: str,
    competition_code: str,
) -> None:
    """Verify build_dimensions, build_game_events, and build_remaining_rows for SC, U, and E."""
    cache = _make_synthetic_competition_cache(
        tmp_path, fixture_cache, season_code, competition_code, gamecode=1
    )

    # 1. Dimensions
    dimensions = build_dimensions(cache, season_code)
    assert dimensions.players, "Players must be extracted from Boxscore"
    assert dimensions.teams, "Teams must be extracted from schedule"
    assert dimensions.team_seasons, "Team seasons must be generated"
    for ts in dimensions.team_seasons:
        ts_season, _ts_team, ts_comp, _ts_name = ts
        assert ts_season == season_code
        assert ts_comp == competition_code

    # 2. Game events
    events = build_game_events(cache, season_code)
    assert events, "GameEventRows must be generated from PlaybyPlay"
    for event in events:
        assert event.season_code == season_code
        assert event.competition_code == competition_code
        assert event.gamecode == 1

    # 3. Remaining rows (lineups, stints, possessions, game_quality, player_minutes)
    remaining = build_remaining_rows(cache, season_code)
    assert remaining.lineups, "Lineups must be generated"
    assert remaining.stints, "Stints must be generated"
    for stint in remaining.stints:
        assert stint.season_code == season_code
        assert stint.gamecode == 1

    assert remaining.possessions, "Possessions must be generated"
    for poss in remaining.possessions:
        assert poss.season_code == season_code
        assert poss.gamecode == 1

    assert remaining.game_qualities, "Game quality must be generated"
    for gq in remaining.game_qualities:
        assert gq.season_code == season_code
        assert gq.gamecode == 1
        assert gq.excluded_by_default is False
        assert gq.quarantine_reasons == []

    assert remaining.player_minutes, "Player minutes must be generated"
    for pm in remaining.player_minutes:
        assert pm.season_code == season_code
        assert pm.gamecode == 1


@pytest.mark.parametrize(
    ("season_code", "competition_code"),
    [
        ("SC2026", "SC"),
        ("U2025", "U"),
        ("E2026", "E"),
    ],
)
def test_run_live_pipeline_orchestration_for_competitions(
    tmp_path: Path,
    fixture_cache: ResponseCache,
    monkeypatch: pytest.MonkeyPatch,
    season_code: str,
    competition_code: str,
) -> None:
    """Break caught: live orchestration silently rewrites a non-E season to E."""
    from euroleague.fetch import competition_for_season_code

    cache = _make_synthetic_competition_cache(
        tmp_path, fixture_cache, season_code, competition_code, gamecode=1
    )

    recorded_progress: list[tuple[str, str, int]] = []
    recorded_sources: list[tuple[str, tuple[int, ...]]] = []
    raw_games_loaded: list[str] = []
    derived_games_derived: list[str] = []
    gated_games: list[str] = []

    connection = object()

    # Record orchestration boundaries without claiming to exercise PostgreSQL.
    monkeypatch.setattr(live_module, "loaded_gamecodes", lambda conn, season: set())
    monkeypatch.setattr(
        live_module,
        "sync_season_schedule",
        lambda conn, season, games: len(games),
    )
    monkeypatch.setattr(
        live_module,
        "record_season_progress",
        lambda conn, season, count: recorded_progress.append(
            (season, competition_for_season_code(season), count)
        ),
    )
    monkeypatch.setattr(
        live_module,
        "load_new_raw_games",
        lambda conn, c, season, games, **kwargs: (
            raw_games_loaded.append(season) or {"events_parsed": 10}
        ),
    )
    monkeypatch.setattr(
        live_module,
        "derive_new_games",
        lambda conn, c, season, codes: derived_games_derived.append(season) or {"lineup": 5},
    )
    monkeypatch.setattr(
        live_module,
        "assert_live_games_gated",
        lambda conn, season, codes: gated_games.append(season) or {},
    )
    monkeypatch.setattr(
        live_module,
        "record_cached_game_sources",
        lambda conn, c, season, codes: recorded_sources.append((season, tuple(codes))),
    )

    summary = run_live_pipeline(connection, cache, season_code, progress=lambda line: None)

    assert summary.season_code == season_code
    assert summary.scheduled == 1
    assert summary.played == 1
    assert summary.already_loaded == 0
    assert summary.newly_loaded == (1,)

    assert recorded_progress == [(season_code, competition_code, 1)]
    assert raw_games_loaded == [season_code]
    assert derived_games_derived == [season_code]
    assert gated_games == [season_code]
    assert recorded_sources == [(season_code, (1,))]
