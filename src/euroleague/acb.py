"""Minimal source-native ACB contracts for domestic player-game data.

This module deliberately stops before database persistence. It knows how to
address ACB's public matchdata endpoints and how to turn one response into
source-native rows. Original ACB identifiers are preserved unchanged so a later
identity layer can link them to EuroLeague ids without rewriting either source.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ACB_API_BASE = "https://api2.acb.com"
ACB_PUBLIC_API_KEY = "0dd94928-6f57-4c08-a3bd-b1b2f092976e"

_STARTER_PLAY_TYPE = 599
_SUB_IN_PLAY_TYPE = 112
_SUB_OUT_PLAY_TYPE = 115


@dataclass(frozen=True)
class AcbPlayerGame:
    match_id: str
    source_player_id: str
    team_source_id: str
    display_name: str
    jersey_number: str | None
    is_starter: bool
    minutes_seconds: int
    points: int
    two_made: int
    two_attempted: int
    three_made: int
    three_attempted: int
    free_throw_made: int
    free_throw_attempted: int
    offensive_rebounds: int
    defensive_rebounds: int
    assists: int
    steals: int
    turnovers: int
    blocks: int
    fouls_committed: int
    fouls_received: int
    plus_minus: int
    valuation: int

    @property
    def field_goals_attempted(self) -> int:
        return self.two_attempted + self.three_attempted


@dataclass(frozen=True)
class AcbPlay:
    match_id: str
    ingest_index: int
    source_order: int | None
    play_type: int | None
    event_kind: str
    source_player_id: str | None
    local: bool | None
    quarter: int | None
    minute: int | None
    second: int | None
    score_home: int | None
    score_away: int | None


def build_acb_boxscore_url(match_id: str) -> str:
    match = _source_id(match_id, "match_id")
    return f"{ACB_API_BASE}/api/matchdata/Result/boxscores?matchId={match}"


def build_acb_play_by_play_url(match_id: str) -> str:
    match = _source_id(match_id, "match_id")
    return f"{ACB_API_BASE}/api/matchdata/PlayByPlay/play-by-play?matchId={match}"


def acb_request_headers(api_key: str | None = None) -> dict[str, str]:
    """Return the headers ACB's own public frontend uses for JSON requests."""
    key = (api_key or ACB_PUBLIC_API_KEY).strip()
    if not key:
        raise ValueError("ACB API key must not be empty.")
    return {"x-apikey": key, "Accept": "application/json"}


def parse_acb_boxscore(match_id: str, payload: dict[str, Any]) -> tuple[AcbPlayerGame, ...]:
    """Parse whole-game player lines from one ACB box-score response."""
    match = _source_id(match_id, "match_id")
    if payload.get("matchFinished") is not True:
        raise ValueError(f"ACB match {match} is not marked finished.")

    rows: list[AcbPlayerGame] = []
    for team_box in payload.get("teamBoxscores") or []:
        team = team_box.get("team") or {}
        team_source_id = _source_id(team.get("clubId") or team.get("id"), "team id")
        players = _whole_game_players(team_box)
        for line in players:
            player = line.get("player") or {}
            source_player_id = _source_id(player.get("id"), "player id")
            first = _trim(player.get("firstName"))
            last = _trim(player.get("lastName"))
            display_name = " ".join(part for part in (first, last) if part)
            if not display_name:
                display_name = _trim(player.get("nickname")) or source_player_id

            rows.append(
                AcbPlayerGame(
                    match_id=match,
                    source_player_id=source_player_id,
                    team_source_id=team_source_id,
                    display_name=display_name,
                    jersey_number=_nullable_source_id(player.get("shirtNumber")),
                    is_starter=line.get("isStarted") is True,
                    minutes_seconds=_minutes_seconds(line.get("playTime")),
                    points=_integer(line.get("points")),
                    two_made=_integer(line.get("twoPointersMade")),
                    two_attempted=_integer(line.get("twoPointersAttempted")),
                    three_made=_integer(line.get("threePointersMade")),
                    three_attempted=_integer(line.get("threePointersAttempted")),
                    free_throw_made=_integer(line.get("freeThrowsMade")),
                    free_throw_attempted=_integer(line.get("freeThrowsAttempted")),
                    offensive_rebounds=_integer(line.get("offRebounds")),
                    defensive_rebounds=_integer(line.get("defRebounds")),
                    assists=_integer(line.get("assists")),
                    steals=_integer(line.get("steals")),
                    turnovers=_integer(line.get("turnovers")),
                    blocks=_integer(line.get("blocks")),
                    fouls_committed=_integer(line.get("personalFouls")),
                    fouls_received=_integer(line.get("foulsReceived", line.get("foulsDrawn"))),
                    plus_minus=_signed_integer(line.get("plusMinus", line.get("plusMinusPoints"))),
                    valuation=_signed_integer(line.get("valuation", line.get("rating"))),
                )
            )
    return tuple(rows)


def parse_acb_play_by_play(match_id: str, payload: dict[str, Any]) -> tuple[AcbPlay, ...]:
    """Preserve the ACB play array exactly as received and attach ingest_index."""
    match = _source_id(match_id, "match_id")
    plays = payload.get("plays")
    if not isinstance(plays, list):
        raise ValueError("ACB play-by-play payload must contain a plays list.")

    rows: list[AcbPlay] = []
    for ingest_index, play in enumerate(plays):
        play_type = _nullable_integer(play.get("playType"))
        rows.append(
            AcbPlay(
                match_id=match,
                ingest_index=ingest_index,
                source_order=_nullable_integer(play.get("order")),
                play_type=play_type,
                event_kind=_event_kind(play_type),
                source_player_id=_nullable_source_id(play.get("playerLicenseId")),
                local=_nullable_boolean(play.get("local")),
                quarter=_nullable_integer(play.get("quarter")),
                minute=_nullable_integer(play.get("minute")),
                second=_nullable_integer(play.get("second")),
                score_home=_nullable_integer(play.get("scoreHome")),
                score_away=_nullable_integer(play.get("scoreAway")),
            )
        )
    return tuple(rows)


def _whole_game_players(team_box: dict[str, Any]) -> list[dict[str, Any]]:
    for period in team_box.get("statsByPeriods") or []:
        if _nullable_integer(period.get("quarter")) == 0:
            stats = period.get("stats") or {}
            players = stats.get("players") or []
            if not isinstance(players, list):
                raise ValueError("ACB quarter-0 players must be a list.")
            return players
    raise ValueError("ACB box score has no quarter-0 whole-game player block.")


def _event_kind(play_type: int | None) -> str:
    if play_type == _STARTER_PLAY_TYPE:
        return "starter"
    if play_type == _SUB_IN_PLAY_TYPE:
        return "sub_in"
    if play_type == _SUB_OUT_PLAY_TYPE:
        return "sub_out"
    return "source_event"


def _trim(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _source_id(value: Any, label: str) -> str:
    result = _trim(value)
    if result is None:
        raise ValueError(f"ACB {label} is missing.")
    return result


def _nullable_source_id(value: Any) -> str | None:
    return _trim(value)


def _integer(value: Any) -> int:
    if value is None or value == "":
        return 0
    return max(int(value), 0)


def _signed_integer(value: Any) -> int:
    if value is None or value == "":
        return 0
    return int(value)


def _nullable_integer(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _nullable_boolean(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    raise ValueError(f"Expected bool or null, found {value!r}.")


def _minutes_seconds(value: Any) -> int:
    text = _trim(value)
    if text is None:
        return 0
    if ":" not in text:
        return int(text) * 60
    minutes, seconds = text.split(":", 1)
    minute_value = int(minutes)
    second_value = int(seconds)
    if minute_value < 0 or not 0 <= second_value < 60:
        raise ValueError(f"Invalid ACB playTime {text!r}.")
    return minute_value * 60 + second_value
