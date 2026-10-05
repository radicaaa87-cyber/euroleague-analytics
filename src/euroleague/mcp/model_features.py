"""Compact model context built server-side to avoid MCP call explosions.

This module deliberately returns derived, pre-game context instead of replaying
box scores and play-by-play through the model one call at a time. Raw PBP stays
available through el_get_play_by_play for drill-down, but routine player-points
analysis should start here.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from euroleague.mcp import queries
from euroleague.mcp.envelope import build_response
from euroleague.mcp.resolve import resolve_player, resolve_season, resolve_team


def _iso_date(value: Any, name: str) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date YYYY-MM-DD, got {value!r}.") from exc


def get_player_model_context(cursor: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """One compact pre-game bundle for the role-adjusted player-points model.

    All rolling statistics are computed from games strictly before as_of_date
    when it is supplied. The current/upcoming bookmaker line is intentionally
    absent: projection must be produced independently, then compared with the
    line outside this tool.
    """

    include_quarantined = queries._boolean(arguments, "include_quarantined", False)
    season_code = resolve_season(cursor, arguments["season"])
    player_id = resolve_player(cursor, season_code, arguments["player"])

    minutes_basis = arguments.get("minutes_basis", "official")
    if minutes_basis not in ("corrected", "raw", "official"):
        raise ValueError(
            "minutes_basis must be 'corrected', 'raw' or 'official', "
            f"got {minutes_basis!r}."
        )
    seconds_column = {
        "corrected": "seconds_corrected",
        "raw": "seconds_raw",
        "official": "seconds_official",
    }[minutes_basis]

    lookback = int(arguments.get("lookback", 10))
    if lookback < 10 or lookback > 20:
        raise ValueError("lookback must be between 10 and 20 games.")

    as_of_date = _iso_date(arguments.get("as_of_date"), "as_of_date")

    conditions = [
        "p.season_code = %s",
        "p.player_id = %s",
        "p.seconds_official > 0",
    ]
    params: list[Any] = [season_code, player_id]
    if not include_quarantined:
        conditions.append("not p.excluded_by_default")
    if as_of_date is not None:
        conditions.append("p.utc_date::date < %s")
        params.append(as_of_date)
    where = " and ".join(conditions)

    cursor.execute(
        f"""
        with recent as (
            select
                p.season_code,
                p.gamecode,
                p.utc_date,
                p.player_id,
                p.player_name,
                p.team_code,
                p.opponent_team_code,
                tg.is_home,
                p.is_starter,
                p.points,
                p.field_goals_attempted,
                p.three_pointers_attempted,
                p.free_throws_attempted,
                p.{seconds_column} as seconds_played,
                p.team_possessions,
                row_number() over (
                    order by p.utc_date desc, p.gamecode desc
                ) as rn
            from v_player_game p
            left join v_team_game tg
              on tg.season_code = p.season_code
             and tg.gamecode = p.gamecode
             and tg.team_code = p.team_code
            where {where}
            order by p.utc_date desc, p.gamecode desc
            limit %s
        ),
        stats as (
            select
                max(player_id) as player_id,
                max(player_name) as player_name,
                max(team_code) filter (where rn = 1) as last_team_code,
                count(*) as history_games,
                max(utc_date)::date as last_game_date,

                round(avg(seconds_played::numeric / 60.0)
                    filter (where rn <= 3), 2) as l3_minutes,
                round(avg(seconds_played::numeric / 60.0)
                    filter (where rn <= 5), 2) as l5_minutes,
                round(avg(seconds_played::numeric / 60.0)
                    filter (where rn <= 10), 2) as l10_minutes,

                round(avg(points::numeric)
                    filter (where rn <= 3), 2) as l3_points,
                round(avg(points::numeric)
                    filter (where rn <= 5), 2) as l5_points,
                round(avg(points::numeric)
                    filter (where rn <= 10), 2) as l10_points,

                round(avg(field_goals_attempted::numeric)
                    filter (where rn <= 3), 2) as l3_fga,
                round(avg(field_goals_attempted::numeric)
                    filter (where rn <= 5), 2) as l5_fga,
                round(avg(field_goals_attempted::numeric)
                    filter (where rn <= 10), 2) as l10_fga,

                round(avg(three_pointers_attempted::numeric)
                    filter (where rn <= 3), 2) as l3_3pa,
                round(avg(three_pointers_attempted::numeric)
                    filter (where rn <= 5), 2) as l5_3pa,
                round(avg(three_pointers_attempted::numeric)
                    filter (where rn <= 10), 2) as l10_3pa,

                round(avg(free_throws_attempted::numeric)
                    filter (where rn <= 3), 2) as l3_fta,
                round(avg(free_throws_attempted::numeric)
                    filter (where rn <= 5), 2) as l5_fta,
                round(avg(free_throws_attempted::numeric)
                    filter (where rn <= 10), 2) as l10_fta,

                round(avg(case when is_starter then 1.0 else 0.0 end)
                    filter (where rn <= 5), 3) as l5_starter_rate,
                round(avg(case when is_starter then 1.0 else 0.0 end)
                    filter (where rn <= 10), 3) as l10_starter_rate,

                round(
                    60.0 * sum(points) filter (where rn <= 10)
                    / nullif(sum(seconds_played) filter (where rn <= 10), 0),
                    3
                ) as l10_points_per_minute,
                round(
                    60.0 * sum(field_goals_attempted) filter (where rn <= 10)
                    / nullif(sum(seconds_played) filter (where rn <= 10), 0),
                    3
                ) as l10_fga_per_minute,
                round(avg(team_possessions::numeric)
                    filter (where rn <= 10), 2) as l10_team_possessions,

                max(points) filter (where rn = 1) as last_points,
                round(
                    (max(seconds_played) filter (where rn = 1))::numeric / 60.0,
                    2
                ) as last_minutes,
                max(field_goals_attempted) filter (where rn = 1) as last_fga,
                (max(is_starter::int) filter (where rn = 1))::boolean
                    as last_was_starter,

                jsonb_agg(
                    jsonb_build_object(
                        'date', utc_date::date,
                        'gamecode', gamecode,
                        'team', team_code,
                        'opponent', opponent_team_code,
                        'home', is_home,
                        'starter', is_starter,
                        'minutes', round(seconds_played::numeric / 60.0, 2),
                        'points', points,
                        'fga', field_goals_attempted,
                        '3pa', three_pointers_attempted,
                        'fta', free_throws_attempted,
                        'team_possessions', team_possessions
                    )
                    order by rn
                ) as recent_games
            from recent
        )
        select
            *,
            round(l3_minutes - l10_minutes, 2) as minutes_trend_l3_vs_l10,
            round(l3_fga - l10_fga, 2) as fga_trend_l3_vs_l10,
            round(l3_points - l10_points, 2) as points_trend_l3_vs_l10
        from stats
        """,
        (*params, lookback),
    )
    rows = queries._rows(cursor)

    opponent_code: str | None = None
    if arguments.get("opponent"):
        opponent_code = resolve_team(cursor, season_code, arguments["opponent"])
        opponent_conditions = ["season_code = %s", "team_code = %s"]
        opponent_params: list[Any] = [season_code, opponent_code]
        if not include_quarantined:
            opponent_conditions.append("not excluded_by_default")
        if as_of_date is not None:
            opponent_conditions.append("utc_date::date < %s")
            opponent_params.append(as_of_date)

        cursor.execute(
            f"""
            with recent as (
                select *
                from v_team_game
                where {' and '.join(opponent_conditions)}
                order by utc_date desc, gamecode desc
                limit 5
            )
            select
                count(*) as opponent_l5_games,
                round(
                    100.0 * sum(points)
                    / nullif(sum(possessions), 0),
                    2
                ) as opponent_l5_off_rating,
                round(
                    100.0 * sum(opponent_points)
                    / nullif(sum(opponent_possessions), 0),
                    2
                ) as opponent_l5_def_rating,
                round(avg(possessions::numeric), 2) as opponent_l5_possessions,
                max(utc_date)::date as opponent_last_game_date
            from recent
            """,
            tuple(opponent_params),
        )
        opponent_rows = queries._rows(cursor)
        if rows and opponent_rows:
            rows[0]["opponent_team_code"] = opponent_code
            rows[0].update(opponent_rows[0])

    if rows and opponent_code is None:
        rows[0]["opponent_team_code"] = None

    return build_response(
        rows=rows,
        coverage=queries.coverage_for(cursor, season_code, include_quarantined),
        excluded=queries.exclusions_for(cursor, season_code, include_quarantined),
        minutes_basis=minutes_basis,
        caveats=[
            "This is a compact pre-game model bundle. Raw play-by-play is not "
            "returned here; use el_get_play_by_play only when a drill-down is needed.",
            "When as_of_date is supplied, every rolling player and opponent statistic "
            "uses only games strictly before that date to prevent look-ahead leakage.",
            "The bookmaker line is intentionally excluded. Produce the projection first, "
            "then calculate EDGE against the central line.",
        ],
    )
