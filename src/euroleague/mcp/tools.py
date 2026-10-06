"""The fifteen tool definitions.

Descriptions are read by the model at call time, so they are written as prompts
rather than as code comments: what the tool answers, what the numbers mean, and
what they do not mean. A tool whose description omits that a number is inferred
will have that number quoted as though it were measured.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from euroleague.mcp import model_features, queries
from euroleague.mcp.envelope import RESPONSE_OUTPUT_SCHEMA
from euroleague.mcp.protocol import Tool

TOOL_NAMES: tuple[str, ...] = (
    "el_describe_warehouse",
    "el_find_games",
    "el_get_game",
    "el_get_boxscore",
    "el_get_team_stats",
    "el_get_player_stats",
    "el_get_player_model_context",
    "el_get_lineup_stats",
    "el_get_player_on_off",
    "el_get_possessions",
    "el_get_play_by_play",
    "el_get_shot_data",
    "el_get_fouls",
    "el_get_referee_stats",
    "el_get_roster",
    "el_acb_find_games",
    "el_acb_get_player_games",
    "el_acb_get_play_by_play",
)

_INCLUDE_QUARANTINED = {
    "type": "boolean",
    "default": False,
    "description": (
        "Include games excluded by default for failing a validation invariant. "
        "Leave false unless you specifically want to inspect the failures; if you set "
        "it true, say so when quoting the result."
    ),
}

_SEASON = {
    "type": "string",
    "description": (
        "Season code such as E2024. E<YYYY> identifies the season starting in autumn <YYYY> "
        "(for example, E2024 is the 2024-25 season). Call el_describe_warehouse to see "
        "which seasons are loaded."
    ),
}

_ACB_SEASON = {
    "type": "string",
    "description": ("ACB season such as 2025-26, meaning the season ending in spring 2026."),
}

_LIMIT = {
    "type": "integer",
    "description": (
        f"Maximum rows to return. Default {queries.DEFAULT_LIMIT}, maximum {queries.MAX_LIMIT}."
    ),
}

_OFFSET = {
    "type": "integer",
    "description": (
        "Rows to skip, for paging through a large result. Use next_offset from the "
        f"previous response. Offsets over {queries.MAX_PAGINATION_OFFSET:,} are refused; "
        "narrow the query before paging further."
    ),
}

# The two largest surfaces, and the arguments that turn a sweep into a question.
# For el_get_play_by_play the published schema already requires gamecode, and both
# transports check that first; this entry is the backstop for any caller path that
# reaches a handler directly.
_BULK_NARROWING_ARGUMENTS = {
    "el_get_play_by_play": ("gamecode",),
    "el_get_shot_data": ("gamecode", "team", "player", "period", "made", "shot_type"),
}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    """Every tool's schema, with include_quarantined added for free."""
    return {
        "type": "object",
        "properties": {**properties, "include_quarantined": _INCLUDE_QUARANTINED},
        "required": required or [],
    }


def _validate_booleans(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    """Ensure any schema-defined boolean argument present is a literal bool."""
    properties = schema.get("properties", {})
    for name, prop in properties.items():
        if prop.get("type") == "boolean" and name in arguments:
            queries._boolean(arguments, name)


def _validate_pagination_depth(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    """Reject deep pages before a tool runner can acquire a database connection."""
    if "offset" in schema.get("properties", {}):
        queries.validate_offset(arguments.get("offset"))


def _has_narrowing_value(arguments: dict[str, Any], name: str) -> bool:
    """Treat false as a valid Boolean filter but reject absent or blank values."""
    value = arguments.get(name)
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _validate_bulk_narrowing(tool_name: str, arguments: dict[str, Any]) -> None:
    """Keep the two largest surfaces focused before a database runner is selected."""
    narrowing_arguments = _BULK_NARROWING_ARGUMENTS.get(tool_name)
    if narrowing_arguments is None:
        return
    if any(_has_narrowing_value(arguments, name) for name in narrowing_arguments):
        return
    names = ", ".join(narrowing_arguments)
    raise ValueError(
        f"{tool_name} needs at least one narrowing argument: {names}. "
        "Narrow the query, then page within that focused result."
    )


def build_registry(
    runner: Callable[
        [Callable[[Any, dict[str, Any]], dict[str, Any]], dict[str, Any]], dict[str, Any]
    ],
) -> dict[str, Tool]:
    """Bind each query function to the supplied query runner."""

    def bind(
        tool_name: str, query: Callable[[Any, dict], dict], schema: dict[str, Any]
    ) -> Callable[[dict], dict]:
        def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            _validate_booleans(schema, arguments)
            _validate_pagination_depth(schema, arguments)
            _validate_bulk_narrowing(tool_name, arguments)
            return runner(query, arguments)

        return handler

    def tool(
        name: str,
        title: str,
        description: str,
        input_schema: dict[str, Any],
        query: Callable[[Any, dict], dict],
    ) -> Tool:
        return Tool(
            name=name,
            title=title,
            description=description,
            input_schema=input_schema,
            output_schema=RESPONSE_OUTPUT_SCHEMA,
            handler=bind(name, query, input_schema),
        )

    tools = [
        tool(
            name="el_describe_warehouse",
            title="Warehouse coverage and quality",
            description=(
                "Call this FIRST. Reports which seasons are loaded, how many games each "
                "holds, whether each is complete, in progress, or of unknown completeness, "
                "the date range covered, which games are excluded by default and "
                "why, and the teams in each season. Season codes follow the E<YYYY> convention "
                "for the season starting in autumn <YYYY> (for example, E2024 is the 2024-25 "
                "season). Counting statistics served by the other tools are the official "
                "euroleague.net box score; possessions, pace, lineups, on/off and every "
                "per-100 rate are this project's own reconstruction from play-by-play "
                "events. Shot-coordinate availability is reported by season. Use this before "
                "assuming any season, team or coordinate coverage is available."
            ),
            input_schema=_schema({}),
            query=queries.describe_warehouse,
        ),
        tool(
            name="el_find_games",
            title="Find games",
            description=(
                "Find games matching a season, team, opponent, date range, phase or "
                "round, and return their gamecodes with the official final score. Use "
                "this to turn a description of a game into the gamecode that el_get_game "
                "and el_get_play_by_play need. Teams may be given as a three-letter code "
                "such as PAN or as a club name. Results are paginated: read row_count and "
                "next_offset rather than assuming you received everything."
            ),
            input_schema=_schema(
                {
                    "season": _SEASON,
                    "team": {
                        "type": "string",
                        "description": "Team code or club name. Matches home or away.",
                    },
                    "opponent": {
                        "type": "string",
                        "description": "A second team, to find the meetings between the two.",
                    },
                    "from_date": {
                        "type": "string",
                        "description": "Earliest game date, ISO format YYYY-MM-DD.",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "Latest game date, ISO format YYYY-MM-DD.",
                    },
                    "phase": {
                        "type": "string",
                        "description": (
                            "Phase code, such as RS for regular season or PO for playoffs."
                        ),
                    },
                    "round_number": {
                        "type": "integer",
                        "description": "Round number within the phase.",
                    },
                    "limit": _LIMIT,
                    "offset": _OFFSET,
                },
                required=["season"],
            ),
            query=queries.find_games,
        ),
        tool(
            name="el_get_game",
            title="One game in full",
            description=(
                "One game's two team lines side by side: the official box score totals, "
                "the four factors (effective field goal percentage, turnover rate, "
                "offensive rebound rate, free throw rate), exact possession counts, and "
                "offensive and defensive rating per 100 possessions. Possessions are "
                "counted from the event stream, never estimated from a box score formula. "
                "Defensive rating uses the opponent's possessions as its denominator. "
                "The officiating crew is the published assignment, not derived by this "
                "project. Get the gamecode from el_find_games."
            ),
            input_schema=_schema(
                {
                    "season": _SEASON,
                    "gamecode": {
                        "type": "integer",
                        "description": "The gamecode, unique within a season. From el_find_games.",
                    },
                },
                required=["season", "gamecode"],
            ),
            query=queries.get_game,
        ),
        tool(
            name="el_get_boxscore",
            title="Single game player box score",
            description=(
                "A single game's full player box score for both teams alongside team totals: "
                "points, field goals, three pointers, free throws, offensive/defensive/total "
                "rebounds, assists, steals, turnovers, blocks, fouls, valuation, and plus-minus. "
                "Counting statistics are the official published euroleague.net box score, "
                "never aggregated from events. Minutes are reported according to minutes_basis "
                "(default 'corrected'). Get the gamecode from el_find_games."
            ),
            input_schema=_schema(
                {
                    "season": _SEASON,
                    "gamecode": {
                        "type": "integer",
                        "description": "The gamecode, unique within a season. From el_find_games.",
                    },
                    "minutes_basis": {
                        "type": "string",
                        "enum": ["corrected", "raw", "official"],
                        "default": "corrected",
                        "description": (
                            "Which minutes reconstruction to return. 'corrected' (default) "
                            "applies the substitution duration correction. 'raw' uses "
                            "unadjusted source timestamps. 'official' uses the minutes published "
                            "in the official box score."
                        ),
                    },
                },
                required=["season", "gamecode"],
            ),
            query=queries.get_boxscore,
        ),
        tool(
            name="el_get_team_stats",
            title="Team season profile",
            description=(
                "A team's season profile: the four factors, offensive and defensive "
                "rating per 100 possessions, and possessions per game. Possessions are "
                "counted exactly from play-by-play events, never estimated from a box "
                "score formula, which is what makes these ratings comparable across "
                "teams that play at different speeds. Omit the team argument to get "
                "every team in the season, ranked by offensive rating. For a clutch "
                "split, pass BOTH clutch_max_seconds_remaining and clutch_max_margin - "
                "there is no default, because definitions of clutch differ."
            ),
            input_schema=_schema(
                {
                    "season": _SEASON,
                    "team": {
                        "type": "string",
                        "description": "Team code or club name. Omit for every team in the season.",
                    },
                    "clutch_max_seconds_remaining": {
                        "type": "integer",
                        "description": (
                            "Restrict to possessions starting with at most this many seconds "
                            "left in the game. 300 is the last five minutes. Must be given "
                            "with clutch_max_margin."
                        ),
                    },
                    "clutch_max_margin": {
                        "type": "integer",
                        "description": (
                            "Restrict to possessions starting within this many points either "
                            "way. Must be given with clutch_max_seconds_remaining."
                        ),
                    },
                },
                required=["season"],
            ),
            query=queries.get_team_stats,
        ),
        tool(
            name="el_get_player_stats",
            title="Player season line",
            description=(
                "A player's season totals or per-game averages. Counting statistics are "
                "the official euroleague.net box score. Minutes are this project's "
                "reconstruction and the response states which kind it served: "
                "'corrected' is the default and applies a measured 60-second "
                "substitution correction, 'raw' uses the source timestamps untouched, "
                "'official' is the published figure. Always repeat that basis when you "
                "quote a minutes figure or any per-minute rate. Omit the player argument "
                "to rank a team or a whole season."
            ),
            input_schema=_schema(
                {
                    "season": _SEASON,
                    "player": {
                        "type": "string",
                        "description": (
                            "Player id such as P012774, or a name. Names are stored "
                            "'SURNAME, FORENAME'; a surname alone usually works. An "
                            "ambiguous name returns the candidates rather than a guess."
                        ),
                    },
                    "team": {"type": "string", "description": "Team code or club name."},
                    "per_game": {
                        "type": "boolean",
                        "default": False,
                        "description": "True for per-game averages, false for season totals.",
                    },
                    "minutes_basis": {
                        "type": "string",
                        "enum": ["corrected", "raw", "official"],
                        "default": "corrected",
                        "description": "Which minutes reconstruction to serve. Default corrected.",
                    },
                    "min_seconds": {
                        "type": "integer",
                        "description": "Drop players below this many total seconds played.",
                    },
                    "limit": _LIMIT,
                    "offset": _OFFSET,
                },
                required=["season"],
            ),
            query=queries.get_player_stats,
        ),
        tool(
            name="el_get_player_model_context",
            title="Compact pre-game player model context",
            description=(
                "Start a player-points analysis here instead of chaining many box-score and "
                "play-by-play calls. Returns one compact server-side bundle with the "
                "leakage-safe simple scoring baseline, L3/L5/L10 minutes, points, FGA, 3PA "
                "and FTA, starter rates, per-minute rates, recent "
                "game rows, plus full-PBP-derived on-court possessions, ratings, stint and "
                "lineup-concentration signals, and optional opponent L5 profile. For an "
                "upcoming game, supply gamecode to add leakage-safe 72h injury/availability "
                "context, source-confidence weighting, teammate vacated minutes/FGA, "
                "teammate pair associations, strict-sample three-player combinations and "
                "exact five-man lineup context (with/without scoring role, shared minutes and "
                "on-court ratings), canonical-identity ACB L3/L5 form, and "
                "nominal-city air-travel distance from recent EuroLeague venues; the opponent is "
                "inferred from that target game unless "
                "explicitly overridden. Supply as_of_date for backtests: every rolling number then "
                "uses only games strictly before that date, preventing look-ahead leakage. "
                "Raw play-by-play remains available through el_get_play_by_play for drill-down "
                "only. The bookmaker line is deliberately not part of this projection context."
            ),
            input_schema=_schema(
                {
                    "season": _SEASON,
                    "player": {
                        "type": "string",
                        "description": "Player id such as P009862, or a player name.",
                    },
                    "opponent": {
                        "type": "string",
                        "description": (
                            "Optional upcoming opponent by team code or club name. When given, "
                            "the same response adds the opponent's previous-five-game profile. "
                            "When gamecode is supplied, the target game's opponent is inferred "
                            "automatically unless this value overrides it."
                        ),
                    },
                    "gamecode": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Optional target EuroLeague gamecode. Adds the archived 72-hour "
                            "pregame role context for this player, including OUT/DOUBT/RETURN "
                            "signals and confidence-weighted teammate vacated minutes/FGA."
                        ),
                    },
                    "as_of_date": {
                        "type": "string",
                        "description": (
                            "Optional ISO date YYYY-MM-DD. Only games strictly before this date "
                            "may enter rolling features; use it for every historical backtest."
                        ),
                    },
                    "lookback": {
                        "type": "integer",
                        "default": 10,
                        "description": (
                            "Number of recent games included in the compact recent_games array. "
                            "Must be between 10 and 20; L3/L5/L10 features stay fixed."
                        ),
                    },
                    "minutes_basis": {
                        "type": "string",
                        "enum": ["corrected", "raw", "official"],
                        "default": "official",
                        "description": (
                            "Minutes source used by every minutes-derived feature. Default "
                            "official for stable historical player-points modelling."
                        ),
                    },
                },
                required=["season", "player"],
            ),
            query=model_features.get_player_model_context,
        ),
        tool(
            name="el_get_lineup_stats",
            title="Five-man unit performance",
            description=(
                "Five-man units ranked by net rating per 100 possessions, with points "
                "scored and allowed on their own possessions. Reconstructed from "
                "substitution events, since the API publishes no lineup data - which is "
                "why lineups carry no external ground truth and are validated by "
                "mechanical invariants instead. Filter with contains_player to find every "
                "unit a player appeared in. Raise min_possessions before drawing any "
                "conclusion: a unit with 30 possessions is noise. A possession that spans "
                "a substitution is credited to the unit on court when it started, which "
                "the response reports as a measured rate."
            ),
            input_schema=_schema(
                {
                    "season": _SEASON,
                    "team": {"type": "string", "description": "Team code or club name."},
                    "contains_player": {
                        "type": "string",
                        "description": "Only units containing this player, by id or name.",
                    },
                    "min_possessions": {
                        "type": "integer",
                        "default": 25,
                        "description": (
                            "Drop units below this many offensive possessions. Default 25. "
                            "Raise it - lineup samples are small and noisy."
                        ),
                    },
                    "limit": _LIMIT,
                    "offset": _OFFSET,
                },
                required=["season"],
            ),
            query=queries.get_lineup_stats,
        ),
        tool(
            name="el_get_player_on_off",
            title="On/off split",
            description=(
                "How a team performed with one player on the floor against without him: "
                "possessions, points, and offensive, defensive and net rating per 100 "
                "for each split. This is a team measurement taken while the player was "
                "present, NOT a measure of the player's individual value - it depends on "
                "his teammates and on the opponent's units. The off split includes games "
                "he did not play. Pass team for a player who appeared for more than one "
                "club in the season. Pass max_seconds_remaining and max_margin to restrict "
                "both sides of the split to clutch possessions - the warehouse bakes in no "
                "definition of clutch, so state yours."
            ),
            input_schema=_schema(
                {
                    "season": _SEASON,
                    "player": {
                        "type": "string",
                        "description": "Player id such as P012774, or a name.",
                    },
                    "team": {
                        "type": "string",
                        "description": "Restrict to one club, for a player who moved mid-season.",
                    },
                    "max_seconds_remaining": {
                        "type": "integer",
                        "description": (
                            "Possessions starting with at most this many seconds left in "
                            "the game. 300 is the last five minutes of a 40-minute game."
                        ),
                    },
                    "max_margin": {
                        "type": "integer",
                        "description": "Possessions starting within this many points either way.",
                    },
                },
                required=["season", "player"],
            ),
            query=queries.get_player_on_off,
        ),
        tool(
            name="el_get_possessions",
            title="Possessions, filtered",
            description=(
                "Individual possessions or their aggregate, filtered by game, team, "
                "lineup, score margin, time remaining or how the possession ended. This "
                "is how you answer any clutch question: pass max_seconds_remaining and "
                "max_margin to state YOUR definition of clutch - the warehouse bakes in "
                "none, because analysts disagree and the definition drifts. Possession "
                "length is served in seconds; a transition or fast-break definition is "
                "the caller's threshold on max_duration_seconds, as clutch is on time "
                "and margin. Possessions are counted exactly from play-by-play events; "
                "never compare the count with a box score estimate, which measures "
                "something different. Set aggregate=true for one summary row per team "
                "instead of the raw rows."
            ),
            input_schema=_schema(
                {
                    "season": _SEASON,
                    "gamecode": {"type": "integer", "description": "Restrict to one game."},
                    "team": {
                        "type": "string",
                        "description": "Restrict to possessions where this team had the ball.",
                    },
                    "lineup_id": {
                        "type": "string",
                        "description": "Restrict to one five-man unit, from el_get_lineup_stats.",
                    },
                    "max_seconds_remaining": {
                        "type": "integer",
                        "description": (
                            "Possessions starting with at most this many seconds left in "
                            "the game. 300 is the last five minutes of a 40-minute game."
                        ),
                    },
                    "max_margin": {
                        "type": "integer",
                        "description": "Possessions starting within this many points either way.",
                    },
                    "max_duration_seconds": {
                        "type": "integer",
                        "description": (
                            "Restrict to possessions lasting at most this many seconds, "
                            "from the first event to the last. This is the caller's own "
                            "threshold for a transition or fast-break possession - the "
                            "warehouse bakes in no fixed definition, the same way clutch "
                            "is a caller threshold on time and margin rather than a "
                            "stored flag. About 0.3% of possessions carry a negative "
                            "duration because the source game clock is measured to run "
                            "backwards by up to a minute around some substitutions; those "
                            "rows pass this filter at any positive threshold. Rows loaded "
                            "before the possession-seconds rebuild have null seconds and "
                            "are excluded by this filter; el_describe_warehouse's coverage "
                            "does not yet report that state."
                        ),
                    },
                    "end_reason": {
                        "type": "string",
                        "enum": [
                            "made_shot",
                            "defensive_rebound",
                            "turnover",
                            "end_of_period",
                            "made_free_throw",
                            "other",
                        ],
                        "description": (
                            "Restrict to possessions that ended this way. Every possession "
                            "carries exactly one of the five real values (made_shot, "
                            "defensive_rebound, turnover, made_free_throw, end_of_period); "
                            "'other' is a reserved safety net that the measured data has "
                            "never populated. Pass aggregate=true with "
                            "aggregate_by='end_reason' to see the full breakdown at once."
                        ),
                    },
                    "aggregate": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "True for one summary row per team instead of raw possessions."
                        ),
                    },
                    "aggregate_by": {
                        "type": "string",
                        "enum": ["team", "end_reason", "team_and_end_reason"],
                        "default": "team",
                        "description": (
                            "Only valid with aggregate=true; passing it with aggregate=false "
                            "is rejected. 'team' (default): one row per team. 'end_reason': "
                            "one row per way possessions ended, with share_of_all_possessions "
                            "out of every possession in the filtered set. "
                            "'team_and_end_reason': one row per team and end reason, with "
                            "share_of_team_possessions out of that team's possessions only."
                        ),
                    },
                    "limit": _LIMIT,
                    "offset": _OFFSET,
                },
                required=["season"],
            ),
            query=queries.get_possessions,
        ),
        tool(
            name="el_get_play_by_play",
            title="Event stream with lineups",
            description=(
                "One game's play-by-play events with the five players on the floor for "
                "both teams attached to every row, plus the stint and possession each "
                "event belongs to. Rows come back in source order by ingest_index, which "
                "is the only trustworthy ordering this data has - do not re-sort them. "
                "Use it to see what actually happened in a stretch of a game rather than "
                "a summary of it. Always narrow this tool with gamecode from el_find_games, "
                "then paginate with from_index or offset; a full game is roughly 450 to "
                "700 events. Timeouts are events too: playtype TOUT is a team timeout, "
                "TOUT_TV a television timeout with no team, CCH a coach's challenge; filter "
                "by playtype to list them with their clock."
            ),
            input_schema=_schema(
                {
                    "season": _SEASON,
                    "gamecode": {
                        "type": "integer",
                        "description": "The gamecode, from el_find_games.",
                    },
                    "period": {
                        "type": "integer",
                        "description": "1 to 4 for quarters, 5 and above for overtime periods.",
                    },
                    "playtype": {
                        "type": "string",
                        "description": (
                            "Restrict to one event code, such as 2FGM made two, 3FGA missed "
                            "three, TO turnover, D defensive rebound, O offensive rebound, "
                            "CM personal foul, OF offensive foul."
                        ),
                    },
                    "from_index": {
                        "type": "integer",
                        "description": (
                            "Start at this ingest_index. Use it to continue a previous page."
                        ),
                    },
                    "limit": _LIMIT,
                    "offset": _OFFSET,
                },
                required=["season", "gamecode"],
            ),
            query=queries.get_play_by_play,
        ),
        tool(
            name="el_get_shot_data",
            title="Shot attempts and locations",
            description=(
                f"Shot attempts with optional court coordinates, paginated at default "
                f"{queries.DEFAULT_LIMIT} and hard maximum {queries.MAX_LIMIT} rows. The "
                "population ALWAYS starts from game_event, so made and missed free throws "
                "remain complete. raw_shot is left-joined only to attach coord_x, coord_y "
                "and zone: it holds made free throws but omits every missed free throw, and "
                "all of its free throws use the (-1,-1) null sentinel. This tool returns "
                "free throws with no coordinates and never serves that sentinel as a "
                "location. Shot type comes from the event action code, never from distance "
                "or coordinate geometry. The response distinguishes no matching shots from "
                "a season with no coordinate coverage. Always narrow this tool with at least "
                "one of gamecode, team, player, period, made, or shot_type before paging."
            ),
            input_schema=_schema(
                {
                    "season": _SEASON,
                    "gamecode": {"type": "integer", "description": "Restrict to one game."},
                    "team": {
                        "type": "string",
                        "description": "Restrict to one team, by code or club name.",
                    },
                    "player": {
                        "type": "string",
                        "description": "Restrict to one player, by opaque id or name.",
                    },
                    "period": {
                        "type": "integer",
                        "description": "1 to 4 for quarters, 5 and above for overtime.",
                    },
                    "made": {
                        "type": "boolean",
                        "description": "True for makes, false for misses; omit for both.",
                    },
                    "shot_type": {
                        "type": "string",
                        "enum": ["2P", "3P", "FT"],
                        "description": (
                            "Two-pointer, three-pointer or free throw. Read from the action "
                            "code, never inferred from coordinates or distance."
                        ),
                    },
                    "only_with_real_coordinates": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Return only rows with a real court coordinate. This removes "
                            "free throws and the nine E2024 field goals published at the "
                            "(-1,-1) null sentinel."
                        ),
                    },
                    "limit": _LIMIT,
                    "offset": _OFFSET,
                },
                required=["season"],
            ),
            query=queries.get_shot_data,
        ),
        tool(
            name="el_get_fouls",
            title="Fouls by type",
            description=(
                "Fouls committed and drawn, split by type and grouped by player, team "
                "or game. Types come straight from the event stream's foul codes: CM "
                "personal, OF offensive, CMU unsportsmanlike, CMT technical, CMD "
                "disqualifying, CMTI throw-in, C coach, B bench, and RV for a foul "
                "drawn. The committed total reconciles exactly to the official box "
                "score. Use foul_type to isolate one code, for example offensive fouls "
                "by player, or technicals by team. Shooting-versus-non-shooting is not "
                "in the data and is never guessed."
            ),
            input_schema=_schema(
                {
                    "season": _SEASON,
                    "team": {"type": "string", "description": "Restrict to one team's fouls."},
                    "player": {
                        "type": "string",
                        "description": "Restrict to one player, by id or by name.",
                    },
                    "gamecode": {"type": "integer", "description": "Restrict to one game."},
                    "foul_type": {
                        "type": "string",
                        "enum": ["CM", "OF", "CMU", "CMT", "C", "B", "CMD", "CMTI", "RV"],
                        "description": "One foul code, or RV for fouls drawn.",
                    },
                    "group_by": {
                        "type": "string",
                        "enum": ["player", "team", "game"],
                        "default": "player",
                        "description": (
                            "One row per player, per team, or per team per game. Coach "
                            "and bench fouls appear only in team and game groupings."
                        ),
                    },
                    "limit": _LIMIT,
                    "offset": _OFFSET,
                },
                required=["season"],
            ),
            query=queries.get_fouls,
        ),
        tool(
            name="el_get_referee_stats",
            title="Referee season aggregates",
            description=(
                "A referee's season: games worked, fouls per game (overall, home, away), "
                "home-win rate, and pace, averaged over every game the referee worked. This "
                "is a descriptive summary, not a causal claim: teams, venues, opponents, and "
                "who else was on the crew are not controlled for, so a high or low figure "
                "does not by itself mean the referee causes it. Quote the games count beside "
                "any figure. Keyed on the schedule's stable referee code; a name published in "
                "the box score with no matching schedule code is dropped and disclosed."
            ),
            input_schema=_schema(
                {
                    "season": _SEASON,
                    "referee": {
                        "type": "string",
                        "description": "Restrict to one referee, by code or by a name substring.",
                    },
                    "limit": _LIMIT,
                    "offset": _OFFSET,
                },
                required=["season"],
            ),
            query=queries.get_referee_stats,
        ),
        tool(
            name="el_get_roster",
            title="Team roster with biography",
            description=(
                "A team's roster: every player who reached a box score that season, with "
                "biography (jersey number, position, height, weight, birth date, age on "
                "1 October of the season's first year, country) from the league's own "
                "registration feed. The biography is attached through the observed-stat-line "
                "link, never by matching names, so it can be missing for a player the link "
                "did not find; a null biography still means the player appeared in the box "
                "score. games_played counts box score rows, not necessarily minutes played. "
                "Roster rows count every box-score appearance, quarantined games included; "
                "include_quarantined changes only the coverage and exclusion notes."
            ),
            input_schema=_schema(
                {
                    "season": _SEASON,
                    "team": {
                        "type": "string",
                        "description": "Restrict to one team, by code or by club name.",
                    },
                    "player": {
                        "type": "string",
                        "description": "Restrict to one player, by id or by name.",
                    },
                    "limit": _LIMIT,
                    "offset": _OFFSET,
                },
                required=["season"],
            ),
            query=queries.get_roster,
        ),
        tool(
            name="el_acb_find_games",
            title="Find ACB Liga Endesa games",
            description=(
                "Find source-native Liga Endesa games in the ACB warehouse. "
                "Use season like 2025-26. Only competition_id=1 is included, so Liga U, "
                "Minicopa, Copa del Rey and Supercopa are excluded from this tool."
            ),
            input_schema=_schema(
                {
                    "season": _ACB_SEASON,
                    "team": {"type": "string", "description": "Optional team name substring."},
                    "from_date": {
                        "type": "string",
                        "description": "Earliest game date, YYYY-MM-DD.",
                    },
                    "to_date": {"type": "string", "description": "Latest game date, YYYY-MM-DD."},
                    "limit": _LIMIT,
                    "offset": _OFFSET,
                },
                required=["season"],
            ),
            query=queries.acb_find_games,
        ),
        tool(
            name="el_acb_get_player_games",
            title="ACB player game logs",
            description=(
                "Get one player game-by-game Liga Endesa box score: play time, points, "
                "2P/3P/FT makes and attempts, total field-goal attempts, rebounds, assists, "
                "steals, turnovers, blocks, fouls, plus-minus and valuation."
            ),
            input_schema=_schema(
                {
                    "season": _ACB_SEASON,
                    "player": {
                        "type": "string",
                        "description": "ACB source player id or player name.",
                    },
                    "team": {"type": "string", "description": "Optional team name filter."},
                    "limit": _LIMIT,
                    "offset": _OFFSET,
                },
                required=["season", "player"],
            ),
            query=queries.acb_get_player_games,
        ),
        tool(
            name="el_acb_get_play_by_play",
            title="ACB game play-by-play",
            description=(
                "Get source-order play-by-play for one Liga Endesa match. Use match_id "
                "from el_acb_find_games. Returns ACB event type, player id, quarter, "
                "clock and score."
            ),
            input_schema=_schema(
                {
                    "season": _ACB_SEASON,
                    "match_id": {
                        "type": "string",
                        "description": "ACB match id from el_acb_find_games.",
                    },
                    "quarter": {"type": "integer", "description": "Optional quarter filter."},
                    "event_kind": {"type": "string", "description": "Optional ACB event kind."},
                    "from_index": {"type": "integer", "description": "Start at this ingest_index."},
                    "limit": _LIMIT,
                    "offset": _OFFSET,
                },
                required=["season", "match_id"],
            ),
            query=queries.acb_get_play_by_play,
        ),
    ]
    return {tool.name: tool for tool in tools}
