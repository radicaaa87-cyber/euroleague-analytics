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
from euroleague.pregame_context import ROLE_CONTEXT_SQL
from euroleague.teammate_combinations import KEY_LINEUP_SQL, TEAMMATE_TRIPLE_SQL
from euroleague.teammate_synergy import TEAMMATE_SYNERGY_SQL
from euroleague.travel import travel_context


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
            f"minutes_basis must be 'corrected', 'raw' or 'official', got {minutes_basis!r}."
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

    target_gamecode: int | None = None
    if arguments.get("gamecode") is not None:
        try:
            target_gamecode = int(arguments["gamecode"])
        except (TypeError, ValueError) as exc:
            raise ValueError("gamecode must be a positive integer.") from exc
        if target_gamecode < 1:
            raise ValueError("gamecode must be a positive integer.")

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

    # Derive rotation and on-court context from the complete reconstructed PBP.
    # This stays inside the server: it is still one external MCP tool call.
    pbp_where = where.replace("p.", "pg.")
    cursor.execute(
        f"""
        with recent_games as (
            select
                pg.gamecode,
                pg.utc_date::date as game_date,
                pg.player_id
            from v_player_game pg
            where {pbp_where}
            order by pg.utc_date desc, pg.gamecode desc
            limit %s
        ),
        player_lineups as (
            select lp.lineup_id
            from v_lineup_player lp
            where lp.player_id = (select max(player_id) from recent_games)
        ),
        possession_rows as (
            select
                vp.gamecode,
                case when vp.offense_lineup_id in (
                    select lineup_id from player_lineups
                ) then 1 else 0 end as offensive_possessions,
                case when vp.defense_lineup_id in (
                    select lineup_id from player_lineups
                ) then 1 else 0 end as defensive_possessions,
                case when vp.offense_lineup_id in (
                    select lineup_id from player_lineups
                ) then vp.points_scored else 0 end as points_for,
                case when vp.defense_lineup_id in (
                    select lineup_id from player_lineups
                ) then vp.points_scored else 0 end as points_against
            from v_possession vp
            join recent_games rg
              on rg.gamecode = vp.gamecode
            where vp.season_code = %s
              and (
                    vp.offense_lineup_id in (select lineup_id from player_lineups)
                 or vp.defense_lineup_id in (select lineup_id from player_lineups)
              )
        ),
        possession_game as (
            select
                gamecode,
                sum(offensive_possessions) as offensive_possessions,
                sum(defensive_possessions) as defensive_possessions,
                sum(points_for) as points_for,
                sum(points_against) as points_against
            from possession_rows
            group by gamecode
        ),
        stint_rows as (
            select
                ls.gamecode,
                ls.home_lineup_id as lineup_id,
                greatest(ls.duration_seconds_raw, 0) as duration_seconds
            from lineup_stint ls
            join recent_games rg on rg.gamecode = ls.gamecode
            join player_lineups pl on pl.lineup_id = ls.home_lineup_id
            where ls.season_code = %s

            union all

            select
                ls.gamecode,
                ls.away_lineup_id as lineup_id,
                greatest(ls.duration_seconds_raw, 0) as duration_seconds
            from lineup_stint ls
            join recent_games rg on rg.gamecode = ls.gamecode
            join player_lineups pl on pl.lineup_id = ls.away_lineup_id
            where ls.season_code = %s
        ),
        stint_game as (
            select
                gamecode,
                count(*) as stint_count,
                round(avg(duration_seconds::numeric), 3) as avg_stint_seconds,
                max(duration_seconds) as max_stint_seconds
            from stint_rows
            group by gamecode
        ),
        lineup_seconds as (
            select
                gamecode,
                lineup_id,
                sum(duration_seconds) as lineup_seconds
            from stint_rows
            group by gamecode, lineup_id
        ),
        lineup_game as (
            select
                gamecode,
                count(*) as distinct_lineups,
                round(
                    max(lineup_seconds)::numeric
                    / nullif(sum(lineup_seconds), 0),
                    4
                ) as primary_lineup_share
            from lineup_seconds
            group by gamecode
        )
        select
            count(*) as pbp_history_games,
            round(avg(pg.offensive_possessions::numeric), 2)
                as l10_pbp_offensive_possessions,
            round(avg(pg.defensive_possessions::numeric), 2)
                as l10_pbp_defensive_possessions,
            round(
                100.0 * sum(pg.points_for)
                / nullif(sum(pg.offensive_possessions), 0),
                2
            ) as l10_pbp_on_off_rating,
            round(
                100.0 * sum(pg.points_against)
                / nullif(sum(pg.defensive_possessions), 0),
                2
            ) as l10_pbp_on_def_rating,
            round(avg(sg.stint_count::numeric), 2) as l10_pbp_stint_count,
            round(avg(lg.distinct_lineups::numeric), 2)
                as l10_pbp_distinct_lineups,
            round(avg(sg.avg_stint_seconds), 2) as l10_pbp_avg_stint_seconds,
            round(avg(sg.max_stint_seconds::numeric), 2)
                as l10_pbp_max_stint_seconds,
            round(avg(lg.primary_lineup_share), 3)
                as l10_pbp_primary_lineup_share
        from recent_games rg
        left join possession_game pg using (gamecode)
        left join stint_game sg using (gamecode)
        left join lineup_game lg using (gamecode)
        """,
        (*params, lookback, season_code, season_code, season_code),
    )
    pbp_rows = queries._rows(cursor)
    if rows and pbp_rows:
        rows[0].update(pbp_rows[0])

    opponent_code: str | None = None
    target_context: dict[str, Any] = {}

    if target_gamecode is not None:
        cursor.execute(
            ROLE_CONTEXT_SQL,
            {
                "season_code": season_code,
                "gamecode": target_gamecode,
                "player_id": player_id,
            },
        )
        context_rows = queries._rows(cursor)
        context = context_rows[0] if context_rows else {}
        target_context = context
        if rows:
            rows[0]["target_gamecode"] = target_gamecode
            rows[0]["pregame_role_context"] = context
            rows[0]["travel_context"] = travel_context(
                rows[0].get("recent_games"),
                target_team_code=context.get("target_team_code"),
                target_opponent_team_code=context.get("opponent_team_code"),
                target_is_home=context.get("is_home"),
            )
        inferred_opponent = context.get("opponent_team_code")
        if inferred_opponent:
            opponent_code = str(inferred_opponent)

    cross_competition_cutoff: Any = target_context.get("target_tipoff_utc")
    if cross_competition_cutoff is None and as_of_date is not None:
        cross_competition_cutoff = f"{as_of_date}T00:00:00+00:00"

    cursor.execute(
        """
        with athlete_link as (
            select athlete_id
            from athlete_source_identity
            where source = 'EL'
              and source_player_id = %s
              and match_status in ('auto_link', 'manual_verified')
        ),
        cutoff as (
            select coalesce(%s::timestamptz, now()) as at
        ),
        recent as (
            select
                h.*,
                row_number() over (
                    order by h.utc_date desc, h.source_game_id desc
                ) as rn
            from v_athlete_game_history h
            join athlete_link a using (athlete_id)
            cross join cutoff c
            where h.source = 'ACB'
              and h.utc_date < c.at
              and h.minutes_seconds > 0
              and not h.excluded_by_default
            order by h.utc_date desc, h.source_game_id desc
            limit 10
        ),
        stats as (
            select
                count(*) as history_games,
                max(utc_date) as last_game_at,
                round(avg(minutes_seconds::numeric / 60.0)
                    filter (where rn <= 3), 2) as l3_minutes,
                round(avg(minutes_seconds::numeric / 60.0)
                    filter (where rn <= 5), 2) as l5_minutes,
                round(avg(points::numeric)
                    filter (where rn <= 3), 2) as l3_points,
                round(avg(points::numeric)
                    filter (where rn <= 5), 2) as l5_points,
                round(avg(field_goals_attempted::numeric)
                    filter (where rn <= 3), 2) as l3_fga,
                round(avg(field_goals_attempted::numeric)
                    filter (where rn <= 5), 2) as l5_fga,
                round(avg(three_attempted::numeric)
                    filter (where rn <= 3), 2) as l3_3pa,
                round(avg(three_attempted::numeric)
                    filter (where rn <= 5), 2) as l5_3pa,
                round(avg(free_throw_attempted::numeric)
                    filter (where rn <= 3), 2) as l3_fta,
                round(avg(free_throw_attempted::numeric)
                    filter (where rn <= 5), 2) as l5_fta,
                round(avg(case when is_starter then 1.0 else 0.0 end)
                    filter (where rn <= 5), 3) as l5_starter_rate,
                round(
                    60.0 * sum(points) filter (where rn <= 5)
                    / nullif(sum(minutes_seconds) filter (where rn <= 5), 0),
                    3
                ) as l5_points_per_minute,
                round(
                    60.0 * sum(field_goals_attempted) filter (where rn <= 5)
                    / nullif(sum(minutes_seconds) filter (where rn <= 5), 0),
                    3
                ) as l5_fga_per_minute,
                count(*) filter (
                    where utc_date >= (select at from cutoff) - interval '7 days'
                ) as games_last_7d,
                round(
                    sum(minutes_seconds::numeric / 60.0) filter (
                        where utc_date >= (select at from cutoff) - interval '7 days'
                    ),
                    2
                ) as minutes_last_7d,
                round(
                    extract(
                        epoch from ((select at from cutoff) - max(utc_date))
                    ) / 86400.0,
                    3
                ) as days_since_last_game,
                jsonb_agg(
                    jsonb_build_object(
                        'date', utc_date::date,
                        'game_id', source_game_id,
                        'team_source_id', team_source_id,
                        'opponent_source_id', opponent_source_id,
                        'starter', is_starter,
                        'minutes', round(minutes_seconds::numeric / 60.0, 2),
                        'points', points,
                        'fga', field_goals_attempted,
                        '3pa', three_attempted,
                        'fta', free_throw_attempted
                    )
                    order by rn
                ) as recent_games
            from recent
        )
        select
            *,
            round(l3_minutes - l5_minutes, 2) as minutes_trend_l3_vs_l5,
            round(l3_fga - l5_fga, 2) as fga_trend_l3_vs_l5,
            round(l3_points - l5_points, 2) as points_trend_l3_vs_l5
        from stats
        """,
        (player_id, cross_competition_cutoff),
    )
    acb_rows = queries._rows(cursor)
    if rows and acb_rows:
        rows[0]["acb_recent_form"] = acb_rows[0]

    synergy_team_code: str | None = None
    if target_context.get("target_team_code"):
        synergy_team_code = str(target_context["target_team_code"])
    elif rows and rows[0].get("last_team_code"):
        synergy_team_code = str(rows[0]["last_team_code"])

    cursor.execute(
        TEAMMATE_SYNERGY_SQL,
        (
            cross_competition_cutoff,
            season_code,
            player_id,
            synergy_team_code,
            synergy_team_code,
            20,
            player_id,
            player_id,
            12,
        ),
    )
    teammate_rows = queries._rows(cursor)
    availability_by_player = {
        str(signal["player_id"]): signal
        for signal in target_context.get("teammate_availability", [])
        if signal.get("player_id")
    }
    for pair in teammate_rows:
        signal = availability_by_player.get(str(pair.get("teammate_id")))
        if signal is not None:
            pair["pregame_availability"] = signal

    cursor.execute(
        TEAMMATE_TRIPLE_SQL,
        (
            cross_competition_cutoff,
            season_code,
            player_id,
            synergy_team_code,
            synergy_team_code,
            20,
            player_id,
            player_id,
            10,
        ),
    )
    triple_rows = queries._rows(cursor)
    for triple in triple_rows:
        availability: list[dict[str, Any]] = []
        for key in ("teammate_a_id", "teammate_b_id"):
            signal = availability_by_player.get(str(triple.get(key)))
            if signal is not None:
                availability.append(signal)
        if availability:
            triple["pregame_availability"] = availability

    cursor.execute(
        KEY_LINEUP_SQL,
        (
            cross_competition_cutoff,
            season_code,
            player_id,
            synergy_team_code,
            synergy_team_code,
            20,
            player_id,
            6,
        ),
    )
    lineup_rows = queries._rows(cursor)
    for lineup in lineup_rows:
        unavailable = [
            availability_by_player[str(member["player_id"])]
            for member in lineup.get("players", [])
            if member.get("player_id")
            and str(member["player_id"]) in availability_by_player
        ]
        if unavailable:
            lineup["pregame_unavailable_players"] = unavailable

    if rows:
        rows[0]["teammate_pair_context"] = teammate_rows
        rows[0]["teammate_triple_context"] = triple_rows
        rows[0]["key_lineup_context"] = lineup_rows

    if arguments.get("opponent"):
        opponent_code = resolve_team(cursor, season_code, arguments["opponent"])

    if opponent_code is not None:
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
                where {" and ".join(opponent_conditions)}
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
            "This compact bundle derives possessions, lineup concentration and stint "
            "patterns from the complete reconstructed play-by-play. Raw event rows stay "
            "server-side; use el_get_play_by_play only for exceptional drill-down.",
            "When as_of_date is supplied, every rolling player and opponent statistic "
            "uses only games strictly before that date to prevent look-ahead leakage.",
            "When gamecode is supplied, pregame_role_context is built only from evidence "
            "published in the 72 hours before that game's tipoff. Availability, role and "
            "vacated-minutes/FGA signals are severity- and source-confidence-weighted; this "
            "forward context remains separate from historical blind-test training until the "
            "timestamped archive is large enough to calibrate it safely.",
            "acb_recent_form follows only canonical athlete identity links and is cut off "
            "strictly before the target tipoff (or as_of_date). It never matches players by "
            "name and it remains a separate domestic-role signal rather than blindly pooling "
            "ACB and EuroLeague averages.",
            "travel_context reports great-circle distance between nominal team home cities. "
            "It is a schedule travel-load proxy, not a claim about the actual flight path or "
            "a temporary neutral/home venue; unknown team codes stay null.",
            "teammate_pair_context is descriptive, not causal. It combines actual shared "
            "stint minutes and on-court team ratings with the target player's historical "
            "PTS/min, FGA/min and minutes in games with versus without each teammate. Small "
            "samples are labeled explicitly and every row is cut off before the target tipoff.",
            "teammate_triple_context requires at least 5 games, 75 actual shared minutes and "
            "3 comparison games before it can be labeled positive or negative. key_lineup_context "
            "uses exact five-man units and requires at least 4 games, 60 shared minutes and "
            "50 team possessions; otherwise the combination remains small_sample.",
            "The bookmaker line is intentionally excluded. Produce the projection first, "
            "then calculate EDGE against the central line.",
        ],
    )
