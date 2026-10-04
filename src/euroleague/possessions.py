"""Count possessions independently for each team from ordered events."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import Enum

from euroleague.events import EventRecord
from euroleague.free_throws import EventOrderError, group_free_throw_trips


class EventRole(Enum):
    """One event type's role in the approved possession definition."""

    ENDING = "ending"
    CONTINUING = "continuing"
    NO_BALL = "not_touching_the_ball"


EVENT_ROLES = {
    "2FGM": EventRole.ENDING,
    "3FGM": EventRole.ENDING,
    "TO": EventRole.ENDING,
    "D": EventRole.ENDING,
    "FTM": EventRole.ENDING,
    "2FGA": EventRole.CONTINUING,
    "3FGA": EventRole.CONTINUING,
    "FTA": EventRole.CONTINUING,
    "O": EventRole.CONTINUING,
    "IN": EventRole.NO_BALL,
    "OUT": EventRole.NO_BALL,
    "RV": EventRole.NO_BALL,
    "CM": EventRole.NO_BALL,
    "AS": EventRole.NO_BALL,
    "TOUT": EventRole.NO_BALL,
    "TOUT_TV": EventRole.NO_BALL,
    "FV": EventRole.NO_BALL,
    "AG": EventRole.NO_BALL,
    "ST": EventRole.NO_BALL,
    "CCH": EventRole.NO_BALL,
    "OF": EventRole.NO_BALL,
    "CMU": EventRole.NO_BALL,
    "CMU_D": EventRole.NO_BALL,
    "CMU_DI": EventRole.NO_BALL,
    "CMU_FL": EventRole.NO_BALL,
    "CMT": EventRole.NO_BALL,
    "CMT1": EventRole.NO_BALL,
    "C": EventRole.NO_BALL,
    "B": EventRole.NO_BALL,
    "CMD": EventRole.NO_BALL,
    "CMTI": EventRole.NO_BALL,
    "BP": EventRole.NO_BALL,
    "EP": EventRole.NO_BALL,
    "EG": EventRole.NO_BALL,
    "JB": EventRole.NO_BALL,
    # Older seasons only (DECISIONS.md item 85). `TPOFF` names each team's tip-off
    # jumper; the jump itself is the separate `JB` row that follows. `F` and `BF`
    # are fighting and bench-fighting fouls (all 15 in E2022 game 313). None of
    # them moves the ball.
    "TPOFF": EventRole.NO_BALL,
    "F": EventRole.NO_BALL,
    "BF": EventRole.NO_BALL,
}

BALL_TOUCHING_TYPES = frozenset({"2FGM", "3FGM", "TO", "D", "2FGA", "3FGA", "O", "FTM", "FTA"})
POSSESSION_RETAINING_FOUL_TYPES = frozenset(
    {"CMT", "C", "B", "CMU", "CMU_D", "CMU_DI", "CMT1", "CMU_FL"}
)


class UnclassifiedEventTypeError(ValueError):
    """Raised rather than silently ignoring an event outside the vocabulary."""


class PossessionTeamError(ValueError):
    """Raised when a ball event does not belong to either game team."""


POINTS_BY_PLAYTYPE = {"2FGM": 2, "3FGM": 3, "FTM": 1}


@dataclass(frozen=True)
class CountedPossession:
    """One independently observed possession ending."""

    offense_team_code: str
    defense_team_code: str
    start_ingest_index: int
    end_ingest_index: int
    period: int
    end_reason: str
    points_scored: int


@dataclass(frozen=True)
class GamePossessionResult:
    """All observed endings plus totals computed from those rows."""

    teams: tuple[str, str]
    possessions: tuple[CountedPossession, ...]
    # Points from free throws that belong to no possession: technical and
    # unsportsmanlike awards, which are shot while the other team may hold the
    # ball. Excluding them from possession scoring is the standard treatment,
    # but they are real points, so they are reported rather than dropped.
    # Possession points plus these equal the official final score exactly.
    off_possession_points: dict[str, int]

    @property
    def team_counts(self) -> dict[str, int]:
        counts = Counter(possession.offense_team_code for possession in self.possessions)
        return {team: counts[team] for team in self.teams}

    @property
    def team_points(self) -> dict[str, int]:
        """Possession points only. Add `off_possession_points` for the score."""
        scored: Counter[str] = Counter()
        for possession in self.possessions:
            scored[possession.offense_team_code] += possession.points_scored
        return {team: scored[team] for team in self.teams}


@dataclass(frozen=True)
class _FreeThrowContext:
    is_last: bool
    ignored_as_ending: bool
    is_and_one: bool


def _other_team(team_code: str, teams: tuple[str, str]) -> str:
    if team_code == teams[0]:
        return teams[1]
    if team_code == teams[1]:
        return teams[0]
    raise PossessionTeamError(
        f"Ball event team {team_code!r} is not one of the game teams {teams!r}."
    )


def _scorer_received_the_foul(between: Sequence[EventRecord], scorer_id: str | None) -> bool:
    """Did the player who just scored receive a foul before these free throws?"""
    if scorer_id is None:
        return False
    return any(event.playtype == "RV" and event.player_id == scorer_id for event in between)


def _free_throw_contexts(events: Sequence[EventRecord]) -> dict[int, _FreeThrowContext]:
    positions = {event.ingest_index: position for position, event in enumerate(events)}
    contexts: dict[int, _FreeThrowContext] = {}
    for trip in group_free_throw_trips(events):
        first_shot = trip.shots[0].event
        first_position = positions[first_shot.ingest_index]
        previous_ball_event = next(
            (
                event
                for event in reversed(events[:first_position])
                if event.playtype in BALL_TOUCHING_TYPES
            ),
            None,
        )
        is_and_one = (
            previous_ball_event is not None
            and previous_ball_event.playtype in {"2FGM", "3FGM"}
            and previous_ball_event.team_code == first_shot.team_code
            and (
                previous_ball_event.player_id == first_shot.player_id
                # The fouled scorer can leave the court before taking the bonus,
                # and a team-mate then shoots it. Identifying the bonus by the
                # shooter misses that; the `RV` row names who was fouled, which
                # is explicit data rather than an inference. The window is
                # bounded by the basket and the first shot, so a foul in the
                # next possession cannot reach into it.
                or _scorer_received_the_foul(
                    events[positions[previous_ball_event.ingest_index] + 1 : first_position],
                    previous_ball_event.player_id,
                )
            )
        )
        has_retaining_foul = any(
            foul.playtype in POSSESSION_RETAINING_FOUL_TYPES for foul in trip.preceding_fouls
        )
        for shot in trip.shots:
            contexts[shot.event.ingest_index] = _FreeThrowContext(
                is_last=shot.is_last_in_inferred_trip,
                ignored_as_ending=is_and_one or has_retaining_foul,
                is_and_one=is_and_one,
            )
    return contexts


def count_game_possessions(
    events: Sequence[EventRecord],
    home_team: str,
    away_team: str,
) -> GamePossessionResult:
    """Count observed endings without deriving totals from team alternation."""
    teams = (home_team, away_team)
    open_starts: dict[str, tuple[int, int] | None] = {home_team: None, away_team: None}
    possessions: list[CountedPossession] = []
    # Points are accumulated beside the frozen rows so an and-one bonus, which
    # arrives after its possession has already closed at the basket, can still
    # be credited to it. Without that the season understates scoring by every
    # and-one free throw made.
    points: list[int] = []
    open_points: dict[str, int] = {home_team: 0, away_team: 0}
    last_closed: dict[str, int | None] = {home_team: None, away_team: None}
    off_possession_points: dict[str, int] = {home_team: 0, away_team: 0}
    previous_ingest_index: int | None = None
    previous_event: EventRecord | None = None
    free_throw_contexts = _free_throw_contexts(events)
    # A free throw excluded from ending a possession - an and-one bonus, or a
    # technical award - sits outside every possession. So does the rebound of
    # it: the possession it belonged to has already ended, or the ball was
    # never this team's. That rebound starts a possession and ends none.
    previous_ball_was_excluded_free_throw = False

    def close_possession(
        offense_team: str,
        end_event: EventRecord,
        end_reason: str,
        *,
        start_if_missing: bool,
    ) -> None:
        start = open_starts[offense_team]
        if start is None:
            if not start_if_missing:
                return
            start = (end_event.ingest_index, end_event.period)
        possessions.append(
            CountedPossession(
                offense_team_code=offense_team,
                defense_team_code=_other_team(offense_team, teams),
                start_ingest_index=start[0],
                end_ingest_index=end_event.ingest_index,
                period=start[1],
                end_reason=end_reason,
                points_scored=0,
            )
        )
        points.append(open_points[offense_team])
        last_closed[offense_team] = len(possessions) - 1
        open_points[offense_team] = 0
        open_starts[offense_team] = None

    for event in events:
        if previous_ingest_index is not None and event.ingest_index <= previous_ingest_index:
            raise EventOrderError(
                "Event ingest_index values must be strictly increasing in untouched API order."
            )
        previous_ingest_index = event.ingest_index

        role = EVENT_ROLES.get(event.playtype)
        if role is None:
            raise UnclassifiedEventTypeError(
                f"Event type {event.playtype!r} is not classified for possession counting."
            )

        if previous_event is not None and event.period != previous_event.period:
            for team in teams:
                close_possession(
                    team,
                    previous_event,
                    "end_of_period",
                    start_if_missing=False,
                )

        previous_event = event
        if role is EventRole.NO_BALL:
            continue

        if event.playtype in {"FTM", "FTA"}:
            context = free_throw_contexts[event.ingest_index]
            if context.ignored_as_ending:
                previous_ball_was_excluded_free_throw = True
                if event.playtype == "FTM" and event.team_code:
                    if context.is_and_one:
                        # The basket already closed this possession. The bonus
                        # point still belongs to it.
                        closed = last_closed.get(event.team_code)
                        if closed is not None:
                            points[closed] += 1
                        else:
                            open_points[event.team_code] += 1
                    else:
                        off_possession_points[event.team_code] += 1
                continue
            previous_ball_was_excluded_free_throw = False
            if not event.team_code:
                raise PossessionTeamError(
                    f"Ball event {event.playtype} at ingest_index {event.ingest_index} has no team."
                )
            _other_team(event.team_code, teams)
            if open_starts[event.team_code] is None:
                open_starts[event.team_code] = (event.ingest_index, event.period)
            if event.playtype == "FTM":
                open_points[event.team_code] += 1
            if event.playtype == "FTM" and context.is_last:
                close_possession(
                    event.team_code,
                    event,
                    "made_free_throw",
                    start_if_missing=True,
                )
            continue

        rebounding_an_excluded_free_throw = previous_ball_was_excluded_free_throw
        previous_ball_was_excluded_free_throw = False

        if not event.team_code:
            raise PossessionTeamError(
                f"Ball event {event.playtype} at ingest_index {event.ingest_index} has no team."
            )

        if event.playtype == "D":
            if rebounding_an_excluded_free_throw:
                # Rebounding a bonus or technical free throw. Nothing to close:
                # closing here invents a second possession for the team that
                # already ended one at the basket.
                if open_starts[event.team_code] is None:
                    open_starts[event.team_code] = (event.ingest_index, event.period)
                continue
            offense_team = _other_team(event.team_code, teams)
            close_possession(
                offense_team,
                event,
                "defensive_rebound",
                start_if_missing=True,
            )
            if open_starts[event.team_code] is None:
                open_starts[event.team_code] = (event.ingest_index, event.period)
            continue

        _other_team(event.team_code, teams)
        if open_starts[event.team_code] is None:
            open_starts[event.team_code] = (event.ingest_index, event.period)

        open_points[event.team_code] += POINTS_BY_PLAYTYPE.get(event.playtype, 0)

        if event.playtype not in {"2FGM", "3FGM", "TO"}:
            continue

        close_possession(
            event.team_code,
            event,
            "turnover" if event.playtype == "TO" else "made_shot",
            start_if_missing=True,
        )

    if previous_event is not None:
        for team in teams:
            close_possession(
                team,
                previous_event,
                "end_of_period",
                start_if_missing=False,
            )

    scored = tuple(
        replace(possession, points_scored=points[position])
        for position, possession in enumerate(possessions)
    )
    return GamePossessionResult(
        teams=teams,
        possessions=scored,
        off_possession_points=off_possession_points,
    )
