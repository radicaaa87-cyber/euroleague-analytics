"""The daily question a live season asks: which games are new, and load those.

Everything before this module loads a *finished* season in one pass. A live
season arrives a few games at a time, and the difference is not the volume - it
is that the same season is loaded again and again, and every run after the first
must add without disturbing.

THE ASYMMETRY THAT SHAPES THIS FILE. Missing a newly played game is a visible
failure: the season comes up short and somebody notices. Re-selecting a game the
warehouse already holds is invisible: the raw loader would replace rows that
lineups, stints and possessions were built from, and nothing would error. So the
selection rule is deliberately conservative - it *adds* games and never removes,
replaces or repairs one. A game that changes after it was loaded is a source
revision, and Decision 7 gives that its own per-game transactional path.

WHY THE DERIVED BUILD STILL READS THE WHOLE SEASON. `validate_season` decides
whether the minutes correction is enabled by comparing aggregates across every
game in the cache (Decision 3's safety belt). Handing it this week's ten games
would compute that flag from ten games instead of the season, and the flag feeds
`elapsed_seconds_corrected`, which feeds stints, lineups and possessions. The
run would succeed and disagree with everything already loaded. So the build
consumes the complete restored cache and only the *write* is scoped to the new
games - which is exactly the split Block B established.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from euroleague.archive import assert_complete_played_cache
from euroleague.cache import ResponseCache
from euroleague.derived import (
    attach_game_event_references,
    build_dimensions,
    build_game_events,
    build_remaining_rows,
    select_remaining_games,
)
from euroleague.derived_load import (
    _assert_dimension_scope,
    _assert_remaining_scope,
    _assert_season_code,
    delete_derived_game_rows,
    insert_staged_derived_game_rows,
    insert_staged_dimension_rows,
    load_derived_rows,
    load_dimensions,
    prune_obsolete_dimensions,
    select_dimensions_for_game,
    stage_attached_game_rows,
    stage_dimension_rows,
    stage_obsolete_dimension_candidates,
)
from euroleague.fetch import competition_for_season_code
from euroleague.gate import assert_phase5_reconciles
from euroleague.load import (
    assert_phase4_safe,
    delete_raw_game_rows,
    delete_raw_shot_rows,
    insert_staged_raw_game_rows,
    insert_staged_raw_shot_rows,
    load_game,
    load_shots_for_game,
    played_games,
    stage_raw_game_rows,
    stage_raw_shot_rows,
)
from euroleague.parse import parse_cached_game, parse_shots
from euroleague.roster import load_cached_roster
from euroleague.source_state import (
    cached_game_source_checksums,
    record_cached_game_sources,
    upsert_applied_game_sources,
)

# The endpoints a played game must have on disk before it can be loaded. Points
# is archived and parsed for coordinates, and is required for the same reason
# the other two are: discovering it missing halfway through leaves the season
# part-loaded.
REQUIRED_ENDPOINTS: tuple[str, ...] = ("Boxscore", "PlaybyPlay", "Points")


@dataclass(frozen=True)
class LiveRunSummary:
    """What one daily run did, in a form safe to print into a public log.

    Deliberately holds counts and gamecodes only. Nothing here is derived from a
    connection string, and `as_log_line` is the only thing the workflow prints.
    """

    season_code: str
    scheduled: int
    played: int
    already_loaded: int
    newly_loaded: tuple[int, ...]
    roster_registrations: int = 0

    def as_log_line(self) -> str:
        """One line stating what happened, including when nothing did.

        A run that loaded nothing must not look like a run that succeeded
        quietly. Before 2026-09-24 every E2026 run legitimately finds zero
        played games, and the log has to say so rather than print nothing.
        """
        games = ",".join(str(code) for code in self.newly_loaded) if self.newly_loaded else "-"
        return (
            f"season {self.season_code}: scheduled={self.scheduled} played={self.played} "
            f"already_loaded={self.already_loaded} new={len(self.newly_loaded)} games={games} "
            f"roster_registrations={self.roster_registrations}"
        )


class GameNotRebuildableError(RuntimeError):
    """Raised when the named game cannot be rebuilt from the cache as it stands."""


@dataclass(frozen=True)
class RebuildSummary:
    """What one Decision 7 rebuild replaced, in a form safe to print publicly.

    `season_games_built` is the number of distinct games the derived build
    produced event rows for - the population the season-wide minutes-correction
    flag was decided from. It is here rather than in a comment because a
    rebuild that quietly narrowed its build to one game would still succeed,
    and this is the number that shows it did.

    `counts` holds every table the rebuild staged, dimension tables included.
    They are the rebuilt game's own players and teams rather than the season's,
    so the number means what the rest of the dict means.
    """

    season_code: str
    gamecode: int
    season_games_built: int
    counts: dict[str, int]

    def as_log_line(self) -> str:
        """One line naming the game, the population it was built from, and the rows."""
        written = ", ".join(f"{table}={count:,}" for table, count in sorted(self.counts.items()))
        return (
            f"rebuilt {self.season_code} game {self.gamecode} from "
            f"{self.season_games_built} cached game(s): {written}"
        )


def _schedule_entry(cache: ResponseCache, season_code: str, gamecode: int) -> dict:
    """Find one game in the cached schedule and refuse anything that is not one.

    Both refusals are cheap and both happen before a single statement is run.
    A gamecode the schedule does not list is a typo or a season mix-up, and
    "rebuilding" it would delete a real game's rows and replace them with
    nothing. A game the schedule does not mark played has no Boxscore to
    rebuild from at all.
    """
    schedule = cache.read_schedule_json(season_code)
    for game in schedule.get("data") or []:
        if int(game["gameCode"]) != int(gamecode):
            continue
        if game.get("played") is not True:
            raise GameNotRebuildableError(
                f"{season_code} game {gamecode} is not marked played in the cached "
                "schedule, so there are no source bytes to rebuild it from. Refresh "
                "the schedule if the game has since been played."
            )
        return game
    raise GameNotRebuildableError(
        f"{season_code} game {gamecode} is not in the cached schedule. Check the "
        "gamecode and the season, and restore the archive if the schedule is stale."
    )


def rebuild_revised_game(
    connection: Any,
    cache: ResponseCache,
    season_code: str,
    gamecode: int,
) -> RebuildSummary:
    """Rebuild one game's parsed and derived rows from revised source bytes.

    This is the half of Decision 7 that had never been built. A settlement
    re-check archives a changed response body beside its predecessor; this
    replaces the rows that were built from the superseded bytes, for that one
    game, in one transaction.

    THE BUILD READS THE WHOLE SEASON AND THE WRITE NAMES ONE GAME. That split
    is not an optimisation, it is the correctness condition. `validate_season`
    decides whether the minutes correction is enabled by comparing aggregates
    across every game in the cache, and that flag feeds
    `elapsed_seconds_corrected`, which feeds stints, lineups and possessions.
    Building from the revised game alone would decide the flag from a
    population of one; the rebuild would succeed, and the rebuilt game would
    silently disagree with every other game in the warehouse. So the completest
    cache the season has is what the build consumes, and only the write is
    narrowed. `assert_complete_played_cache` is what makes "the whole season"
    a checked precondition rather than a hope.

    ONE TRANSACTION, NOT SEVERAL. Raw rows and derived rows are replaced
    together. Committing the raw half separately would leave a window - and,
    on failure, a permanent state - where the game's derived rows describe
    source bytes that are no longer stored.

    POINTS MOVES WITH THE GAME. The live writer now loads `raw_shot`, so a
    revised Points body is staged and replaced inside the same transaction as
    the other raw and derived rows. Its checksum is not marked applied until
    every replacement write succeeds.
    """
    _assert_season_code(season_code)
    gamecode = int(gamecode)
    schedule_game = _schedule_entry(cache, season_code, gamecode)

    # Ordered deliberately: everything that can refuse does so before the
    # transaction opens, so a refusal leaves the warehouse untouched.
    assert_complete_played_cache(cache, season_code)

    parsed = parse_cached_game(cache, season_code, schedule_game)
    shots = tuple(
        parse_shots(
            season_code,
            gamecode,
            parsed.game.competition_code,
            cache.read_json(season_code, "Points", gamecode),
        )
    )
    source_checksums = cached_game_source_checksums(cache, season_code, (gamecode,))[gamecode]
    dimensions = build_dimensions(cache, season_code)
    events = build_game_events(cache, season_code)
    remaining = build_remaining_rows(cache, season_code)
    _assert_dimension_scope(dimensions, season_code)
    _assert_remaining_scope(remaining, season_code)
    season_games_built = len({row.gamecode for row in events})

    game_rows = select_remaining_games(remaining, [gamecode])
    empty_derived_tables = [
        name
        for name, rows in (
            ("lineups", game_rows.lineups),
            ("stints", game_rows.stints),
            ("event attachments", game_rows.event_attachments),
            ("player minutes", game_rows.player_minutes),
            ("game quality", game_rows.game_qualities),
            ("possessions", game_rows.possessions),
        )
        if not rows
    ]
    if empty_derived_tables:
        raise GameNotRebuildableError(
            f"The derived build produced no {', '.join(empty_derived_tables)} rows for "
            f"{season_code} game {gamecode}. Rebuilding would delete the stored rows "
            "and replace them with nothing."
        )
    game_events = attach_game_event_references(
        tuple(row for row in events if row.gamecode == gamecode),
        game_rows.event_attachments,
    )
    if not game_events:
        raise GameNotRebuildableError(
            f"The derived build produced no rows for {season_code} game {gamecode}. "
            "Rebuilding would delete the stored rows and replace them with nothing."
        )
    game_dimensions = select_dimensions_for_game(dimensions, game_rows, season_code)

    counts: dict[str, int] = {}
    with connection.transaction(), connection.cursor() as cursor:
        # Stage everything first. A COPY that fails - a revised body that no
        # longer parses into loadable rows - then fails before anything stored
        # has been deleted.
        counts.update(stage_raw_game_rows(cursor, parsed))
        counts["events_parsed"] = len(parsed.events)
        counts["raw_shot"] = stage_raw_shot_rows(cursor, season_code, gamecode, shots)
        counts.update(stage_dimension_rows(cursor, game_dimensions))
        counts.update(stage_attached_game_rows(cursor, game_events, game_rows))
        stage_obsolete_dimension_candidates(cursor, season_code, gamecode)

        # Derived rows go first. Since migration 0023 `game_event` no longer
        # references a raw table, so the order is no longer forced by a
        # cascade; it stays because `delete_derived_game_rows` has its own
        # load-bearing order among the derived tables, and because every count
        # in this transaction is meant to be explicit rather than a side effect.
        delete_derived_game_rows(cursor, season_code, gamecode)
        delete_raw_shot_rows(cursor, season_code, gamecode)
        delete_raw_game_rows(cursor, season_code, gamecode)

        insert_staged_raw_game_rows(cursor)
        insert_staged_raw_shot_rows(cursor)
        insert_staged_dimension_rows(cursor)
        insert_staged_derived_game_rows(cursor)
        prune_obsolete_dimensions(cursor)
        upsert_applied_game_sources(cursor, season_code, gamecode, source_checksums)

    # No VACUUM. One game's dead tuples are not worth a statement that is the
    # only thing in this function not scoped to the game being rebuilt.
    return RebuildSummary(
        season_code=season_code,
        gamecode=gamecode,
        season_games_built=season_games_built,
        counts=counts,
    )


def select_new_games(schedule_data: Iterable[dict], loaded_gamecodes: Iterable[int]) -> list[dict]:
    """Return played games the warehouse does not hold, in gamecode order.

    `played_games` owns what "played" means, shared with the fetcher. This adds
    the second half of the question and nothing else: subtract what is already
    loaded. It never returns a game to be replaced, so a schedule that stops
    marking a loaded game played produces no work here rather than a deletion.
    """
    already = {int(code) for code in loaded_gamecodes}
    return [game for game in played_games(schedule_data) if int(game["gameCode"]) not in already]


def loaded_gamecodes(connection: Any, season_code: str) -> set[int]:
    """Which games of this season the raw layer already holds."""
    with connection.cursor() as cursor:
        cursor.execute(
            "select gamecode from raw_game where season_code = %s",
            (season_code,),
        )
        return {int(row[0]) for row in cursor.fetchall()}


def assert_new_games_cached(cache: ResponseCache, season_code: str, games: Sequence[dict]) -> None:
    """Require every selected game's responses on disk before anything is written.

    Checked for all games before the first is loaded. Finding game nine missing
    after eight are written leaves a state somebody has to reason about; finding
    it first costs one pass over the cache and leaves the warehouse untouched.
    """
    missing = [
        int(game["gameCode"])
        for game in games
        if any(
            not cache.exists(season_code, endpoint, int(game["gameCode"]))
            for endpoint in REQUIRED_ENDPOINTS
        )
    ]
    if missing:
        raise FileNotFoundError(
            f"{len(missing)} game(s) selected as newly played in {season_code} are "
            f"incomplete in the cache: {missing[:10]}. Run the fetcher or restore "
            "the archive; the loader will not fetch them."
        )


def assert_new_games_safe(connection: Any, season_code: str, gamecodes: Sequence[int]) -> None:
    """Refuse to write over a selected game that already carries derived rows.

    Scoped to the games being written, never to the season. A season-wide
    question is always yes after the first week of a live season, and asking it
    would stop the season dead for a reason that is not a defect.
    """
    assert_phase4_safe(connection, season_code, [int(code) for code in gamecodes])


def load_new_raw_games(
    connection: Any,
    cache: ResponseCache,
    season_code: str,
    games: Sequence[dict],
    *,
    progress: Callable[[str], None] = print,
) -> dict[str, int]:
    """Load each newly played game's raw rows, leaving every other game alone."""
    if not games:
        return {}

    gamecodes = [int(game["gameCode"]) for game in games]
    assert_new_games_cached(cache, season_code, games)
    assert_new_games_safe(connection, season_code, gamecodes)

    totals: dict[str, int] = {}
    for index, schedule_game in enumerate(games, start=1):
        gamecode = int(schedule_game["gameCode"])
        parsed = parse_cached_game(cache, season_code, schedule_game)
        counts = load_game(connection, parsed)
        shots = parse_shots(
            season_code,
            gamecode,
            parsed.game.competition_code,
            cache.read_json(season_code, "Points", gamecode),
        )
        counts["raw_shot"] = load_shots_for_game(connection, season_code, gamecode, shots)
        for table, count in counts.items():
            totals[table] = totals.get(table, 0) + count
        progress(
            f"[{index:>3}/{len(games)}] game {gamecode:>3}: "
            f"{counts['events_parsed']:,} events, {counts['raw_boxscore_player']:,} players"
        )
    return totals


def derive_new_games(
    connection: Any,
    cache: ResponseCache,
    season_code: str,
    gamecodes: Sequence[int],
) -> dict[str, int]:
    """Build from the whole season, write only the new games.

    The completeness check is not ceremony. Building from a partial cache would
    silently recompute Decision 3's correction flag from a subset - see this
    module's docstring - so the build refuses rather than producing plausible
    rows that disagree with the ones already stored.
    """
    if not gamecodes:
        return {}

    assert_complete_played_cache(cache, season_code)
    dimensions = build_dimensions(cache, season_code)
    events = build_game_events(cache, season_code)
    remaining = build_remaining_rows(cache, season_code)
    return load_derived_rows(
        connection,
        dimensions,
        events,
        remaining,
        season_code,
        gamecodes=[int(code) for code in gamecodes],
    )


def sync_season_schedule(
    connection: Any,
    season_code: str,
    schedule_games: Sequence[dict],
) -> int:
    """Mirror lightweight schedule facts, including future games.

    This does not load box scores or derived rows.  It exists so forward-looking
    pregame collection can see fixtures that raw_game intentionally does not
    contain until they are played.
    """
    rows: list[tuple[Any, ...]] = []
    for game in schedule_games:
        local = game.get("local") or {}
        road = game.get("road") or {}
        local_club = local.get("club") or {}
        road_club = road.get("club") or {}
        phase = game.get("phaseType") or {}
        season = game.get("season") or {}

        home_code = str(local_club.get("code") or "").strip()
        away_code = str(road_club.get("code") or "").strip()
        if not home_code or not away_code:
            raise ValueError(
                f"{season_code} schedule game {game.get('gameCode')} has missing team code."
            )

        rows.append(
            (
                season_code,
                int(game["gameCode"]),
                str(season.get("competitionCode") or "").strip() or None,
                str(phase.get("code") or "").strip() or None,
                str(phase.get("name") or "").strip() or None,
                int(game["round"]) if game.get("round") is not None else None,
                str(game.get("roundName") or "").strip() or None,
                game.get("played") is True,
                str(game.get("gameStatus") or "").strip() or None,
                game.get("utcDate"),
                home_code,
                str(local_club.get("name") or "").strip() or home_code,
                away_code,
                str(road_club.get("name") or "").strip() or away_code,
                str((game.get("venue") or {}).get("name") or "").strip() or None,
            )
        )

    if not rows:
        return 0

    with connection.cursor() as cursor:
        cursor.executemany(
            """
            insert into season_schedule_game (
                season_code, gamecode, competition_code, phase_code, phase_name,
                round_number, round_name, played, game_status, utc_date,
                home_team_code, home_team_name, away_team_code, away_team_name,
                venue_name, schedule_refreshed_at
            )
            values (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, now()
            )
            on conflict (season_code, gamecode) do update set
                competition_code = excluded.competition_code,
                phase_code = excluded.phase_code,
                phase_name = excluded.phase_name,
                round_number = excluded.round_number,
                round_name = excluded.round_name,
                played = excluded.played,
                game_status = excluded.game_status,
                utc_date = excluded.utc_date,
                home_team_code = excluded.home_team_code,
                home_team_name = excluded.home_team_name,
                away_team_code = excluded.away_team_code,
                away_team_name = excluded.away_team_name,
                venue_name = excluded.venue_name,
                schedule_refreshed_at = now()
            """,
            rows,
        )
    return len(rows)


def record_season_progress(
    connection: Any,
    season_code: str,
    scheduled_games: int,
) -> None:
    """Record or update the season's scheduled game count and load timestamp."""
    competition_code = competition_for_season_code(season_code)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            insert into season_progress (
                season_code, competition_code, scheduled_games, last_loaded_at
            )
            values (%s, %s, %s, now())
            on conflict (season_code) do update set
                competition_code = excluded.competition_code,
                scheduled_games = excluded.scheduled_games,
                last_loaded_at = excluded.last_loaded_at
            """,
            (season_code, competition_code, scheduled_games),
        )


def assert_live_games_gated(
    connection: Any,
    season_code: str,
    gamecodes: Sequence[int],
) -> dict[str, Any]:
    """Enforce Phase 5 mechanical invariants on warehouse data after deriving new games.

    Runs assert_phase5_reconciles scoped to the newly loaded games to ensure that
    every newly loaded game satisfies exact lineup constraints (5 on court at all
    times, no unpaired substitutions, 200 team minutes per regulation game, and lineup
    possessions within POSSESSION_GATE_TOLERANCE).

    Blind spot / Failure modes not detected:
        This gate verifies database relations and mechanical invariants within the warehouse.
        It does NOT detect subtle scoring-table attribution errors where an on-court player is
        credited with an action performed by a teammate, nor does it detect unplayed games that
        the source API never marked as played in the schedule.
    """
    return assert_phase5_reconciles(connection, season_code, gamecodes=gamecodes)


def run_live_pipeline(
    connection: Any,
    cache: ResponseCache,
    season_code: str,
    *,
    progress: Callable[[str], None] = print,
) -> LiveRunSummary:
    """Load and derive whatever this season has newly played. Idempotent by design.

    Running it twice in a row is a no-op the second time, because the second run
    finds nothing new. That property is what makes a daily schedule safe: a
    retry after a partial failure resumes rather than duplicates.
    """
    schedule = cache.read_schedule_json(season_code)
    schedule_games = list(schedule.get("data") or [])
    already = loaded_gamecodes(connection, season_code)
    new_games = select_new_games(schedule_games, already)
    gamecodes = tuple(int(game["gameCode"]) for game in new_games)

    if schedule_games:
        sync_season_schedule(connection, season_code, schedule_games)
        record_season_progress(connection, season_code, len(schedule_games))

    roster_registrations = 0
    roster_path_for = getattr(cache, "roster_path", None)
    has_roster_snapshot = callable(roster_path_for) and roster_path_for(season_code).is_file()
    if has_roster_snapshot:
        if not gamecodes:
            load_dimensions(connection, build_dimensions(cache, season_code), season_code)
        roster_registrations = load_cached_roster(connection, cache, season_code)

    summary = LiveRunSummary(
        season_code=season_code,
        scheduled=len(schedule_games),
        played=len(played_games(schedule_games)),
        already_loaded=len(already),
        newly_loaded=gamecodes,
        roster_registrations=roster_registrations,
    )

    if not gamecodes:
        progress(summary.as_log_line())
        return summary

    load_new_raw_games(connection, cache, season_code, new_games, progress=progress)
    derive_new_games(connection, cache, season_code, gamecodes)
    assert_live_games_gated(connection, season_code, gamecodes)
    record_cached_game_sources(connection, cache, season_code, gamecodes)
    progress(summary.as_log_line())
    return summary
