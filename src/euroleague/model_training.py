"""Server-side feature export for player-points machine learning.

The training path reads PostgreSQL directly in one read-only query. It uses the
complete reconstructed play-by-play layer (possessions, lineups and stints) but
exports compact pre-game features instead of paging raw events through ChatGPT.
That avoids MCP row-budget consumption and action explosions.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from euroleague.config import DatabaseSettings
from euroleague.mcp.db import connect

_MINUTES_COLUMNS = {
    "official": "seconds_official",
    "corrected": "seconds_corrected",
    "raw": "seconds_raw",
}


def training_dataset_sql(minutes_basis: str = "official") -> str:
    """Return the leakage-safe player-game feature query.

    Every rolling window ends at 1 PRECEDING, so the target game's box score,
    possessions, lineup usage and stint pattern never enter its own features.
    """
    try:
        seconds = _MINUTES_COLUMNS[minutes_basis]
    except KeyError as exc:
        raise ValueError(
            "minutes_basis must be one of: official, corrected, raw"
        ) from exc

    return f"""
    with requested_seasons as (
        select unnest(%s::text[]) as season_code
    ),
    possession_player_rows as (
        select
            p.season_code,
            p.gamecode,
            lp.player_id,
            1::integer as offensive_possessions,
            0::integer as defensive_possessions,
            p.points_scored as points_for,
            0::integer as points_against,
            case
                when p.duration_seconds between 0 and 8 then 1
                else 0
            end as transition_offensive_possessions
        from v_possession p
        join requested_seasons rs using (season_code)
        join v_lineup_player lp
          on lp.lineup_id = p.offense_lineup_id
        where not p.excluded_by_default

        union all

        select
            p.season_code,
            p.gamecode,
            lp.player_id,
            0::integer as offensive_possessions,
            1::integer as defensive_possessions,
            0::integer as points_for,
            p.points_scored as points_against,
            0::integer as transition_offensive_possessions
        from v_possession p
        join requested_seasons rs using (season_code)
        join v_lineup_player lp
          on lp.lineup_id = p.defense_lineup_id
        where not p.excluded_by_default
    ),
    pbp_possession_game as (
        select
            season_code,
            gamecode,
            player_id,
            sum(offensive_possessions) as pbp_offensive_possessions,
            sum(defensive_possessions) as pbp_defensive_possessions,
            sum(points_for) as pbp_points_for,
            sum(points_against) as pbp_points_against,
            sum(transition_offensive_possessions)
                as pbp_transition_offensive_possessions
        from possession_player_rows
        group by 1, 2, 3
    ),
    player_stint_rows as (
        select
            ls.season_code,
            ls.gamecode,
            lp.player_id,
            ls.home_lineup_id as lineup_id,
            greatest(ls.duration_seconds_raw, 0) as duration_seconds
        from lineup_stint ls
        join requested_seasons rs using (season_code)
        join v_game g
          on g.season_code = ls.season_code
         and g.gamecode = ls.gamecode
        join v_lineup_player lp
          on lp.lineup_id = ls.home_lineup_id
        where not g.excluded_by_default

        union all

        select
            ls.season_code,
            ls.gamecode,
            lp.player_id,
            ls.away_lineup_id as lineup_id,
            greatest(ls.duration_seconds_raw, 0) as duration_seconds
        from lineup_stint ls
        join requested_seasons rs using (season_code)
        join v_game g
          on g.season_code = ls.season_code
         and g.gamecode = ls.gamecode
        join v_lineup_player lp
          on lp.lineup_id = ls.away_lineup_id
        where not g.excluded_by_default
    ),
    player_stint_summary as (
        select
            season_code,
            gamecode,
            player_id,
            count(*) as pbp_stint_count,
            round(avg(duration_seconds::numeric), 3) as pbp_avg_stint_seconds,
            max(duration_seconds) as pbp_max_stint_seconds,
            sum(duration_seconds) as pbp_stint_seconds
        from player_stint_rows
        group by 1, 2, 3
    ),
    player_lineup_seconds as (
        select
            season_code,
            gamecode,
            player_id,
            lineup_id,
            sum(duration_seconds) as lineup_seconds
        from player_stint_rows
        group by 1, 2, 3, 4
    ),
    player_lineup_summary as (
        select
            season_code,
            gamecode,
            player_id,
            count(*) as pbp_distinct_lineups,
            round(
                max(lineup_seconds)::numeric
                / nullif(sum(lineup_seconds), 0),
                4
            ) as pbp_primary_lineup_share
        from player_lineup_seconds
        group by 1, 2, 3
    ),
    pbp_stint_game as (
        select
            s.season_code,
            s.gamecode,
            s.player_id,
            s.pbp_stint_count,
            s.pbp_avg_stint_seconds,
            s.pbp_max_stint_seconds,
            s.pbp_stint_seconds,
            l.pbp_distinct_lineups,
            l.pbp_primary_lineup_share
        from player_stint_summary s
        join player_lineup_summary l
          using (season_code, gamecode, player_id)
    ),
    player_base as (
        select
            p.season_code,
            p.gamecode,
            p.utc_date::date as game_date,
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
            p.{seconds} as seconds_played,

            coalesce(pg.pbp_offensive_possessions, 0)
                as pbp_offensive_possessions,
            coalesce(pg.pbp_defensive_possessions, 0)
                as pbp_defensive_possessions,
            coalesce(pg.pbp_points_for, 0) as pbp_points_for,
            coalesce(pg.pbp_points_against, 0) as pbp_points_against,
            coalesce(pg.pbp_transition_offensive_possessions, 0)
                as pbp_transition_offensive_possessions,

            coalesce(sg.pbp_stint_count, 0) as pbp_stint_count,
            sg.pbp_avg_stint_seconds,
            sg.pbp_max_stint_seconds,
            sg.pbp_stint_seconds,
            coalesce(sg.pbp_distinct_lineups, 0) as pbp_distinct_lineups,
            sg.pbp_primary_lineup_share
        from v_player_game p
        join requested_seasons rs using (season_code)
        join v_team_game tg
          on tg.season_code = p.season_code
         and tg.gamecode = p.gamecode
         and tg.team_code = p.team_code
        left join pbp_possession_game pg
          on pg.season_code = p.season_code
         and pg.gamecode = p.gamecode
         and pg.player_id = p.player_id
        left join pbp_stint_game sg
          on sg.season_code = p.season_code
         and sg.gamecode = p.gamecode
         and sg.player_id = p.player_id
        where p.seconds_official > 0
          and not p.excluded_by_default
    ),
    player_features as (
        select
            *,
            count(*) over w10 as pre_history_games,

            round(avg(seconds_played::numeric / 60.0) over w3, 3) as pre_l3_minutes,
            round(avg(seconds_played::numeric / 60.0) over w5, 3) as pre_l5_minutes,
            round(avg(seconds_played::numeric / 60.0) over w10, 3) as pre_l10_minutes,

            round(avg(points::numeric) over w3, 3) as pre_l3_points,
            round(avg(points::numeric) over w5, 3) as pre_l5_points,
            round(avg(points::numeric) over w10, 3) as pre_l10_points,

            round(avg(field_goals_attempted::numeric) over w3, 3) as pre_l3_fga,
            round(avg(field_goals_attempted::numeric) over w5, 3) as pre_l5_fga,
            round(avg(field_goals_attempted::numeric) over w10, 3) as pre_l10_fga,

            round(avg(three_pointers_attempted::numeric) over w3, 3) as pre_l3_3pa,
            round(avg(three_pointers_attempted::numeric) over w5, 3) as pre_l5_3pa,
            round(avg(three_pointers_attempted::numeric) over w10, 3) as pre_l10_3pa,

            round(avg(free_throws_attempted::numeric) over w3, 3) as pre_l3_fta,
            round(avg(free_throws_attempted::numeric) over w5, 3) as pre_l5_fta,
            round(avg(free_throws_attempted::numeric) over w10, 3) as pre_l10_fta,

            round(avg(case when is_starter then 1.0 else 0.0 end) over w5, 4)
                as pre_l5_starter_rate,
            round(avg(case when is_starter then 1.0 else 0.0 end) over w10, 4)
                as pre_l10_starter_rate,
            lag(is_starter) over wall as pre_last_was_starter,

            round(
                60.0 * sum(points) over w10
                / nullif(sum(seconds_played) over w10, 0),
                4
            ) as pre_l10_points_per_minute,
            round(
                60.0 * sum(field_goals_attempted) over w10
                / nullif(sum(seconds_played) over w10, 0),
                4
            ) as pre_l10_fga_per_minute,

            round(
                avg(pbp_offensive_possessions::numeric) over w5, 3
            ) as pre_l5_pbp_offensive_possessions,
            round(
                avg(pbp_offensive_possessions::numeric) over w10, 3
            ) as pre_l10_pbp_offensive_possessions,
            round(
                avg(pbp_defensive_possessions::numeric) over w5, 3
            ) as pre_l5_pbp_defensive_possessions,
            round(
                avg(pbp_defensive_possessions::numeric) over w10, 3
            ) as pre_l10_pbp_defensive_possessions,

            round(
                100.0 * sum(pbp_points_for) over w10
                / nullif(sum(pbp_offensive_possessions) over w10, 0),
                3
            ) as pre_l10_pbp_on_off_rating,
            round(
                100.0 * sum(pbp_points_against) over w10
                / nullif(sum(pbp_defensive_possessions) over w10, 0),
                3
            ) as pre_l10_pbp_on_def_rating,
            round(
                (sum(pbp_transition_offensive_possessions) over w10)::numeric
                / nullif(sum(pbp_offensive_possessions) over w10, 0),
                4
            ) as pre_l10_pbp_transition_offense_share,

            round(avg(pbp_stint_count::numeric) over w5, 3)
                as pre_l5_pbp_stint_count,
            round(avg(pbp_distinct_lineups::numeric) over w5, 3)
                as pre_l5_pbp_distinct_lineups,
            round(avg(pbp_avg_stint_seconds) over w5, 3)
                as pre_l5_pbp_avg_stint_seconds,
            round(avg(pbp_max_stint_seconds::numeric) over w5, 3)
                as pre_l5_pbp_max_stint_seconds,
            round(avg(pbp_primary_lineup_share) over w5, 4)
                as pre_l5_pbp_primary_lineup_share
        from player_base
        window
            wall as (
                partition by season_code, player_id
                order by game_date, gamecode
            ),
            w3 as (
                partition by season_code, player_id
                order by game_date, gamecode
                rows between 3 preceding and 1 preceding
            ),
            w5 as (
                partition by season_code, player_id
                order by game_date, gamecode
                rows between 5 preceding and 1 preceding
            ),
            w10 as (
                partition by season_code, player_id
                order by game_date, gamecode
                rows between 10 preceding and 1 preceding
            )
    ),
    team_features as (
        select
            tg.season_code,
            tg.gamecode,
            tg.team_code,
            round(
                100.0 * sum(tg.points) over w5
                / nullif(sum(tg.possessions) over w5, 0),
                3
            ) as pre_l5_off_rating,
            round(
                100.0 * sum(tg.opponent_points) over w5
                / nullif(sum(tg.opponent_possessions) over w5, 0),
                3
            ) as pre_l5_def_rating,
            round(avg(tg.possessions::numeric) over w5, 3)
                as pre_l5_possessions
        from v_team_game tg
        join requested_seasons rs using (season_code)
        where not tg.excluded_by_default
        window w5 as (
            partition by tg.season_code, tg.team_code
            order by tg.utc_date::date, tg.gamecode
            rows between 5 preceding and 1 preceding
        )
    )
    select
        pf.season_code,
        pf.gamecode,
        pf.game_date,
        pf.player_id,
        pf.player_name,
        pf.team_code,
        pf.opponent_team_code,
        pf.is_home,

        pf.pre_history_games,
        pf.pre_l3_minutes,
        pf.pre_l5_minutes,
        pf.pre_l10_minutes,
        pf.pre_l3_points,
        pf.pre_l5_points,
        pf.pre_l10_points,
        pf.pre_l3_fga,
        pf.pre_l5_fga,
        pf.pre_l10_fga,
        pf.pre_l3_3pa,
        pf.pre_l5_3pa,
        pf.pre_l10_3pa,
        pf.pre_l3_fta,
        pf.pre_l5_fta,
        pf.pre_l10_fta,
        pf.pre_l5_starter_rate,
        pf.pre_l10_starter_rate,
        pf.pre_last_was_starter,
        pf.pre_l10_points_per_minute,
        pf.pre_l10_fga_per_minute,

        pf.pre_l5_pbp_offensive_possessions,
        pf.pre_l10_pbp_offensive_possessions,
        pf.pre_l5_pbp_defensive_possessions,
        pf.pre_l10_pbp_defensive_possessions,
        pf.pre_l10_pbp_on_off_rating,
        pf.pre_l10_pbp_on_def_rating,
        pf.pre_l10_pbp_transition_offense_share,
        pf.pre_l5_pbp_stint_count,
        pf.pre_l5_pbp_distinct_lineups,
        pf.pre_l5_pbp_avg_stint_seconds,
        pf.pre_l5_pbp_max_stint_seconds,
        pf.pre_l5_pbp_primary_lineup_share,

        ot.pre_l5_off_rating as opponent_pre_l5_off_rating,
        ot.pre_l5_def_rating as opponent_pre_l5_def_rating,
        ot.pre_l5_possessions as opponent_pre_l5_possessions,

        round(pf.pre_l3_minutes - pf.pre_l10_minutes, 3)
            as pre_minutes_trend_l3_vs_l10,
        round(pf.pre_l3_fga - pf.pre_l10_fga, 3)
            as pre_fga_trend_l3_vs_l10,
        round(pf.pre_l3_points - pf.pre_l10_points, 3)
            as pre_points_trend_l3_vs_l10,

        pf.points as target_points,
        round(pf.seconds_played::numeric / 60.0, 3) as target_minutes,
        pf.field_goals_attempted as target_fga,
        pf.three_pointers_attempted as target_3pa,
        pf.free_throws_attempted as target_fta,
        pf.is_starter as target_was_starter
    from player_features pf
    left join team_features ot
      on ot.season_code = pf.season_code
     and ot.gamecode = pf.gamecode
     and ot.team_code = pf.opponent_team_code
    where pf.pre_history_games >= %s
    order by pf.game_date, pf.gamecode, pf.player_id
    """


def export_training_dataset(
    *,
    seasons: Iterable[str],
    output_path: Path | str,
    minutes_basis: str = "official",
    min_history_games: int = 3,
) -> dict[str, Any]:
    """Write the PBP-derived feature set with one DB query and no MCP paging."""
    season_list = [str(season).strip() for season in seasons if str(season).strip()]
    if not season_list:
        raise ValueError("At least one season code is required.")
    if min_history_games < 1 or min_history_games > 10:
        raise ValueError("min_history_games must be between 1 and 10.")

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    settings = DatabaseSettings.from_env()
    connection = connect(settings)
    row_count = 0
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                training_dataset_sql(minutes_basis),
                (season_list, min_history_games),
            )
            columns = [column[0] for column in cursor.description]
            with destination.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(columns)
                while True:
                    batch = cursor.fetchmany(1000)
                    if not batch:
                        break
                    writer.writerows(batch)
                    row_count += len(batch)
    finally:
        connection.close()

    return {
        "output_path": str(destination),
        "rows": row_count,
        "seasons": season_list,
        "minutes_basis": minutes_basis,
        "min_history_games": min_history_games,
        "feature_source": "boxscore_plus_full_pbp_derived",
    }
