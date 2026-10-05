"""Validation tests for exact possession counting from ordered events."""

from __future__ import annotations

from collections import Counter

import pytest

from euroleague.cache import ResponseCache
from euroleague.derived import build_game_events, build_remaining_rows
from euroleague.events import EventRecord, flatten_play_by_play
from euroleague.possessions import (
    EVENT_ROLES,
    EventRole,
    UnclassifiedEventTypeError,
    count_game_possessions,
)

ALL_E2024_EVENT_TYPES = {
    "2FGA",
    "2FGM",
    "3FGA",
    "3FGM",
    "AG",
    "AS",
    "B",
    "BP",
    "C",
    "CCH",
    "CM",
    "CMD",
    "CMT",
    "CMT1",
    "CMTI",
    "CMU",
    "CMU_D",
    "CMU_DI",
    "CMU_FL",
    "D",
    "EG",
    "EP",
    "FTA",
    "FTM",
    "FV",
    "IN",
    "JB",
    "O",
    "OF",
    "OUT",
    "RV",
    "ST",
    "TO",
    "TOUT",
    "TOUT_TV",
}


def _event(
    index: int,
    playtype: str,
    team_code: str | None = "AAA",
    player_id: str | None = "P1",
    *,
    period: int = 1,
    score_a: int = 0,
    score_b: int = 0,
) -> EventRecord:
    return EventRecord(
        ingest_index=index,
        source_list="FirstQuarter" if period == 1 else "SecondQuarter",
        period=period,
        numberofplay=index + 1,
        playtype=playtype,
        player_id=player_id,
        team_code=team_code,
        markertime="05:00",
        minute=5,
        points_a_raw=None,
        points_b_raw=None,
        elapsed_seconds_raw=(period - 1) * 600 + 300,
        clock_moved_backwards=False,
        score_a=score_a,
        score_b=score_b,
    )


# Types absent from E2023-E2025 but present in older seasons (DECISIONS.md item 85):
# `TPOFF` names each team's tip-off jumper, two rows in every E2020 and E2021
# game; `F` and `BF` are fighting and bench-fighting fouls, 15 rows, all in
# E2022 game 313.
OLDER_SEASON_EVENT_TYPES = {"TPOFF", "F", "BF"}


def test_vocabulary_explicitly_classifies_all_observed_event_types() -> None:
    """Break caught: a newly observed type is silently ignored by the counter."""
    assert set(EVENT_ROLES) == ALL_E2024_EVENT_TYPES | OLDER_SEASON_EVENT_TYPES
    assert sum(role is EventRole.ENDING for role in EVENT_ROLES.values()) == 5
    assert sum(role is EventRole.CONTINUING for role in EVENT_ROLES.values()) == 4
    assert sum(role is EventRole.NO_BALL for role in EVENT_ROLES.values()) == 29


def test_older_season_marker_and_fight_rows_do_not_change_possessions() -> None:
    """Break caught: a tip-off marker or a fighting foul opens or closes a possession."""
    plain = [
        _event(0, "2FGA", "AAA"),
        _event(1, "D", "BBB"),
        _event(2, "2FGM", "BBB", score_b=2),
        _event(3, "TO", "AAA"),
    ]
    with_older_rows = [
        _event(0, "TPOFF", "AAA"),
        _event(1, "TPOFF", "BBB"),
        _event(2, "2FGA", "AAA"),
        _event(3, "D", "BBB"),
        _event(4, "F", "BBB"),
        _event(5, "F", "AAA"),
        _event(6, "BF", "AAA", player_id="CO_A"),
        _event(7, "2FGM", "BBB", score_b=2),
        _event(8, "TO", "AAA"),
    ]

    expected = count_game_possessions(plain, "AAA", "BBB")
    observed = count_game_possessions(with_older_rows, "AAA", "BBB")

    assert observed.team_counts == expected.team_counts == {"AAA": 2, "BBB": 1}
    assert [p.end_reason for p in observed.possessions] == [
        p.end_reason for p in expected.possessions
    ]


def test_unclassified_event_type_fails_instead_of_being_ignored() -> None:
    """Break caught: an unknown API event changes possessions without an error."""
    with pytest.raises(UnclassifiedEventTypeError, match="UNKNOWN"):
        count_game_possessions([_event(0, "UNKNOWN")], "AAA", "BBB")


def test_made_field_goal_closes_its_teams_open_possession() -> None:
    """Break caught: a made basket is counted as points but not as an ending."""
    events = [_event(0, "2FGA"), _event(1, "O"), _event(2, "2FGM", score_a=2)]

    result = count_game_possessions(events, "AAA", "BBB")

    assert result.team_counts == {"AAA": 1, "BBB": 0}
    endings = [
        (possession.offense_team_code, possession.end_reason) for possession in result.possessions
    ]
    assert endings == [("AAA", "made_shot")]


def test_blank_player_team_turnover_is_a_real_possession_ending() -> None:
    """Break caught: requiring a player ID drops team turnovers."""
    result = count_game_possessions([_event(0, "TO", player_id=None)], "AAA", "BBB")

    assert result.team_counts == {"AAA": 1, "BBB": 0}
    assert result.possessions[0].end_reason == "turnover"


def test_team_rebounds_behave_like_player_rebounds_for_ball_control() -> None:
    """Break caught: team rebounds are skipped as bookkeeping rows."""
    events = [
        _event(0, "2FGA", "AAA"),
        _event(1, "O", "AAA", player_id=None),
        _event(2, "3FGA", "AAA"),
        _event(3, "D", "BBB", player_id=None),
        _event(4, "TO", "BBB"),
    ]

    result = count_game_possessions(events, "AAA", "BBB")

    assert result.team_counts == {"AAA": 1, "BBB": 1}
    assert [possession.end_reason for possession in result.possessions] == [
        "defensive_rebound",
        "turnover",
    ]


def test_last_shot_outcome_decides_a_regular_free_throw_trip_ending() -> None:
    """Break caught: every make ends a possession, or a final miss ends one early."""
    made_trip = [
        _event(0, "CM", "BBB", "DEF"),
        _event(1, "FTM", "AAA", "P1"),
        _event(2, "FTM", "AAA", "P1"),
    ]
    missed_trip_then_rebound = [
        _event(0, "CM", "BBB", "DEF"),
        _event(1, "FTM", "AAA", "P1"),
        _event(2, "FTA", "AAA", "P1"),
        _event(3, "D", "BBB", "P2"),
        _event(4, "TO", "BBB", "P2"),
    ]

    made = count_game_possessions(made_trip, "AAA", "BBB")
    missed = count_game_possessions(missed_trip_then_rebound, "AAA", "BBB")

    assert made.team_counts == {"AAA": 1, "BBB": 0}
    assert [possession.end_reason for possession in made.possessions] == ["made_free_throw"]
    assert missed.team_counts == {"AAA": 1, "BBB": 1}
    assert [possession.end_reason for possession in missed.possessions] == [
        "defensive_rebound",
        "turnover",
    ]


def test_and_one_free_throw_does_not_end_the_possession_twice() -> None:
    """Break caught: the made basket and its bonus free throw both increment the count."""
    events = [
        _event(0, "2FGM", "AAA", "P1", score_a=2),
        _event(1, "CM", "BBB", "DEF", score_a=2),
        _event(2, "FTM", "AAA", "P1", score_a=3),
    ]

    result = count_game_possessions(events, "AAA", "BBB")

    assert result.team_counts == {"AAA": 1, "BBB": 0}
    assert [possession.end_reason for possession in result.possessions] == ["made_shot"]


@pytest.mark.parametrize("foul_type", ["CMT", "C", "B", "CMU"])
def test_technical_or_unsportsmanlike_free_throw_does_not_end_control(
    foul_type: str,
) -> None:
    """Break caught: a possession-retaining penalty free throw invents an ending."""
    events = [
        _event(0, "2FGA", "AAA", "P1"),
        _event(1, foul_type, "BBB", "DEF"),
        _event(2, "FTM", "AAA", "P1", score_a=1),
        _event(3, "TO", "AAA", "P1", score_a=1),
    ]

    result = count_game_possessions(events, "AAA", "BBB")

    assert result.team_counts == {"AAA": 1, "BBB": 0}
    assert [possession.end_reason for possession in result.possessions] == ["turnover"]


def test_defensive_rebound_of_a_missed_and_one_does_not_invent_a_second_possession() -> None:
    """Break caught: the bonus free throw is skipped but its rebound still closes.

    The possession already ended at the basket. Rebounding the bonus miss hands
    the ball to the defence; it must not close a second possession for the team
    that scored.
    """
    events = [
        _event(0, "2FGM", "AAA", "P1", score_a=2),
        _event(1, "CM", "BBB", "DEF", score_a=2),
        _event(2, "FTA", "AAA", "P1", score_a=2),
        _event(3, "D", "BBB", "P9", score_a=2),
        _event(4, "TO", "BBB", "P9", score_a=2),
    ]

    result = count_game_possessions(events, "AAA", "BBB")

    assert result.team_counts == {"AAA": 1, "BBB": 1}
    assert [possession.end_reason for possession in result.possessions] == [
        "made_shot",
        "turnover",
    ]


def test_offensive_rebound_of_a_missed_and_one_starts_a_new_possession() -> None:
    """Break caught: suppressing the rebound entirely loses a real second chance."""
    events = [
        _event(0, "2FGM", "AAA", "P1", score_a=2),
        _event(1, "CM", "BBB", "DEF", score_a=2),
        _event(2, "FTA", "AAA", "P1", score_a=2),
        _event(3, "O", "AAA", "P2", score_a=2),
        _event(4, "2FGM", "AAA", "P2", score_a=4),
    ]

    result = count_game_possessions(events, "AAA", "BBB")

    assert result.team_counts == {"AAA": 2, "BBB": 0}
    assert [possession.end_reason for possession in result.possessions] == [
        "made_shot",
        "made_shot",
    ]


def test_game_200_and_one_rebounds_are_not_counted_as_extra_possessions(
    fixture_cache: ResponseCache,
) -> None:
    """Break caught: the defect that produced 272 phantom E2024 possessions.

    Game 200 carried five of them in one game and was the worst gate failure at
    eight possessions apart. Indexes 58-62 are the worked case: PAN scores, the
    bonus free throw misses, and ZAL rebounds it.
    """
    events = flatten_play_by_play(fixture_cache.read_json("E2024", "PlaybyPlay", 200))
    by_index = {event.ingest_index: event for event in events}

    assert by_index[58].playtype == "2FGM"
    assert by_index[61].playtype == "FTA"
    assert by_index[62].playtype == "D"
    assert by_index[62].team_code != by_index[58].team_code

    result = count_game_possessions(events, "PAN", "ZAL")
    ending_at_the_rebound = [
        possession for possession in result.possessions if possession.end_ingest_index == 62
    ]

    assert ending_at_the_rebound == []


def test_offensive_foul_is_ignored_and_its_turnover_row_is_counted_once() -> None:
    """Break caught: `OF` and its separate `TO` row create two endings."""
    events = [_event(0, "OF", "AAA", "P1"), _event(1, "TO", "AAA", "P1")]

    result = count_game_possessions(events, "AAA", "BBB")

    assert result.team_counts == {"AAA": 1, "BBB": 0}
    assert [possession.end_reason for possession in result.possessions] == ["turnover"]


def test_period_end_uses_the_structural_period_transition_not_end_markers() -> None:
    """Break caught: an open period is lost when `EP` or `EG` is absent or duplicated."""
    events = [
        _event(0, "2FGA", "AAA", "P1", period=1),
        _event(1, "TO", "BBB", "P2", period=2),
    ]

    result = count_game_possessions(events, "AAA", "BBB")

    assert result.team_counts == {"AAA": 1, "BBB": 1}
    assert [
        (
            possession.offense_team_code,
            possession.end_ingest_index,
            possession.end_reason,
        )
        for possession in result.possessions
    ] == [
        ("AAA", 0, "end_of_period"),
        ("BBB", 1, "turnover"),
    ]


def test_and_one_bonus_point_belongs_to_the_possession_that_already_closed() -> None:
    """Break caught: the and-one free throw is skipped and its point vanishes."""
    events = [
        _event(0, "2FGM", "AAA", "P1", score_a=2),
        _event(1, "CM", "BBB", "DEF", score_a=2),
        _event(2, "FTM", "AAA", "P1", score_a=3),
    ]

    result = count_game_possessions(events, "AAA", "BBB")

    assert result.team_points == {"AAA": 3, "BBB": 0}
    assert result.off_possession_points == {"AAA": 0, "BBB": 0}


def test_technical_free_throw_points_are_reported_outside_every_possession() -> None:
    """Break caught: technical points are silently dropped or credited to offence."""
    events = [
        _event(0, "2FGA", "AAA", "P1"),
        _event(1, "CMT", "BBB", "DEF"),
        _event(2, "FTM", "AAA", "P1", score_a=1),
        _event(3, "TO", "AAA", "P1", score_a=1),
    ]

    result = count_game_possessions(events, "AAA", "BBB")

    assert result.team_points == {"AAA": 0, "BBB": 0}
    assert result.off_possession_points == {"AAA": 1, "BBB": 0}


def test_every_possession_is_credited_to_a_lineup_of_the_offence(
    fixture_cache: ResponseCache,
) -> None:
    """Break caught: a home/away swap credits possessions to the wrong five."""
    rows = build_remaining_rows(fixture_cache, "E2024")
    team_of_lineup = {lineup.lineup_id: lineup.team_code for lineup in rows.lineups}

    assert rows.possessions
    for possession in rows.possessions:
        assert team_of_lineup[possession.offense_lineup_id] == possession.offense_team_code
        assert team_of_lineup[possession.defense_lineup_id] == possession.defense_team_code


def test_lineup_possessions_sum_to_team_possessions(fixture_cache: ResponseCache) -> None:
    """Break caught: the straddle convention differs between the two levels.

    A possession spanning a substitution is credited wholly to the lineup on
    court when it started. If the lineup-level and team-level totals used
    different conventions this sum would fail for reasons that look exactly
    like a bug, which is why `CLAUDE.md` names it as an invariant.
    """
    rows = build_remaining_rows(fixture_cache, "E2024")
    team_of_lineup = {lineup.lineup_id: lineup.team_code for lineup in rows.lineups}

    per_game_team: Counter[tuple[int, str]] = Counter()
    per_game_lineup: Counter[tuple[int, str, str]] = Counter()
    for possession in rows.possessions:
        per_game_team[(possession.gamecode, possession.offense_team_code)] += 1
        per_game_lineup[
            (
                possession.gamecode,
                team_of_lineup[possession.offense_lineup_id],
                possession.offense_lineup_id,
            )
        ] += 1

    rolled_up: Counter[tuple[int, str]] = Counter()
    for (gamecode, team_code, _lineup_id), count in per_game_lineup.items():
        rolled_up[(gamecode, team_code)] += count

    assert rolled_up == per_game_team


def test_a_possession_starting_inside_a_stint_is_credited_to_that_stint(
    fixture_cache: ResponseCache,
) -> None:
    """Break caught: a straddling possession is credited to where it ended."""
    rows = build_remaining_rows(fixture_cache, "E2024")
    stints = {(stint.gamecode, stint.stint_index): stint for stint in rows.stints}

    straddling = [row for row in rows.possessions if row.straddles_substitution]

    assert straddling, "the straddle population must not be empty"
    for possession in rows.possessions:
        stint = stints[(possession.gamecode, possession.stint_index)]
        assert stint.start_ingest_index <= possession.start_ingest_index
        assert possession.start_ingest_index <= stint.end_ingest_index
    for possession in straddling:
        stint = stints[(possession.gamecode, possession.stint_index)]
        assert possession.end_ingest_index > stint.end_ingest_index


def test_possession_seconds_are_monotonic_and_inside_their_stint(
    fixture_cache: ResponseCache,
) -> None:
    """Mechanical: a possession stays inside its stint and ends no earlier
    than it starts, unless a documented MARKERTIME backward-clock step
    (CLAUDE.md: "occasionally runs backwards ... around substitutions during
    free throws") falls inside the possession - never for any other reason -
    and the raw and corrected clocks agree on possession boundaries because
    the correction touches only IN/OUT rows.

    Measured on the full E2024 cache on 2026-09-07: 139 of 47,829 possessions
    (0.291%) carry a clock_moved_backwards event in their span, by up to 60
    seconds; every one of the strict-order violations traces to exactly one
    of those events, never to an unflagged one. On the committed fixture set,
    that is 16 possessions with end_seconds_elapsed < start_seconds_elapsed
    and 2 possessions with start_seconds_elapsed < their stint's
    start_elapsed_raw (game 35 and game 272; game 323's "full 60-second
    backwards clock step" is the largest of the 16). Both counts are
    asserted below, not just their sum, so a future fix that quietly stops
    exercising one of the two shapes is caught.
    """
    rows = build_remaining_rows(fixture_cache, "E2024")
    stints = {(stint.gamecode, stint.stint_index): stint for stint in rows.stints}

    events_by_game: dict[int, list] = {}
    for event in build_game_events(fixture_cache, "E2024"):
        events_by_game.setdefault(event.gamecode, []).append(event)
    positions_by_game = {
        gamecode: {event.ingest_index: position for position, event in enumerate(events)}
        for gamecode, events in events_by_game.items()
    }
    events_by_key = {
        (event.gamecode, event.ingest_index): event
        for events in events_by_game.values()
        for event in events
    }

    ends_before_start = 0
    starts_before_stint = 0
    for possession in rows.possessions:
        stint = stints[(possession.gamecode, possession.stint_index)]

        start_event = events_by_key[(possession.gamecode, possession.start_ingest_index)]
        assert start_event.elapsed_seconds_corrected == start_event.elapsed_seconds_raw
        end_event = events_by_key[(possession.gamecode, possession.end_ingest_index)]
        assert end_event.elapsed_seconds_corrected == end_event.elapsed_seconds_raw

        positions = positions_by_game[possession.gamecode]
        span = events_by_game[possession.gamecode][
            positions[possession.start_ingest_index] : positions[possession.end_ingest_index] + 1
        ]
        clock_stepped_backwards = any(event.clock_moved_backwards for event in span)

        possession_starts_before_stint = possession.start_seconds_elapsed < stint.start_elapsed_raw
        possession_ends_before_start = (
            possession.end_seconds_elapsed < possession.start_seconds_elapsed
        )

        if possession_starts_before_stint or possession_ends_before_start:
            ends_before_start += possession_ends_before_start
            starts_before_stint += possession_starts_before_stint
            assert clock_stepped_backwards, (
                f"possession {possession.gamecode}/{possession.possession_index} breaks "
                "seconds ordering with no clock_moved_backwards event in its span - that "
                "would be a computation bug, not the documented clock defect"
            )
        else:
            assert possession.start_seconds_elapsed >= stint.start_elapsed_raw
            assert possession.end_seconds_elapsed >= possession.start_seconds_elapsed

    assert ends_before_start > 0, "the fixture set's clock-defect games must exercise this branch"
    assert starts_before_stint > 0, "the fixture set's clock-defect games must exercise this branch"


def test_a_game_failing_the_possession_gate_is_quarantined_not_dropped(
    fixture_cache: ResponseCache,
) -> None:
    """Break caught: gate failures are silently loaded or silently discarded."""
    rows = build_remaining_rows(fixture_cache, "E2024")
    quality = {row.gamecode: row for row in rows.game_qualities}

    failing = [code for code, row in quality.items() if "possession_gate" in row.quarantine_reasons]

    assert failing, "the fixture set must carry at least one gate failure"
    for gamecode in failing:
        assert quality[gamecode].excluded_by_default
        # The rows are still built. Quarantine excludes by default, never deletes.
        assert any(row.gamecode == gamecode for row in rows.possessions)


@pytest.mark.parametrize("season", ["E2024", "E2025"])
@pytest.mark.full_season
def test_possession_points_plus_off_possession_points_equal_the_final_score(
    season: str,
) -> None:
    """Break caught: a scoring event lands in no possession and is lost.

    This is the phase's one exact accounting identity, and unlike the gate it
    has an external ground truth: the official running score. Technical and
    unsportsmanlike free throws belong to no possession by design, so they are
    reported separately rather than dropped, and the two together must
    reconcile to the last forward-filled score of the game.
    """
    cache = ResponseCache("exploration/cache")
    mismatches: list[tuple[int, int, int, int, int]] = []

    for game in cache.read_schedule_json(season)["data"]:
        gamecode = int(game["gameCode"])
        home_team = str(game["local"]["club"]["code"]).strip()
        away_team = str(game["road"]["club"]["code"]).strip()
        events = flatten_play_by_play(cache.read_json(season, "PlaybyPlay", gamecode))
        result = count_game_possessions(events, home_team, away_team)

        home_total = result.team_points[home_team] + result.off_possession_points[home_team]
        away_total = result.team_points[away_team] + result.off_possession_points[away_team]
        final = events[-1]
        if (home_total, away_total) != (final.score_a, final.score_b):
            mismatches.append((gamecode, home_total, final.score_a, away_total, final.score_b))

    assert mismatches == []


@pytest.mark.parametrize("season", ["E2024", "E2025"])
@pytest.mark.full_season
def test_each_team_is_within_two_independently_counted_possessions(season: str) -> None:
    """Break caught: believable pace hides a team-level counting disagreement."""
    cache = ResponseCache("exploration/cache")
    failures: list[tuple[int, str, int, str, int]] = []

    for game in cache.read_schedule_json(season)["data"]:
        gamecode = int(game["gameCode"])
        home_team = str(game["local"]["club"]["code"]).strip()
        away_team = str(game["road"]["club"]["code"]).strip()
        result = count_game_possessions(
            flatten_play_by_play(cache.read_json(season, "PlaybyPlay", gamecode)),
            home_team,
            away_team,
        )
        home_count = result.team_counts[home_team]
        away_count = result.team_counts[away_team]
        if abs(home_count - away_count) > 2:
            failures.append((gamecode, home_team, home_count, away_team, away_count))

    assert failures == []


def test_and_one_bonus_taken_by_a_substitute_still_belongs_to_the_closed_possession() -> None:
    """Break caught: the bonus counts a second possession when the shooter changed.

    The fouled scorer can leave the court before the bonus is taken - an injury
    substitution - and a team-mate then shoots it. The shot is still the bonus
    for the basket that already closed the possession. Recognising it by the
    shooter's identity misses exactly this case; the `RV` row says who was
    fouled, and that is explicit data rather than an inference.

    Measured in E2024 games 29 and 270.
    """
    events = [
        _event(0, "2FGM", "AAA", "P1", score_a=2),
        _event(1, "CM", "BBB", "DEF", score_a=2),
        _event(2, "RV", "AAA", "P1", score_a=2),
        _event(3, "IN", "AAA", "P2", score_a=2),
        _event(4, "OUT", "AAA", "P1", score_a=2),
        _event(5, "FTM", "AAA", "P2", score_a=3),
    ]

    result = count_game_possessions(events, "AAA", "BBB")

    assert result.team_counts == {"AAA": 1, "BBB": 0}
    assert [possession.end_reason for possession in result.possessions] == ["made_shot"]
    assert result.team_points["AAA"] == 3
    assert result.off_possession_points["AAA"] == 0


def test_a_foul_on_a_different_player_after_a_basket_is_not_a_bonus() -> None:
    """Break caught: relaxing the shooter check swallows an ordinary next possession.

    A foul on someone other than the scorer, after the basket, is a foul in the
    next possession - the defence had the ball and gave it back. Its free throws
    must close a possession of their own.
    """
    events = [
        _event(0, "2FGM", "AAA", "P1", score_a=2),
        _event(1, "CM", "BBB", "DEF", score_a=2),
        _event(2, "RV", "AAA", "P3", score_a=2),
        _event(3, "FTM", "AAA", "P3", score_a=3),
    ]

    result = count_game_possessions(events, "AAA", "BBB")

    assert result.team_counts == {"AAA": 2, "BBB": 0}
    assert [possession.end_reason for possession in result.possessions] == [
        "made_shot",
        "made_free_throw",
    ]
