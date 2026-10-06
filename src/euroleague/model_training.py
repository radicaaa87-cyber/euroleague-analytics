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
from euroleague.travel import travel_distance_sql

_MINUTES_COLUMNS = {
    "official": "seconds_official",
    "corrected": "seconds_corrected",
    "raw": "seconds_raw",
}

_SAFE_STATIC_FEATURES = {"is_home"}


def model_feature_columns(columns: Iterable[str]) -> list[str]:
    """Return only columns that are legal model inputs.

    Historical signals are explicitly prefixed pre_. The only static
    non-rolling input currently allowed is is_home. Player/team ids,
    bookmaker lines and every target_* field are deliberately excluded.
    """
    return [
        column for column in columns if column.startswith("pre_") or column in _SAFE_STATIC_FEATURES
    ]


def training_dataset_sql(minutes_basis: str = "official") -> str:
    """Return the leakage-safe player-game feature query.

    Every rolling window ends at 1 PRECEDING, so the target game's box score,
    possessions, lineup usage and stint pattern never enter its own features.
    """
    try:
        seconds = _MINUTES_COLUMNS[minutes_basis]
    except KeyError as exc:
        raise ValueError("minutes_basis must be one of: official, corrected, raw") from exc

    previous_venue = (
        "case when role_prev_is_home then role_prev_team_code else role_prev_opponent_team_code end"
    )
    current_venue = "case when is_home then team_code else opponent_team_code end"
    travel_km_sql = travel_distance_sql(previous_venue, current_venue)

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
            0::integer as points_against
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
            p.points_scored as points_against
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
            sum(points_against) as pbp_points_against
        from possession_player_rows
        group by 1, 2, 3
    ),
    player_stint_rows as (
        select
            ls.season_code,
            ls.gamecode,
            lp.player_id,
            ls.home_lineup_id as lineup_id,
            greatest(ls.duration_seconds_raw, 0) as duration_seconds,
            ls.home_points as team_points,
            ls.away_points as opponent_points,
            ls.possessions_home as team_possessions,
            ls.possessions_away as opponent_possessions
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
            greatest(ls.duration_seconds_raw, 0) as duration_seconds,
            ls.away_points as team_points,
            ls.home_points as opponent_points,
            ls.possessions_away as team_possessions,
            ls.possessions_home as opponent_possessions
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
            sum(duration_seconds) as lineup_seconds,
            sum(team_points) as lineup_team_points,
            sum(opponent_points) as lineup_opponent_points,
            sum(team_possessions) as lineup_team_possessions,
            sum(opponent_possessions) as lineup_opponent_possessions
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
    player_teammate_possession_game as (
        select
            p.season_code,
            p.gamecode,
            target.player_id,
            teammate.player_id as teammate_id,
            count(*) as shared_offensive_possessions
        from v_possession p
        join requested_seasons rs using (season_code)
        join v_lineup_player target
          on target.lineup_id = p.offense_lineup_id
        join v_lineup_player teammate
          on teammate.lineup_id = p.offense_lineup_id
         and teammate.player_id <> target.player_id
        where not p.excluded_by_default
        group by 1, 2, 3, 4
    ),
    player_teammate_fga_game as (
        select
            e.season_code,
            e.gamecode,
            e.player_id,
            teammate.player_id as teammate_id,
            count(*) as shared_fga
        from game_event e
        join requested_seasons rs using (season_code)
        join v_game g
          on g.season_code = e.season_code
         and g.gamecode = e.gamecode
        join v_lineup_player teammate
          on teammate.lineup_id = case
                when e.codeteam = g.home_team_code then e.home_lineup_id
                else e.away_lineup_id
             end
         and teammate.player_id <> e.player_id
        where e.playtype in ('2FGM', '2FGA', '3FGM', '3FGA')
          and e.player_id is not null
          and e.codeteam is not null
          and not g.excluded_by_default
        group by 1, 2, 3, 4
    ),
    player_teammate_on_off_game as (
        select
            pp.season_code,
            pp.gamecode,
            pp.player_id,
            pp.teammate_id,
            pp.shared_offensive_possessions,
            greatest(
                coalesce(pg.pbp_offensive_possessions, 0)
                    - pp.shared_offensive_possessions,
                0
            ) as teammate_off_offensive_possessions,
            coalesce(pfga.shared_fga, 0) as shared_fga,
            greatest(
                coalesce(vpg.field_goals_attempted, 0)
                    - coalesce(pfga.shared_fga, 0),
                0
            ) as teammate_off_fga,
            round(
                100.0 * coalesce(pfga.shared_fga, 0)
                    / nullif(pp.shared_offensive_possessions, 0),
                4
            ) as teammate_on_fga_per_100,
            round(
                100.0 * greatest(
                    coalesce(vpg.field_goals_attempted, 0)
                        - coalesce(pfga.shared_fga, 0),
                    0
                )
                    / nullif(
                        greatest(
                            coalesce(pg.pbp_offensive_possessions, 0)
                                - pp.shared_offensive_possessions,
                            0
                        ),
                        0
                    ),
                4
            ) as teammate_off_fga_per_100
        from player_teammate_possession_game pp
        join v_player_game vpg
          on vpg.season_code = pp.season_code
         and vpg.gamecode = pp.gamecode
         and vpg.player_id = pp.player_id
        left join pbp_possession_game pg
          on pg.season_code = pp.season_code
         and pg.gamecode = pp.gamecode
         and pg.player_id = pp.player_id
        left join player_teammate_fga_game pfga
          on pfga.season_code = pp.season_code
         and pfga.gamecode = pp.gamecode
         and pfga.player_id = pp.player_id
         and pfga.teammate_id = pp.teammate_id
        where not vpg.excluded_by_default
    ),
    player_role2_on_off_game as (
        select
            season_code,
            gamecode,
            player_id,
            count(*) filter (
                where shared_offensive_possessions >= 5
                  and teammate_off_offensive_possessions >= 5
            ) as role2_teammate_contexts,
            round(
                avg(
                    teammate_off_fga_per_100 - teammate_on_fga_per_100
                ) filter (
                    where shared_offensive_possessions >= 5
                      and teammate_off_offensive_possessions >= 5
                ),
                4
            ) as role2_mean_teammate_off_fga_uplift,
            round(
                max(
                    teammate_off_fga_per_100 - teammate_on_fga_per_100
                ) filter (
                    where shared_offensive_possessions >= 5
                      and teammate_off_offensive_possessions >= 5
                ),
                4
            ) as role2_max_teammate_off_fga_uplift
        from player_teammate_on_off_game
        group by 1, 2, 3
    ),
    player_lineup_ranked as (
        select
            pls.season_code,
            pls.gamecode,
            pls.player_id,
            pls.lineup_id,
            g.utc_date as game_tipoff_utc,
            round(pls.lineup_seconds::numeric / 60.0, 3) as shared_minutes,
            round(
                100.0 * pls.lineup_team_points
                    / nullif(pls.lineup_team_possessions, 0)
                - 100.0 * pls.lineup_opponent_points
                    / nullif(pls.lineup_opponent_possessions, 0),
                3
            ) as net_rating,
            row_number() over (
                partition by pls.season_code, pls.gamecode, pls.player_id
                order by pls.lineup_seconds desc, pls.lineup_id
            ) as combination_rank
        from player_lineup_seconds pls
        join v_game g
          on g.season_code = pls.season_code
         and g.gamecode = pls.gamecode
    ),
    player_top_lineup as (
        select *
        from player_lineup_ranked
        where combination_rank = 1
    ),
    player_top_lineup_events as (
        select
            *,
            case
                when lag(lineup_id) over lineup_all = lineup_id then 1
                else 0
            end as repeat_event
        from player_top_lineup
        window lineup_all as (
            partition by player_id
            order by game_tipoff_utc, gamecode
        )
    ),
    player_top_lineup_pregame as (
        select
            season_code,
            gamecode,
            player_id,
            lag(shared_minutes) over lineup_all
                as pre_last_top_lineup_shared_minutes,
            lag(net_rating) over lineup_all
                as pre_last_top_lineup_net_rating,
            round(avg(shared_minutes) over lineup_w5, 3)
                as pre_l5_top_lineup_shared_minutes,
            round(avg(net_rating) over lineup_w5, 3)
                as pre_l5_top_lineup_net_rating,
            round(avg(repeat_event::numeric) over lineup_w5, 4)
                as pre_l5_top_lineup_repeat_rate
        from player_top_lineup_events
        window
            lineup_all as (
                partition by player_id
                order by game_tipoff_utc, gamecode
            ),
            lineup_w5 as (
                partition by player_id
                order by game_tipoff_utc, gamecode
                rows between 5 preceding and 1 preceding
            )
    ),
    player_pair_game as (
        select
            psr.season_code,
            psr.gamecode,
            psr.player_id,
            teammate.player_id as teammate_id,
            g.utc_date as game_tipoff_utc,
            sum(psr.duration_seconds)::numeric as shared_seconds,
            sum(psr.team_points) as team_points,
            sum(psr.opponent_points) as opponent_points,
            sum(psr.team_possessions) as team_possessions,
            sum(psr.opponent_possessions) as opponent_possessions
        from player_stint_rows psr
        join v_lineup_player teammate
          on teammate.lineup_id = psr.lineup_id
         and teammate.player_id <> psr.player_id
        join v_game g
          on g.season_code = psr.season_code
         and g.gamecode = psr.gamecode
        group by
            psr.season_code,
            psr.gamecode,
            psr.player_id,
            teammate.player_id,
            g.utc_date
    ),
    player_pair_ranked as (
        select
            *,
            round(shared_seconds / 60.0, 3) as shared_minutes,
            round(
                100.0 * team_points / nullif(team_possessions, 0)
                - 100.0 * opponent_points / nullif(opponent_possessions, 0),
                3
            ) as net_rating,
            row_number() over (
                partition by season_code, gamecode, player_id
                order by shared_seconds desc, teammate_id
            ) as combination_rank
        from player_pair_game
    ),
    player_top_pair as (
        select *
        from player_pair_ranked
        where combination_rank = 1
    ),
    player_top_pair_events as (
        select
            *,
            case
                when lag(teammate_id) over pair_all = teammate_id then 1
                else 0
            end as repeat_event
        from player_top_pair
        window pair_all as (
            partition by player_id
            order by game_tipoff_utc, gamecode
        )
    ),
    player_top_pair_pregame as (
        select
            season_code,
            gamecode,
            player_id,
            lag(shared_minutes) over pair_all
                as pre_last_top_pair_shared_minutes,
            lag(net_rating) over pair_all
                as pre_last_top_pair_net_rating,
            round(avg(shared_minutes) over pair_w5, 3)
                as pre_l5_top_pair_shared_minutes,
            round(avg(net_rating) over pair_w5, 3)
                as pre_l5_top_pair_net_rating,
            round(avg(repeat_event::numeric) over pair_w5, 4)
                as pre_l5_top_pair_repeat_rate
        from player_top_pair_events
        window
            pair_all as (
                partition by player_id
                order by game_tipoff_utc, gamecode
            ),
            pair_w5 as (
                partition by player_id
                order by game_tipoff_utc, gamecode
                rows between 5 preceding and 1 preceding
            )
    ),
    player_triple_game as (
        select
            psr.season_code,
            psr.gamecode,
            psr.player_id,
            teammate_a.player_id as teammate_a_id,
            teammate_b.player_id as teammate_b_id,
            g.utc_date as game_tipoff_utc,
            sum(psr.duration_seconds)::numeric as shared_seconds,
            sum(psr.team_points) as team_points,
            sum(psr.opponent_points) as opponent_points,
            sum(psr.team_possessions) as team_possessions,
            sum(psr.opponent_possessions) as opponent_possessions
        from player_stint_rows psr
        join v_lineup_player teammate_a
          on teammate_a.lineup_id = psr.lineup_id
         and teammate_a.player_id <> psr.player_id
        join v_lineup_player teammate_b
          on teammate_b.lineup_id = psr.lineup_id
         and teammate_b.player_id <> psr.player_id
         and teammate_a.player_id < teammate_b.player_id
        join v_game g
          on g.season_code = psr.season_code
         and g.gamecode = psr.gamecode
        group by
            psr.season_code,
            psr.gamecode,
            psr.player_id,
            teammate_a.player_id,
            teammate_b.player_id,
            g.utc_date
    ),
    player_triple_ranked as (
        select
            *,
            round(shared_seconds / 60.0, 3) as shared_minutes,
            round(
                100.0 * team_points / nullif(team_possessions, 0)
                - 100.0 * opponent_points / nullif(opponent_possessions, 0),
                3
            ) as net_rating,
            row_number() over (
                partition by season_code, gamecode, player_id
                order by shared_seconds desc, teammate_a_id, teammate_b_id
            ) as combination_rank
        from player_triple_game
    ),
    player_top_triple as (
        select *
        from player_triple_ranked
        where combination_rank = 1
    ),
    player_top_triple_events as (
        select
            *,
            lag(teammate_a_id) over triple_all as previous_teammate_a_id,
            lag(teammate_b_id) over triple_all as previous_teammate_b_id
        from player_top_triple
        window triple_all as (
            partition by player_id
            order by game_tipoff_utc, gamecode
        )
    ),
    player_top_triple_repeat as (
        select
            *,
            case
                when previous_teammate_a_id = teammate_a_id
                 and previous_teammate_b_id = teammate_b_id
                    then 1
                else 0
            end as repeat_event
        from player_top_triple_events
    ),
    player_top_triple_pregame as (
        select
            season_code,
            gamecode,
            player_id,
            lag(shared_minutes) over triple_all
                as pre_last_top_triple_shared_minutes,
            lag(net_rating) over triple_all
                as pre_last_top_triple_net_rating,
            round(avg(shared_minutes) over triple_w5, 3)
                as pre_l5_top_triple_shared_minutes,
            round(avg(net_rating) over triple_w5, 3)
                as pre_l5_top_triple_net_rating,
            round(avg(repeat_event::numeric) over triple_w5, 4)
                as pre_l5_top_triple_repeat_rate
        from player_top_triple_repeat
        window
            triple_all as (
                partition by player_id
                order by game_tipoff_utc, gamecode
            ),
            triple_w5 as (
                partition by player_id
                order by game_tipoff_utc, gamecode
                rows between 5 preceding and 1 preceding
            )
    ),
    lineup_matchup_candidate as (
        select
            ls.season_code,
            ls.gamecode,
            ls.stint_index,
            home_player.player_id,
            home_roster.position_name as player_position_name,
            home_roster.height_cm as player_height_cm,
            home_roster.weight_kg as player_weight_kg,
            away_player.player_id as opponent_player_id,
            away_roster.position_name as opponent_position_name,
            away_roster.height_cm as opponent_height_cm,
            away_roster.weight_kg as opponent_weight_kg,
            greatest(ls.duration_seconds_raw, 0)::numeric as overlap_seconds
        from lineup_stint ls
        join requested_seasons rs using (season_code)
        join v_game g
          on g.season_code = ls.season_code
         and g.gamecode = ls.gamecode
        join lineup home_lineup
          on home_lineup.lineup_id = ls.home_lineup_id
        join lineup away_lineup
          on away_lineup.lineup_id = ls.away_lineup_id
        join v_lineup_player home_player
          on home_player.lineup_id = ls.home_lineup_id
        join v_lineup_player away_player
          on away_player.lineup_id = ls.away_lineup_id
        left join v_roster home_roster
          on home_roster.season_code = ls.season_code
         and home_roster.team_code = home_lineup.team_code
         and home_roster.player_id = home_player.player_id
        left join v_roster away_roster
          on away_roster.season_code = ls.season_code
         and away_roster.team_code = away_lineup.team_code
         and away_roster.player_id = away_player.player_id
        where not g.excluded_by_default

        union all

        select
            ls.season_code,
            ls.gamecode,
            ls.stint_index,
            away_player.player_id,
            away_roster.position_name as player_position_name,
            away_roster.height_cm as player_height_cm,
            away_roster.weight_kg as player_weight_kg,
            home_player.player_id as opponent_player_id,
            home_roster.position_name as opponent_position_name,
            home_roster.height_cm as opponent_height_cm,
            home_roster.weight_kg as opponent_weight_kg,
            greatest(ls.duration_seconds_raw, 0)::numeric as overlap_seconds
        from lineup_stint ls
        join requested_seasons rs using (season_code)
        join v_game g
          on g.season_code = ls.season_code
         and g.gamecode = ls.gamecode
        join lineup home_lineup
          on home_lineup.lineup_id = ls.home_lineup_id
        join lineup away_lineup
          on away_lineup.lineup_id = ls.away_lineup_id
        join v_lineup_player home_player
          on home_player.lineup_id = ls.home_lineup_id
        join v_lineup_player away_player
          on away_player.lineup_id = ls.away_lineup_id
        left join v_roster home_roster
          on home_roster.season_code = ls.season_code
         and home_roster.team_code = home_lineup.team_code
         and home_roster.player_id = home_player.player_id
        left join v_roster away_roster
          on away_roster.season_code = ls.season_code
         and away_roster.team_code = away_lineup.team_code
         and away_roster.player_id = away_player.player_id
        where not g.excluded_by_default
    ),
    lineup_matchup_weighted as (
        select
            *,
            overlap_seconds
            * case
                when player_position_name is null
                  or opponent_position_name is null
                    then 0.50
                when player_position_name = opponent_position_name
                    then 1.00
                else 0.30
              end
            * case
                when player_height_cm is null
                  or opponent_height_cm is null
                    then 0.70
                else greatest(
                    0.25,
                    1.00 - abs(player_height_cm - opponent_height_cm)::numeric / 30.0
                )
              end as matchup_weight
        from lineup_matchup_candidate
        where overlap_seconds > 0
    ),
    player_game_inferred_matchup as (
        select
            season_code,
            gamecode,
            player_id,
            round(
                sum(opponent_height_cm::numeric * matchup_weight)
                / nullif(
                    sum(matchup_weight) filter (where opponent_height_cm is not null),
                    0
                ),
                3
            ) as inferred_defender_height_cm,
            round(
                sum(opponent_weight_kg::numeric * matchup_weight)
                / nullif(
                    sum(matchup_weight) filter (where opponent_weight_kg is not null),
                    0
                ),
                3
            ) as inferred_defender_weight_kg,
            round(
                sum(matchup_weight) filter (
                    where opponent_position_name = player_position_name
                ) / nullif(sum(matchup_weight), 0),
                4
            ) as inferred_same_position_share,
            round(
                sum(matchup_weight) filter (
                    where opponent_position_name = 'Guard'
                ) / nullif(sum(matchup_weight), 0),
                4
            ) as inferred_guard_share,
            round(
                sum(matchup_weight) filter (
                    where opponent_position_name = 'Forward'
                ) / nullif(sum(matchup_weight), 0),
                4
            ) as inferred_forward_share,
            round(
                sum(matchup_weight) filter (
                    where opponent_position_name = 'Center'
                ) / nullif(sum(matchup_weight), 0),
                4
            ) as inferred_center_share
        from lineup_matchup_weighted
        group by 1, 2, 3
    ),
    team_rotation_player_game as (
        select
            psr.season_code,
            psr.gamecode,
            g.utc_date as game_tipoff_utc,
            l.team_code,
            psr.player_id,
            sum(psr.duration_seconds)::numeric as player_seconds,
            r.position_name,
            r.height_cm,
            r.weight_kg
        from player_stint_rows psr
        join v_game g
          on g.season_code = psr.season_code
         and g.gamecode = psr.gamecode
        join lineup l
          on l.lineup_id = psr.lineup_id
        left join v_roster r
          on r.season_code = psr.season_code
         and r.team_code = l.team_code
         and r.player_id = psr.player_id
        group by
            psr.season_code,
            psr.gamecode,
            g.utc_date,
            l.team_code,
            psr.player_id,
            r.position_name,
            r.height_cm,
            r.weight_kg
    ),
    team_game_rotation_profile as (
        select
            season_code,
            gamecode,
            game_tipoff_utc,
            team_code,
            round(
                sum(height_cm::numeric * player_seconds)
                / nullif(
                    sum(player_seconds) filter (where height_cm is not null),
                    0
                ),
                3
            ) as rotation_avg_height_cm,
            round(
                sum(weight_kg::numeric * player_seconds)
                / nullif(
                    sum(player_seconds) filter (where weight_kg is not null),
                    0
                ),
                3
            ) as rotation_avg_weight_kg,
            round(
                sum(player_seconds) filter (where position_name = 'Guard')
                / nullif(sum(player_seconds), 0),
                4
            ) as rotation_guard_share,
            round(
                sum(player_seconds) filter (where position_name = 'Forward')
                / nullif(sum(player_seconds), 0),
                4
            ) as rotation_forward_share,
            round(
                sum(player_seconds) filter (where position_name = 'Center')
                / nullif(sum(player_seconds), 0),
                4
            ) as rotation_center_share
        from team_rotation_player_game
        group by 1, 2, 3, 4
    ),
    team_game_position_profile as (
        select
            season_code,
            gamecode,
            game_tipoff_utc,
            team_code,
            position_name,
            round(
                sum(height_cm::numeric * player_seconds)
                / nullif(
                    sum(player_seconds) filter (where height_cm is not null),
                    0
                ),
                3
            ) as position_avg_height_cm,
            round(
                sum(weight_kg::numeric * player_seconds)
                / nullif(
                    sum(player_seconds) filter (where weight_kg is not null),
                    0
                ),
                3
            ) as position_avg_weight_kg,
            round(
                sum(player_seconds)
                / nullif(
                    sum(sum(player_seconds)) over (
                        partition by season_code, gamecode, team_code
                    ),
                    0
                ),
                4
            ) as position_rotation_share
        from team_rotation_player_game
        where position_name is not null
        group by 1, 2, 3, 4, 5
    ),
    model_positions(position_name) as (
        values ('Guard'), ('Forward'), ('Center')
    ),
    team_game_position_grid as (
        select
            tg.season_code,
            tg.gamecode,
            tg.utc_date as game_tipoff_utc,
            tg.team_code,
            mp.position_name,
            gp.position_avg_height_cm,
            gp.position_avg_weight_kg,
            gp.position_rotation_share
        from v_team_game tg
        join requested_seasons rs using (season_code)
        cross join model_positions mp
        left join team_game_position_profile gp
          on gp.season_code = tg.season_code
         and gp.gamecode = tg.gamecode
         and gp.team_code = tg.team_code
         and gp.position_name = mp.position_name
        where not tg.excluded_by_default
    ),
    team_position_features as (
        select
            *,
            lag(game_tipoff_utc) over position_w5
                as position_feature_cutoff_time,
            round(avg(position_avg_height_cm) over position_w5, 3)
                as pre_l5_position_avg_height_cm,
            round(avg(position_avg_weight_kg) over position_w5, 3)
                as pre_l5_position_avg_weight_kg,
            round(avg(position_rotation_share) over position_w5, 4)
                as pre_l5_position_rotation_share
        from team_game_position_grid
        window position_w5 as (
            partition by team_code, position_name
            order by game_tipoff_utc, gamecode
            rows between 5 preceding and 1 preceding
        )
    ),
    team_rotation_features as (
        select
            *,
            lag(game_tipoff_utc) over rotation_w5
                as rotation_feature_cutoff_time,
            round(avg(rotation_avg_height_cm) over rotation_w5, 3)
                as pre_l5_rotation_avg_height_cm,
            round(avg(rotation_avg_weight_kg) over rotation_w5, 3)
                as pre_l5_rotation_avg_weight_kg,
            round(avg(rotation_guard_share) over rotation_w5, 4)
                as pre_l5_rotation_guard_share,
            round(avg(rotation_forward_share) over rotation_w5, 4)
                as pre_l5_rotation_forward_share,
            round(avg(rotation_center_share) over rotation_w5, 4)
                as pre_l5_rotation_center_share
        from team_game_rotation_profile
        window rotation_w5 as (
            partition by team_code
            order by game_tipoff_utc, gamecode
            rows between 5 preceding and 1 preceding
        )
    ),
    event_score_context as (
        select
            e.*,
            lag(e.score_home) over event_order as pre_event_score_home,
            lag(e.score_away) over event_order as pre_event_score_away
        from game_event e
        join requested_seasons rs using (season_code)
        window event_order as (
            partition by e.season_code, e.gamecode
            order by e.ingest_index
        )
    ),
    player_game_shot_context as (
        select
            e.season_code,
            e.gamecode,
            e.player_id,
            count(*) as pbp_fga,
            count(*) filter (
                where vp.seconds_remaining_at_start <= 180
                  and case
                        when e.codeteam = g.home_team_code
                            then coalesce(e.pre_event_score_home, e.score_home)
                               - coalesce(e.pre_event_score_away, e.score_away)
                        else coalesce(e.pre_event_score_away, e.score_away)
                           - coalesce(e.pre_event_score_home, e.score_home)
                      end <= -4
            ) as late_trailing_fga,
            count(*) filter (
                where e.elapsed_seconds_raw is not null
                  and vp.start_seconds_elapsed is not null
                  and e.elapsed_seconds_raw - vp.start_seconds_elapsed between 18 and 60
            ) as late_clock_proxy_fga,
            count(*) filter (
                where (
                    vp.seconds_remaining_at_start <= 180
                    and case
                          when e.codeteam = g.home_team_code
                              then coalesce(e.pre_event_score_home, e.score_home)
                                 - coalesce(e.pre_event_score_away, e.score_away)
                          else coalesce(e.pre_event_score_away, e.score_away)
                             - coalesce(e.pre_event_score_home, e.score_home)
                        end <= -4
                )
                or (
                    e.elapsed_seconds_raw is not null
                    and vp.start_seconds_elapsed is not null
                    and e.elapsed_seconds_raw - vp.start_seconds_elapsed between 18 and 60
                )
            ) as situational_fga
        from event_score_context e
        join v_game g
          on g.season_code = e.season_code
         and g.gamecode = e.gamecode
        left join v_possession vp
          on vp.season_code = e.season_code
         and vp.gamecode = e.gamecode
         and vp.possession_index = e.possession_index
        where e.playtype in ('2FGM', '2FGA', '3FGM', '3FGA')
          and e.player_id is not null
          and not g.excluded_by_default
        group by 1, 2, 3
    ),
    game_script_context as (
        select
            e.season_code,
            e.gamecode,
            bool_or(e.period > 4) as went_overtime
        from game_event e
        join requested_seasons rs using (season_code)
        group by 1, 2
    ),
    player_base as (
        select
            p.season_code,
            p.gamecode,
            p.utc_date as game_tipoff_utc,
            p.utc_date::date as game_date,
            p.player_id,
            p.player_name,
            p.team_code,
            p.opponent_team_code,
            tg.is_home,
            p.is_starter,
            pr.position_name as player_position_name,
            pr.height_cm as player_height_cm,
            pr.weight_kg as player_weight_kg,
            im.inferred_defender_height_cm as matchup_defender_height_cm,
            im.inferred_defender_weight_kg as matchup_defender_weight_kg,
            im.inferred_same_position_share as matchup_same_position_share,
            im.inferred_guard_share as matchup_guard_share,
            im.inferred_forward_share as matchup_forward_share,
            im.inferred_center_share as matchup_center_share,
            coalesce(sc.pbp_fga, 0) as pbp_fga,
            coalesce(sc.late_trailing_fga, 0) as late_trailing_fga,
            coalesce(sc.late_clock_proxy_fga, 0) as late_clock_proxy_fga,
            coalesce(sc.situational_fga, 0) as situational_fga,
            greatest(
                p.field_goals_attempted - coalesce(sc.situational_fga, 0),
                0
            ) as context_neutral_fga,
            p.fouls_commited,
            tg.points - tg.opponent_points as team_final_margin,
            abs(tg.points - tg.opponent_points) >= 15 as game_was_blowout,
            abs(tg.points - tg.opponent_points) <= 5 as game_was_close,
            coalesce(gc.went_overtime, false) as game_went_overtime,
            p.points,
            p.field_goals_made,
            p.field_goals_attempted,
            p.three_pointers_made,
            p.three_pointers_attempted,
            p.free_throws_made,
            p.free_throws_attempted,
            p.{seconds} as seconds_played,
            round(
                p.points::numeric
                / nullif(
                    2.0 * (
                        p.field_goals_attempted::numeric
                        + 0.44 * p.free_throws_attempted::numeric
                    ),
                    0
                ),
                4
            ) as game_ts_proxy,

            coalesce(pg.pbp_offensive_possessions, 0)
                as pbp_offensive_possessions,
            coalesce(pg.pbp_defensive_possessions, 0)
                as pbp_defensive_possessions,
            coalesce(pg.pbp_points_for, 0) as pbp_points_for,
            coalesce(pg.pbp_points_against, 0) as pbp_points_against,

            round(
                100.0 * p.field_goals_attempted::numeric
                    / nullif(pg.pbp_offensive_possessions, 0),
                4
            ) as role2_fga_per_100_possessions,
            round(
                100.0 * (
                    p.field_goals_attempted::numeric
                    + 0.44 * p.free_throws_attempted::numeric
                ) / nullif(pg.pbp_offensive_possessions, 0),
                4
            ) as role2_scoring_opportunities_per_100,
            round(
                p.field_goals_attempted::numeric
                    / nullif(tg.field_goals_attempted, 0),
                4
            ) as role2_team_fga_share,
            round(
                (
                    p.field_goals_attempted::numeric
                    + 0.44 * p.free_throws_attempted::numeric
                ) / nullif(
                    tg.field_goals_attempted::numeric
                        + 0.44 * tg.free_throws_attempted::numeric,
                    0
                ),
                4
            ) as role2_team_scoring_opportunity_share,
            dense_rank() over (
                partition by p.season_code, p.gamecode, p.team_code
                order by (
                    p.field_goals_attempted::numeric
                    + 0.44 * p.free_throws_attempted::numeric
                ) desc, p.player_id
            ) as role2_option_rank,
            coalesce(r2.role2_teammate_contexts, 0) as role2_teammate_contexts,
            r2.role2_mean_teammate_off_fga_uplift,
            r2.role2_max_teammate_off_fga_uplift,

            coalesce(sg.pbp_stint_count, 0) as pbp_stint_count,
            sg.pbp_avg_stint_seconds,
            sg.pbp_max_stint_seconds,
            sg.pbp_stint_seconds,
            coalesce(sg.pbp_distinct_lineups, 0) as pbp_distinct_lineups,
            sg.pbp_primary_lineup_share,

            pair.pre_last_top_pair_shared_minutes,
            pair.pre_last_top_pair_net_rating,
            pair.pre_l5_top_pair_shared_minutes,
            pair.pre_l5_top_pair_net_rating,
            pair.pre_l5_top_pair_repeat_rate,
            triple.pre_last_top_triple_shared_minutes,
            triple.pre_last_top_triple_net_rating,
            triple.pre_l5_top_triple_shared_minutes,
            triple.pre_l5_top_triple_net_rating,
            triple.pre_l5_top_triple_repeat_rate,
            five.pre_last_top_lineup_shared_minutes,
            five.pre_last_top_lineup_net_rating,
            five.pre_l5_top_lineup_shared_minutes,
            five.pre_l5_top_lineup_net_rating,
            five.pre_l5_top_lineup_repeat_rate
        from v_player_game p
        join requested_seasons rs using (season_code)
        join v_team_game tg
          on tg.season_code = p.season_code
         and tg.gamecode = p.gamecode
         and tg.team_code = p.team_code
        left join v_roster pr
          on pr.season_code = p.season_code
         and pr.team_code = p.team_code
         and pr.player_id = p.player_id
        left join player_game_inferred_matchup im
          on im.season_code = p.season_code
         and im.gamecode = p.gamecode
         and im.player_id = p.player_id
        left join player_game_shot_context sc
          on sc.season_code = p.season_code
         and sc.gamecode = p.gamecode
         and sc.player_id = p.player_id
        left join game_script_context gc
          on gc.season_code = p.season_code
         and gc.gamecode = p.gamecode
        left join pbp_possession_game pg
          on pg.season_code = p.season_code
         and pg.gamecode = p.gamecode
         and pg.player_id = p.player_id
        left join pbp_stint_game sg
          on sg.season_code = p.season_code
         and sg.gamecode = p.gamecode
         and sg.player_id = p.player_id
        left join player_role2_on_off_game r2
          on r2.season_code = p.season_code
         and r2.gamecode = p.gamecode
         and r2.player_id = p.player_id
        left join player_top_pair_pregame pair
          on pair.season_code = p.season_code
         and pair.gamecode = p.gamecode
         and pair.player_id = p.player_id
        left join player_top_triple_pregame triple
          on triple.season_code = p.season_code
         and triple.gamecode = p.gamecode
         and triple.player_id = p.player_id
        left join player_top_lineup_pregame five
          on five.season_code = p.season_code
         and five.gamecode = p.gamecode
         and five.player_id = p.player_id
        where p.seconds_official > 0
          and not p.excluded_by_default
    ),
    player_role_baseline as (
        select
            *,
            lag(is_starter) over role_all as role_prev_starter,
            lag(is_home) over role_all as role_prev_is_home,
            lag(team_code) over role_all as role_prev_team_code,
            lag(opponent_team_code) over role_all as role_prev_opponent_team_code,
            round(
                avg(seconds_played::numeric / 60.0) over role_w5,
                3
            ) as role_pre_l5_minutes,
            round(
                avg(field_goals_attempted::numeric) over role_w5,
                3
            ) as role_pre_l5_fga
        from player_base
        window
            role_all as (
                partition by player_id
                order by game_tipoff_utc, gamecode
            ),
            role_w5 as (
                partition by player_id
                order by game_tipoff_utc, gamecode
                rows between 5 preceding and 1 preceding
            )
    ),
    player_role_events as (
        select
            *,
            case
                when role_pre_l5_minutes is not null
                 and seconds_played::numeric / 60.0
                    >= greatest(role_pre_l5_minutes + 5.0, role_pre_l5_minutes * 1.20)
                    then 1
                else 0
            end as minute_spike_event,
            case
                when role_pre_l5_minutes is not null
                 and seconds_played::numeric / 60.0
                    <= least(role_pre_l5_minutes - 5.0, role_pre_l5_minutes * 0.80)
                    then 1
                else 0
            end as minute_drop_event,
            case
                when role_pre_l5_fga is not null
                 and field_goals_attempted
                    >= greatest(role_pre_l5_fga + 3.0, role_pre_l5_fga * 1.30)
                    then 1
                else 0
            end as fga_spike_event,
            case
                when role_pre_l5_fga is not null
                 and field_goals_attempted
                    <= least(role_pre_l5_fga - 3.0, role_pre_l5_fga * 0.70)
                    then 1
                else 0
            end as fga_drop_event,
            case
                when role_prev_starter is not null
                 and is_starter is distinct from role_prev_starter
                    then 1
                else 0
            end as starter_change_event,
            case
                when role_prev_starter = false and is_starter
                    then 1
                else 0
            end as starter_promotion_event,
            case
                when role_prev_starter = true and not is_starter
                    then 1
                else 0
            end as starter_demotion_event,
            case
                when role_prev_is_home is not null
                 and is_home is distinct from role_prev_is_home
                    then 1
                else 0
            end as home_away_switch_event,
            round(({travel_km_sql})::numeric, 1) as travel_air_km_to_game
        from player_role_baseline
    ),
    player_role_reasons as (
        select
            *,
            case
                when minute_drop_event = 1 and fouls_commited >= 4 then 1 else 0
            end as minute_drop_foul_context,
            case
                when minute_drop_event = 1 and game_was_blowout then 1 else 0
            end as minute_drop_blowout_context,
            case
                when minute_drop_event = 1 and starter_demotion_event = 1 then 1 else 0
            end as minute_drop_demotion_context,
            case
                when minute_spike_event = 1 and game_went_overtime then 1 else 0
            end as minute_spike_overtime_context,
            case
                when minute_spike_event = 1 and game_was_close then 1 else 0
            end as minute_spike_close_game_context,
            case
                when minute_spike_event = 1 and starter_promotion_event = 1 then 1 else 0
            end as minute_spike_promotion_context,
            case
                when fga_spike_event = 1
                 and (
                    situational_fga >= 2
                    or situational_fga::numeric
                        / nullif(field_goals_attempted, 0) >= 0.25
                 )
                    then 1
                else 0
            end as fga_spike_situational_context,
            case
                when fga_spike_event = 1
                 and context_neutral_fga
                    >= greatest(role_pre_l5_fga + 3.0, role_pre_l5_fga * 1.30)
                    then 1
                else 0
            end as fga_spike_role_expansion_context,
            case
                when minute_drop_event = 1
                 and not (
                    fouls_commited >= 4
                    or game_was_blowout
                    or starter_demotion_event = 1
                 )
                    then 1
                else 0
            end as minute_drop_unexplained_context,
            case
                when minute_spike_event = 1
                 and not (
                    game_went_overtime
                    or game_was_close
                    or starter_promotion_event = 1
                 )
                    then 1
                else 0
            end as minute_spike_unexplained_context,
            case
                when fga_spike_event = 1
                 and not (
                    situational_fga >= 2
                    or situational_fga::numeric
                        / nullif(field_goals_attempted, 0) >= 0.25
                    or context_neutral_fga
                        >= greatest(role_pre_l5_fga + 3.0, role_pre_l5_fga * 1.30)
                 )
                    then 1
                else 0
            end as fga_spike_unexplained_context
        from player_role_events
    ),
    player_hand_baseline as (
        select
            *,
            count(*) over hand_w10 as hand_pre_history_games,
            round(avg(game_ts_proxy) over hand_w10, 4)
                as hand_pre_l10_ts_mean,
            round(stddev_samp(game_ts_proxy) over hand_w10, 4)
                as hand_pre_l10_ts_std,
            round(avg(seconds_played::numeric / 60.0) over hand_w5, 3)
                as hand_pre_l5_minutes,
            round(avg(field_goals_attempted::numeric) over hand_w5, 3)
                as hand_pre_l5_fga
        from player_role_reasons
        window
            hand_w5 as (
                partition by player_id
                order by game_tipoff_utc, gamecode
                rows between 5 preceding and 1 preceding
            ),
            hand_w10 as (
                partition by player_id
                order by game_tipoff_utc, gamecode
                rows between 10 preceding and 1 preceding
            )
    ),
    player_hand_classified as (
        select
            *,
            round(game_ts_proxy - hand_pre_l10_ts_mean, 4)
                as hand_eff_delta_vs_prior_l10,
            case
                when hand_pre_history_games < 5
                  or game_ts_proxy is null
                  or hand_pre_l10_ts_mean is null
                    then 0
                when game_ts_proxy - hand_pre_l10_ts_mean
                    >= greatest(
                        0.05,
                        0.75 * coalesce(hand_pre_l10_ts_std, 0)
                    )
                    then 1
                when game_ts_proxy - hand_pre_l10_ts_mean
                    <= -greatest(
                        0.05,
                        0.75 * coalesce(hand_pre_l10_ts_std, 0)
                    )
                    then -1
                else 0
            end as hand_state
        from player_hand_baseline
    ),
    player_hand_changes as (
        select
            *,
            lag(hand_state) over hand_all as prev_hand_state,
            case
                when lag(hand_state) over hand_all is distinct from hand_state
                    then 1
                else 0
            end as hand_state_change
        from player_hand_classified
        window hand_all as (
            partition by player_id
            order by game_tipoff_utc, gamecode
        )
    ),
    player_hand_grouped as (
        select
            *,
            sum(hand_state_change) over (
                partition by player_id
                order by game_tipoff_utc, gamecode
                rows between unbounded preceding and current row
            ) as hand_group
        from player_hand_changes
    ),
    player_hand_runs as (
        select
            *,
            row_number() over (
                partition by player_id, hand_group
                order by game_tipoff_utc, gamecode
            ) as hand_run_length
        from player_hand_grouped
    ),
    player_hand_events as (
        select
            *,
            lag(hand_run_length) over hand_all as prev_hand_run_length,
            case
                when hand_state_change = 1 and prev_hand_state = 1
                    then lag(hand_run_length) over hand_all
            end as ended_hot_episode_length,
            case
                when hand_state_change = 1 and prev_hand_state = -1
                    then lag(hand_run_length) over hand_all
            end as ended_cold_episode_length,
            case
                when hand_state_change = 1
                 and prev_hand_state = 1
                 and (
                    seconds_played::numeric / 60.0
                        <= 0.80 * hand_pre_l5_minutes
                    or field_goals_attempted::numeric
                        <= 0.80 * hand_pre_l5_fga
                 )
                    then 1
                else 0
            end as hot_break_role_drop,
            case
                when hand_state_change = 1
                 and prev_hand_state = 1
                 and not (
                    seconds_played::numeric / 60.0
                        <= 0.80 * hand_pre_l5_minutes
                    or field_goals_attempted::numeric
                        <= 0.80 * hand_pre_l5_fga
                 )
                    then 1
                else 0
            end as hot_break_efficiency_reversion,
            case
                when hand_state_change = 1
                 and prev_hand_state = -1
                 and (
                    seconds_played::numeric / 60.0
                        >= 1.15 * hand_pre_l5_minutes
                    or field_goals_attempted::numeric
                        >= 1.20 * hand_pre_l5_fga
                 )
                    then 1
                else 0
            end as cold_break_role_expansion,
            case
                when hand_state_change = 1
                 and prev_hand_state = -1
                 and not (
                    seconds_played::numeric / 60.0
                        >= 1.15 * hand_pre_l5_minutes
                    or field_goals_attempted::numeric
                        >= 1.20 * hand_pre_l5_fga
                 )
                    then 1
                else 0
            end as cold_break_efficiency_recovery
        from player_hand_runs
        window hand_all as (
            partition by player_id
            order by game_tipoff_utc, gamecode
        )
    ),
    player_features as (
        select
            *,
            count(*) over w10 as pre_history_games,
            count(*) over wseason as pre_current_season_games,
            round(
                coalesce(
                    avg(points::numeric) over wseason,
                    avg(points::numeric) over whistory
                ),
                3
            ) as pre_naive_points_mean,

            max(player_height_cm) over wprofile as pre_player_height_cm,
            max(player_weight_kg) over wprofile as pre_player_weight_kg,
            max(case when player_position_name = 'Guard' then 1 else 0 end)
                over wprofile as pre_player_is_guard,
            max(case when player_position_name = 'Forward' then 1 else 0 end)
                over wprofile as pre_player_is_forward,
            max(case when player_position_name = 'Center' then 1 else 0 end)
                over wprofile as pre_player_is_center,

            count(matchup_defender_height_cm) over w20
                as pre_matchup_profile_games,
            round(
                (regr_slope(
                    60.0 * points::double precision
                        / nullif(seconds_played, 0),
                    matchup_defender_height_cm::double precision
                ) over w20)::numeric,
                5
            ) as pre_matchup_ppm_vs_defender_height_slope,
            round(
                (regr_slope(
                    game_ts_proxy::double precision,
                    matchup_defender_height_cm::double precision
                ) over w20)::numeric,
                5
            ) as pre_matchup_ts_vs_defender_height_slope,
            round(
                avg(
                    60.0 * points::numeric / nullif(seconds_played, 0)
                ) filter (
                    where matchup_defender_height_cm >= player_height_cm + 4
                ) over w20,
                4
            ) as pre_ppm_vs_taller_defender_profile,
            round(
                avg(
                    60.0 * points::numeric / nullif(seconds_played, 0)
                ) filter (
                    where matchup_defender_height_cm
                        between player_height_cm - 3 and player_height_cm + 3
                ) over w20,
                4
            ) as pre_ppm_vs_similar_defender_profile,
            round(
                avg(
                    60.0 * points::numeric / nullif(seconds_played, 0)
                ) filter (
                    where matchup_defender_height_cm <= player_height_cm - 4
                ) over w20,
                4
            ) as pre_ppm_vs_shorter_defender_profile,
            round(
                avg(game_ts_proxy) filter (
                    where matchup_defender_height_cm >= player_height_cm + 4
                ) over w20,
                4
            ) as pre_ts_vs_taller_defender_profile,
            round(
                avg(game_ts_proxy) filter (
                    where matchup_defender_height_cm
                        between player_height_cm - 3 and player_height_cm + 3
                ) over w20,
                4
            ) as pre_ts_vs_similar_defender_profile,
            round(
                avg(game_ts_proxy) filter (
                    where matchup_defender_height_cm <= player_height_cm - 4
                ) over w20,
                4
            ) as pre_ts_vs_shorter_defender_profile,
            round(avg(matchup_same_position_share) over w20, 4)
                as pre_avg_same_position_matchup_share,
            round(
                (regr_slope(
                    60.0 * points::double precision
                        / nullif(seconds_played, 0),
                    matchup_guard_share::double precision
                ) over w20)::numeric,
                5
            ) as pre_matchup_ppm_vs_guard_share_slope,
            round(
                (regr_slope(
                    60.0 * points::double precision
                        / nullif(seconds_played, 0),
                    matchup_center_share::double precision
                ) over w20)::numeric,
                5
            ) as pre_matchup_ppm_vs_center_share_slope,

            coalesce(prev_hand_state, 0) as pre_last_hand_state,
            round(
                lag(hand_eff_delta_vs_prior_l10) over wall,
                4
            ) as pre_last_ts_delta_vs_prior_l10,
            case
                when prev_hand_state = 1
                    then coalesce(prev_hand_run_length, 0)
                else 0
            end as pre_hot_streak_games,
            case
                when prev_hand_state = -1
                    then coalesce(prev_hand_run_length, 0)
                else 0
            end as pre_cold_streak_games,

            count(ended_hot_episode_length) over whistory
                as pre_completed_hot_episodes,
            count(ended_cold_episode_length) over whistory
                as pre_completed_cold_episodes,
            round(avg(ended_hot_episode_length::numeric) over whistory, 3)
                as pre_avg_hot_episode_games,
            round(avg(ended_cold_episode_length::numeric) over whistory, 3)
                as pre_avg_cold_episode_games,
            max(ended_hot_episode_length) over whistory
                as pre_max_hot_episode_games,
            max(ended_cold_episode_length) over whistory
                as pre_max_cold_episode_games,

            round(
                (sum(hot_break_role_drop) over whistory)::numeric
                / nullif(count(ended_hot_episode_length) over whistory, 0),
                4
            ) as pre_hot_break_role_drop_rate,
            round(
                (sum(hot_break_efficiency_reversion) over whistory)::numeric
                / nullif(count(ended_hot_episode_length) over whistory, 0),
                4
            ) as pre_hot_break_eff_reversion_rate,
            round(
                (sum(cold_break_role_expansion) over whistory)::numeric
                / nullif(count(ended_cold_episode_length) over whistory, 0),
                4
            ) as pre_cold_break_role_expansion_rate,
            round(
                (sum(cold_break_efficiency_recovery) over whistory)::numeric
                / nullif(count(ended_cold_episode_length) over whistory, 0),
                4
            ) as pre_cold_break_eff_recovery_rate,

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

            round(stddev_samp(seconds_played::numeric / 60.0) over w5, 3)
                as pre_l5_minutes_std,
            round(stddev_samp(seconds_played::numeric / 60.0) over w10, 3)
                as pre_l10_minutes_std,
            round(
                max(seconds_played::numeric / 60.0) over w10
                - min(seconds_played::numeric / 60.0) over w10,
                3
            ) as pre_l10_minutes_range,
            round(
                stddev_samp(seconds_played::numeric / 60.0) over w10
                / nullif(avg(seconds_played::numeric / 60.0) over w10, 0),
                4
            ) as pre_l10_minutes_cv,
            round(stddev_samp(field_goals_attempted::numeric) over w5, 3)
                as pre_l5_fga_std,
            round(stddev_samp(field_goals_attempted::numeric) over w10, 3)
                as pre_l10_fga_std,
            max(field_goals_attempted) over w10
                - min(field_goals_attempted) over w10
                as pre_l10_fga_range,
            round(
                stddev_samp(field_goals_attempted::numeric) over w10
                / nullif(avg(field_goals_attempted::numeric) over w10, 0),
                4
            ) as pre_l10_fga_cv,
            round(stddev_samp(points::numeric) over w10, 3)
                as pre_l10_points_std,
            max(points) over w10 - min(points) over w10
                as pre_l10_points_range,

            round(avg(minute_spike_event::numeric) over w10, 4)
                as pre_l10_minute_spike_rate,
            round(avg(minute_drop_event::numeric) over w10, 4)
                as pre_l10_minute_drop_rate,
            round(avg(fga_spike_event::numeric) over w10, 4)
                as pre_l10_fga_spike_rate,
            round(avg(fga_drop_event::numeric) over w10, 4)
                as pre_l10_fga_drop_rate,
            round(avg(starter_change_event::numeric) over w10, 4)
                as pre_l10_starter_change_rate,

            round(
                (sum(minute_drop_foul_context) over w20)::numeric
                / nullif(sum(minute_drop_event) over w20, 0),
                4
            ) as pre_minute_drop_foul_reason_share,
            round(
                (sum(minute_drop_blowout_context) over w20)::numeric
                / nullif(sum(minute_drop_event) over w20, 0),
                4
            ) as pre_minute_drop_blowout_reason_share,
            round(
                (sum(minute_drop_demotion_context) over w20)::numeric
                / nullif(sum(minute_drop_event) over w20, 0),
                4
            ) as pre_minute_drop_demotion_reason_share,
            round(
                (sum(minute_drop_unexplained_context) over w20)::numeric
                / nullif(sum(minute_drop_event) over w20, 0),
                4
            ) as pre_minute_drop_unexplained_share,
            round(
                (sum(minute_spike_overtime_context) over w20)::numeric
                / nullif(sum(minute_spike_event) over w20, 0),
                4
            ) as pre_minute_spike_overtime_reason_share,
            round(
                (sum(minute_spike_close_game_context) over w20)::numeric
                / nullif(sum(minute_spike_event) over w20, 0),
                4
            ) as pre_minute_spike_close_reason_share,
            round(
                (sum(minute_spike_promotion_context) over w20)::numeric
                / nullif(sum(minute_spike_event) over w20, 0),
                4
            ) as pre_minute_spike_promotion_reason_share,
            round(
                (sum(minute_spike_unexplained_context) over w20)::numeric
                / nullif(sum(minute_spike_event) over w20, 0),
                4
            ) as pre_minute_spike_unexplained_share,
            round(
                (sum(fga_spike_situational_context) over w20)::numeric
                / nullif(sum(fga_spike_event) over w20, 0),
                4
            ) as pre_fga_spike_situational_reason_share,
            round(
                (sum(fga_spike_role_expansion_context) over w20)::numeric
                / nullif(sum(fga_spike_event) over w20, 0),
                4
            ) as pre_fga_spike_role_expansion_share,
            round(
                (sum(fga_spike_unexplained_context) over w20)::numeric
                / nullif(sum(fga_spike_event) over w20, 0),
                4
            ) as pre_fga_spike_unexplained_share,

            lag(minute_spike_event) over wall as pre_last_minute_spike,
            lag(minute_drop_event) over wall as pre_last_minute_drop,
            lag(fga_spike_event) over wall as pre_last_fga_spike,
            lag(fga_drop_event) over wall as pre_last_fga_drop,
            lag(starter_change_event) over wall as pre_last_starter_change,
            lag(minute_drop_foul_context) over wall
                as pre_last_minute_drop_foul_context,
            lag(minute_drop_blowout_context) over wall
                as pre_last_minute_drop_blowout_context,
            lag(minute_spike_overtime_context) over wall
                as pre_last_minute_spike_overtime_context,
            lag(minute_spike_close_game_context) over wall
                as pre_last_minute_spike_close_context,
            lag(fga_spike_situational_context) over wall
                as pre_last_fga_spike_situational_context,
            lag(fga_spike_role_expansion_context) over wall
                as pre_last_fga_spike_role_expansion_context,
            lag(fga_spike_unexplained_context) over wall
                as pre_last_fga_spike_unexplained_context,

            round(lag(seconds_played::numeric / 60.0) over wall, 3)
                as pre_last_minutes,
            lag(field_goals_attempted) over wall as pre_last_fga,
            lag(three_pointers_attempted) over wall as pre_last_3pa,
            lag(context_neutral_fga) over wall as pre_last_context_neutral_fga,
            lag(situational_fga) over wall as pre_last_situational_fga,
            lag(late_trailing_fga) over wall as pre_last_late_trailing_fga,
            lag(late_clock_proxy_fga) over wall as pre_last_late_clock_proxy_fga,
            round(
                lag(
                    situational_fga::numeric
                    / nullif(field_goals_attempted, 0)
                ) over wall,
                4
            ) as pre_last_situational_fga_share,
            round(avg(context_neutral_fga::numeric) over w5, 3)
                as pre_l5_context_neutral_fga,
            round(avg(context_neutral_fga::numeric) over w10, 3)
                as pre_l10_context_neutral_fga,
            round(
                (sum(situational_fga) over w5)::numeric
                / nullif(sum(field_goals_attempted) over w5, 0),
                4
            ) as pre_l5_situational_fga_share,
            round(
                (sum(situational_fga) over w10)::numeric
                / nullif(sum(field_goals_attempted) over w10, 0),
                4
            ) as pre_l10_situational_fga_share,
            round(
                (sum(late_trailing_fga) over w5)::numeric
                / nullif(sum(field_goals_attempted) over w5, 0),
                4
            ) as pre_l5_late_trailing_fga_share,
            round(
                (sum(late_clock_proxy_fga) over w5)::numeric
                / nullif(sum(field_goals_attempted) over w5, 0),
                4
            ) as pre_l5_late_clock_proxy_fga_share,
            round(
                (sum(three_pointers_attempted) over w10)::numeric
                / nullif(sum(field_goals_attempted) over w10, 0),
                4
            ) as pre_l10_3pa_share,

            round(avg(case when is_starter then 1.0 else 0.0 end) over w5, 4)
                as pre_l5_starter_rate,
            round(avg(case when is_starter then 1.0 else 0.0 end) over w10, 4)
                as pre_l10_starter_rate,
            lag(is_starter) over wall as pre_last_was_starter,
            lag(game_tipoff_utc) over wall as player_feature_cutoff_time,

            round(
                extract(
                    epoch from (
                        game_tipoff_utc
                        - lag(game_tipoff_utc) over wall
                    )
                ) / 86400.0,
                3
            ) as pre_days_rest,
            round(
                extract(
                    epoch from (
                        game_tipoff_utc
                        - lag(game_tipoff_utc) over wall
                    )
                ) / 3600.0,
                2
            ) as pre_hours_rest,
            travel_air_km_to_game as pre_travel_air_km,
            lag(not is_home) over wall as pre_last_was_away,
            round(
                avg(case when not is_home then 1.0 else 0.0 end) over w5,
                4
            ) as pre_l5_away_rate,
            sum(case when not is_home then 1 else 0 end) over w3
                as pre_away_games_last_3,
            sum(case when not is_home then 1 else 0 end) over w7d
                as pre_away_games_last_7d,
            sum(case when not is_home then 1 else 0 end) over w14d
                as pre_away_games_last_14d,
            round(avg(home_away_switch_event::numeric) over w5, 4)
                as pre_l5_home_away_switch_rate,
            count(*) over w7d as pre_games_last_7d,
            count(*) over w14d as pre_games_last_14d,
            round(sum(seconds_played::numeric / 60.0) over w7d, 3)
                as pre_minutes_last_7d,
            round(sum(seconds_played::numeric / 60.0) over w14d, 3)
                as pre_minutes_last_14d,

            round(
                (sum(points) over w3)::numeric
                / nullif(
                    2.0 * (
                        (sum(field_goals_attempted) over w3)::numeric
                        + 0.44 * (sum(free_throws_attempted) over w3)::numeric
                    ),
                    0
                ),
                4
            ) as pre_l3_ts_proxy,
            round(
                (sum(points) over w5)::numeric
                / nullif(
                    2.0 * (
                        (sum(field_goals_attempted) over w5)::numeric
                        + 0.44 * (sum(free_throws_attempted) over w5)::numeric
                    ),
                    0
                ),
                4
            ) as pre_l5_ts_proxy,
            round(
                (sum(points) over w10)::numeric
                / nullif(
                    2.0 * (
                        (sum(field_goals_attempted) over w10)::numeric
                        + 0.44 * (sum(free_throws_attempted) over w10)::numeric
                    ),
                    0
                ),
                4
            ) as pre_l10_ts_proxy,

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
                (
                    sum(field_goals_made - three_pointers_made) over w10
                )::numeric
                / nullif(
                    sum(field_goals_attempted - three_pointers_attempted) over w10,
                    0
                ),
                4
            ) as pre_l10_2p_pct,
            round(
                (sum(three_pointers_made) over w10)::numeric
                / nullif(sum(three_pointers_attempted) over w10, 0),
                4
            ) as pre_l10_3p_pct,
            round(
                (sum(free_throws_made) over w10)::numeric
                / nullif(sum(free_throws_attempted) over w10, 0),
                4
            ) as pre_l10_ft_pct,

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

            round(avg(pbp_stint_count::numeric) over w5, 3)
                as pre_l5_pbp_stint_count,
            round(avg(pbp_distinct_lineups::numeric) over w5, 3)
                as pre_l5_pbp_distinct_lineups,
            round(avg(pbp_avg_stint_seconds) over w5, 3)
                as pre_l5_pbp_avg_stint_seconds,
            round(avg(pbp_max_stint_seconds::numeric) over w5, 3)
                as pre_l5_pbp_max_stint_seconds,
            round(avg(pbp_primary_lineup_share) over w5, 4)
                as pre_l5_pbp_primary_lineup_share,

            lag(role2_fga_per_100_possessions) over wall
                as pre_role2_last_fga_per_100_possessions,
            round(avg(role2_fga_per_100_possessions) over w3, 4)
                as pre_role2_l3_fga_per_100_possessions,
            round(avg(role2_fga_per_100_possessions) over w5, 4)
                as pre_role2_l5_fga_per_100_possessions,
            round(avg(role2_fga_per_100_possessions) over w10, 4)
                as pre_role2_l10_fga_per_100_possessions,
            round(
                avg(role2_fga_per_100_possessions) over w3
                    - avg(role2_fga_per_100_possessions) over w10,
                4
            ) as pre_role2_fga_per_100_trend_l3_vs_l10,

            lag(role2_scoring_opportunities_per_100) over wall
                as pre_role2_last_scoring_opportunities_per_100,
            round(avg(role2_scoring_opportunities_per_100) over w5, 4)
                as pre_role2_l5_scoring_opportunities_per_100,
            round(avg(role2_scoring_opportunities_per_100) over w10, 4)
                as pre_role2_l10_scoring_opportunities_per_100,

            round(avg(role2_team_fga_share) over w3, 4)
                as pre_role2_l3_team_fga_share,
            round(avg(role2_team_fga_share) over w5, 4)
                as pre_role2_l5_team_fga_share,
            round(avg(role2_team_fga_share) over w10, 4)
                as pre_role2_l10_team_fga_share,
            round(
                avg(role2_team_fga_share) over w3
                    - avg(role2_team_fga_share) over w10,
                4
            ) as pre_role2_team_fga_share_trend_l3_vs_l10,
            round(avg(role2_team_scoring_opportunity_share) over w5, 4)
                as pre_role2_l5_team_scoring_opportunity_share,

            lag(role2_option_rank) over wall as pre_role2_last_option_rank,
            round(avg(role2_option_rank::numeric) over w5, 3)
                as pre_role2_l5_option_rank,
            round(
                avg(case when role2_option_rank = 1 then 1.0 else 0.0 end) over w5,
                4
            ) as pre_role2_l5_primary_option_rate,
            round(
                avg(case when role2_option_rank <= 2 then 1.0 else 0.0 end) over w5,
                4
            ) as pre_role2_l5_top2_option_rate,

            round(avg(role2_teammate_contexts::numeric) over w5, 3)
                as pre_role2_l5_teammate_contexts,
            round(avg(role2_mean_teammate_off_fga_uplift) over w5, 4)
                as pre_role2_l5_mean_teammate_off_fga_uplift,
            round(avg(role2_max_teammate_off_fga_uplift) over w5, 4)
                as pre_role2_l5_max_teammate_off_fga_uplift,
            round(avg(role2_max_teammate_off_fga_uplift) over w10, 4)
                as pre_role2_l10_max_teammate_off_fga_uplift
        from player_hand_events
        window
            wall as (
                partition by player_id
                order by game_tipoff_utc, gamecode
            ),
            wprofile as (
                partition by season_code, player_id
                order by game_tipoff_utc, gamecode
                rows between unbounded preceding and current row
            ),
            whistory as (
                partition by player_id
                order by game_tipoff_utc, gamecode
                rows between unbounded preceding and 1 preceding
            ),
            wseason as (
                partition by season_code, player_id
                order by game_tipoff_utc, gamecode
                rows between unbounded preceding and 1 preceding
            ),
            w3 as (
                partition by player_id
                order by game_tipoff_utc, gamecode
                rows between 3 preceding and 1 preceding
            ),
            w5 as (
                partition by player_id
                order by game_tipoff_utc, gamecode
                rows between 5 preceding and 1 preceding
            ),
            w10 as (
                partition by player_id
                order by game_tipoff_utc, gamecode
                rows between 10 preceding and 1 preceding
            ),
            w20 as (
                partition by player_id
                order by game_tipoff_utc, gamecode
                rows between 20 preceding and 1 preceding
            ),
            w7d as (
                partition by player_id
                order by game_tipoff_utc
                range between interval '7 days' preceding
                    and interval '1 microsecond' preceding
            ),
            w14d as (
                partition by player_id
                order by game_tipoff_utc
                range between interval '14 days' preceding
                    and interval '1 microsecond' preceding
            )
    ),
    el_player_identity as (
        select
            source_player_id as player_id,
            athlete_id
        from athlete_source_identity
        where source = 'EL'
          and match_status in ('auto_link', 'manual_verified')
    ),
    acb_history_ranked as (
        select
            pf.season_code as target_season_code,
            pf.gamecode as target_gamecode,
            pf.player_id as target_player_id,
            pf.game_tipoff_utc as target_tipoff_utc,
            h.utc_date as acb_game_date,
            h.source_game_id as acb_game_id,
            h.minutes_seconds,
            h.points,
            h.field_goals_attempted,
            h.is_starter,
            row_number() over (
                partition by pf.season_code, pf.gamecode, pf.player_id
                order by h.utc_date desc, h.source_game_id desc
            ) as acb_recency_rank
        from player_features pf
        join el_player_identity epi
          on epi.player_id = pf.player_id
        join v_athlete_game_history h
          on h.athlete_id = epi.athlete_id
         and h.source = 'ACB'
         and not h.excluded_by_default
         and h.utc_date < pf.game_tipoff_utc
         and h.utc_date >= pf.game_tipoff_utc - interval '45 days'
    ),
    acb_features as (
        select
            target_season_code as season_code,
            target_gamecode as gamecode,
            target_player_id as player_id,
            max(acb_game_date) as acb_feature_cutoff_time,
            count(*) filter (where acb_recency_rank <= 5)
                as pre_acb_l5_games,
            round(
                avg(minutes_seconds::numeric / 60.0)
                    filter (where acb_recency_rank <= 3),
                3
            ) as pre_acb_l3_minutes,
            round(
                avg(minutes_seconds::numeric / 60.0)
                    filter (where acb_recency_rank <= 5),
                3
            ) as pre_acb_l5_minutes,
            round(
                avg(field_goals_attempted::numeric)
                    filter (where acb_recency_rank <= 3),
                3
            ) as pre_acb_l3_fga,
            round(
                avg(field_goals_attempted::numeric)
                    filter (where acb_recency_rank <= 5),
                3
            ) as pre_acb_l5_fga,
            round(
                avg(points::numeric)
                    filter (where acb_recency_rank <= 3),
                3
            ) as pre_acb_l3_points,
            round(
                avg(points::numeric)
                    filter (where acb_recency_rank <= 5),
                3
            ) as pre_acb_l5_points,
            round(
                avg(case when is_starter then 1.0 else 0.0 end)
                    filter (where acb_recency_rank <= 5),
                4
            ) as pre_acb_l5_starter_rate,
            round(
                60.0
                * sum(points) filter (where acb_recency_rank <= 5)
                / nullif(
                    sum(minutes_seconds) filter (where acb_recency_rank <= 5),
                    0
                ),
                4
            ) as pre_acb_l5_points_per_minute,
            round(
                60.0
                * sum(field_goals_attempted) filter (where acb_recency_rank <= 5)
                / nullif(
                    sum(minutes_seconds) filter (where acb_recency_rank <= 5),
                    0
                ),
                4
            ) as pre_acb_l5_fga_per_minute,
            count(*) filter (
                where acb_game_date >= target_tipoff_utc - interval '7 days'
            ) as pre_acb_games_last_7d,
            round(
                sum(minutes_seconds::numeric / 60.0) filter (
                    where acb_game_date >= target_tipoff_utc - interval '7 days'
                ),
                3
            ) as pre_acb_minutes_last_7d,
            round(
                extract(
                    epoch from (target_tipoff_utc - max(acb_game_date))
                ) / 86400.0,
                3
            ) as pre_days_since_last_acb_game
        from acb_history_ranked
        group by
            target_season_code,
            target_gamecode,
            target_player_id,
            target_tipoff_utc
    ),
    historical_context_events as (
        select
            pf.season_code,
            pf.gamecode,
            pf.player_id as target_player_id,
            pf.game_tipoff_utc,
            e.player_id as context_player_id,
            e.event_type,
            e.role_direction,
            e.severity,
            e.source_confidence,
            e.role_impact_score,
            e.published_at,
            recent.avg_minutes_l5,
            recent.avg_fga_l5
        from player_features pf
        join pregame_context_event e
          on e.season_code = pf.season_code
         and e.gamecode = pf.gamecode
         and e.team_code = pf.team_code
         and e.published_at < pf.game_tipoff_utc
         and e.published_at >= pf.game_tipoff_utc - interval '72 hours'
        left join lateral (
            select
                round(avg(previous.minutes), 3) as avg_minutes_l5,
                round(avg(previous.fga), 3) as avg_fga_l5
            from (
                select
                    hist.seconds_played::numeric / 60.0 as minutes,
                    hist.field_goals_attempted::numeric as fga
                from player_base hist
                where hist.player_id = e.player_id
                  and hist.team_code = pf.team_code
                  and hist.game_tipoff_utc < pf.game_tipoff_utc
                order by hist.game_tipoff_utc desc, hist.gamecode desc
                limit 5
            ) previous
        ) recent
          on e.player_id is not null
         and e.player_id <> pf.player_id
    ),
    historical_context_features as (
        select
            season_code,
            gamecode,
            target_player_id as player_id,
            max(published_at) as context_feature_cutoff_time,
            count(*) as pre_context_event_count,
            count(*) filter (
                where context_player_id = target_player_id
            ) as pre_self_context_event_count,
            count(*) filter (
                where context_player_id is not null
                  and context_player_id <> target_player_id
            ) as pre_teammate_context_event_count,
            count(*) filter (
                where context_player_id is null
            ) as pre_team_context_event_count,
            count(*) filter (
                where source_confidence >= 0.90
            ) as pre_context_official_event_count,
            count(*) filter (
                where source_confidence >= 0.65
                  and source_confidence < 0.90
            ) as pre_context_reported_event_count,
            count(*) filter (
                where source_confidence < 0.65
            ) as pre_context_weak_event_count,
            coalesce(max(source_confidence), 0)
                as pre_context_max_source_confidence,
            coalesce(max(severity), 0)
                as pre_context_max_severity,
            coalesce(max(severity * source_confidence) filter (
                where context_player_id = target_player_id
                  and event_type = 'availability_out'
            ), 0) as pre_self_out_score,
            coalesce(max(severity * source_confidence) filter (
                where context_player_id = target_player_id
                  and event_type = 'availability_doubt'
            ), 0) as pre_self_doubt_score,
            coalesce(max(severity * source_confidence) filter (
                where context_player_id = target_player_id
                  and event_type = 'return'
            ), 0) as pre_self_return_score,
            round(coalesce(sum(greatest(role_impact_score, 0)) filter (
                where context_player_id = target_player_id
            ), 0), 5) as pre_self_role_up_score,
            round(coalesce(sum(greatest(-role_impact_score, 0)) filter (
                where context_player_id = target_player_id
            ), 0), 5) as pre_self_role_down_score,
            round(coalesce(sum(severity * source_confidence) filter (
                where context_player_id is not null
                  and context_player_id <> target_player_id
                  and event_type = 'availability_out'
            ), 0), 5) as pre_teammate_out_score_sum,
            round(coalesce(sum(severity * source_confidence) filter (
                where context_player_id is not null
                  and context_player_id <> target_player_id
                  and event_type = 'availability_doubt'
            ), 0), 5) as pre_teammate_doubt_score_sum,
            round(coalesce(sum(
                coalesce(avg_minutes_l5, 0) * severity * source_confidence
            ) filter (
                where context_player_id is not null
                  and context_player_id <> target_player_id
                  and event_type = 'availability_out'
            ), 0), 3) as pre_teammate_out_vacated_minutes_l5,
            round(coalesce(sum(
                coalesce(avg_fga_l5, 0) * severity * source_confidence
            ) filter (
                where context_player_id is not null
                  and context_player_id <> target_player_id
                  and event_type = 'availability_out'
            ), 0), 3) as pre_teammate_out_vacated_fga_l5,
            round(coalesce(sum(
                coalesce(avg_minutes_l5, 0) * severity * source_confidence
            ) filter (
                where context_player_id is not null
                  and context_player_id <> target_player_id
                  and event_type = 'availability_doubt'
            ), 0), 3) as pre_teammate_doubt_vacated_minutes_l5,
            round(coalesce(sum(
                coalesce(avg_fga_l5, 0) * severity * source_confidence
            ) filter (
                where context_player_id is not null
                  and context_player_id <> target_player_id
                  and event_type = 'availability_doubt'
            ), 0), 3) as pre_teammate_doubt_vacated_fga_l5
        from historical_context_events
        group by season_code, gamecode, target_player_id
    ),
    historical_context_collection as (
        select
            pf.season_code,
            pf.gamecode,
            pf.player_id,
            max(c.collected_at) as collection_feature_cutoff_time,
            count(c.collection_id) as pre_context_collection_runs,
            coalesce(sum(c.query_count), 0) as pre_context_query_count,
            coalesce(sum(c.successful_query_count), 0)
                as pre_context_successful_query_count,
            coalesce(sum(c.failed_query_count), 0)
                as pre_context_failed_query_count,
            coalesce(sum(c.players_queried), 0)
                as pre_context_players_queried,
            coalesce(sum(c.items_seen), 0)
                as pre_context_items_seen,
            coalesce(sum(c.inserted_event_count), 0)
                as pre_context_inserted_event_count,
            case when count(c.collection_id) > 0 then 1 else 0 end
                as pre_context_data_available,
            round(
                coalesce(sum(c.successful_query_count), 0)::numeric
                / nullif(coalesce(sum(c.query_count), 0), 0),
                4
            ) as pre_context_query_success_rate
        from player_features pf
        left join pregame_context_collection c
          on c.season_code = pf.season_code
         and c.gamecode = pf.gamecode
         and c.team_code = pf.team_code
         and c.collected_at < pf.game_tipoff_utc
         and c.collected_at >= pf.game_tipoff_utc - interval '72 hours'
        group by pf.season_code, pf.gamecode, pf.player_id
    ),
    team_features as (
        select
            tg.season_code,
            tg.gamecode,
            tg.team_code,
            lag(tg.utc_date) over w5 as team_feature_cutoff_time,
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
            partition by tg.team_code
            order by tg.utc_date, tg.gamecode
            rows between 5 preceding and 1 preceding
        )
    )
    select
        pf.season_code,
        pf.gamecode,
        pf.game_tipoff_utc,
        greatest(
            pf.player_feature_cutoff_time,
            tt.team_feature_cutoff_time,
            ot.team_feature_cutoff_time,
            ort.rotation_feature_cutoff_time,
            opt.position_feature_cutoff_time,
            af.acb_feature_cutoff_time,
            hcf.context_feature_cutoff_time,
            hcc.collection_feature_cutoff_time
        ) as feature_cutoff_time,
        pf.game_date,
        pf.player_id,
        pf.player_name,
        pf.team_code,
        pf.opponent_team_code,
        pf.is_home,

        pf.pre_history_games,
        pf.pre_current_season_games,
        pf.pre_player_height_cm,
        pf.pre_player_weight_kg,
        pf.pre_player_is_guard,
        pf.pre_player_is_forward,
        pf.pre_player_is_center,
        pf.pre_matchup_profile_games,
        pf.pre_matchup_ppm_vs_defender_height_slope,
        pf.pre_matchup_ts_vs_defender_height_slope,
        pf.pre_ppm_vs_taller_defender_profile,
        pf.pre_ppm_vs_similar_defender_profile,
        pf.pre_ppm_vs_shorter_defender_profile,
        pf.pre_ts_vs_taller_defender_profile,
        pf.pre_ts_vs_similar_defender_profile,
        pf.pre_ts_vs_shorter_defender_profile,
        pf.pre_avg_same_position_matchup_share,
        pf.pre_matchup_ppm_vs_guard_share_slope,
        pf.pre_matchup_ppm_vs_center_share_slope,
        pf.pre_last_hand_state,
        pf.pre_last_ts_delta_vs_prior_l10,
        pf.pre_hot_streak_games,
        pf.pre_cold_streak_games,
        pf.pre_completed_hot_episodes,
        pf.pre_completed_cold_episodes,
        pf.pre_avg_hot_episode_games,
        pf.pre_avg_cold_episode_games,
        pf.pre_max_hot_episode_games,
        pf.pre_max_cold_episode_games,
        pf.pre_hot_break_role_drop_rate,
        pf.pre_hot_break_eff_reversion_rate,
        pf.pre_cold_break_role_expansion_rate,
        pf.pre_cold_break_eff_recovery_rate,
        pf.pre_l3_minutes,
        pf.pre_l5_minutes,
        pf.pre_l10_minutes,
        pf.pre_naive_points_mean,
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

        pf.pre_l5_minutes_std,
        pf.pre_l10_minutes_std,
        pf.pre_l10_minutes_range,
        pf.pre_l10_minutes_cv,
        pf.pre_l5_fga_std,
        pf.pre_l10_fga_std,
        pf.pre_l10_fga_range,
        pf.pre_l10_fga_cv,
        pf.pre_l10_points_std,
        pf.pre_l10_points_range,
        pf.pre_l10_minute_spike_rate,
        pf.pre_l10_minute_drop_rate,
        pf.pre_l10_fga_spike_rate,
        pf.pre_l10_fga_drop_rate,
        pf.pre_l10_starter_change_rate,
        pf.pre_minute_drop_foul_reason_share,
        pf.pre_minute_drop_blowout_reason_share,
        pf.pre_minute_drop_demotion_reason_share,
        pf.pre_minute_drop_unexplained_share,
        pf.pre_minute_spike_overtime_reason_share,
        pf.pre_minute_spike_close_reason_share,
        pf.pre_minute_spike_promotion_reason_share,
        pf.pre_minute_spike_unexplained_share,
        pf.pre_fga_spike_situational_reason_share,
        pf.pre_fga_spike_role_expansion_share,
        pf.pre_fga_spike_unexplained_share,
        pf.pre_last_minute_spike,
        pf.pre_last_minute_drop,
        pf.pre_last_fga_spike,
        pf.pre_last_fga_drop,
        pf.pre_last_starter_change,
        pf.pre_last_minute_drop_foul_context,
        pf.pre_last_minute_drop_blowout_context,
        pf.pre_last_minute_spike_overtime_context,
        pf.pre_last_minute_spike_close_context,
        pf.pre_last_fga_spike_situational_context,
        pf.pre_last_fga_spike_role_expansion_context,
        pf.pre_last_fga_spike_unexplained_context,

        pf.pre_last_minutes,
        pf.pre_last_fga,
        pf.pre_last_3pa,
        pf.pre_last_context_neutral_fga,
        pf.pre_last_situational_fga,
        pf.pre_last_late_trailing_fga,
        pf.pre_last_late_clock_proxy_fga,
        pf.pre_last_situational_fga_share,
        pf.pre_l5_context_neutral_fga,
        pf.pre_l10_context_neutral_fga,
        pf.pre_l5_situational_fga_share,
        pf.pre_l10_situational_fga_share,
        pf.pre_l5_late_trailing_fga_share,
        pf.pre_l5_late_clock_proxy_fga_share,
        pf.pre_l10_3pa_share,

        pf.pre_l5_starter_rate,
        pf.pre_l10_starter_rate,
        pf.pre_last_was_starter,
        pf.pre_days_rest,
        pf.pre_hours_rest,
        pf.pre_travel_air_km,
        pf.pre_last_was_away,
        pf.pre_l5_away_rate,
        pf.pre_away_games_last_3,
        pf.pre_away_games_last_7d,
        pf.pre_away_games_last_14d,
        pf.pre_l5_home_away_switch_rate,
        pf.pre_games_last_7d,
        pf.pre_games_last_14d,
        pf.pre_minutes_last_7d,
        pf.pre_minutes_last_14d,
        pf.pre_l3_ts_proxy,
        pf.pre_l5_ts_proxy,
        pf.pre_l10_ts_proxy,
        pf.pre_l10_points_per_minute,
        pf.pre_l10_fga_per_minute,
        pf.pre_l10_2p_pct,
        pf.pre_l10_3p_pct,
        pf.pre_l10_ft_pct,

        pf.pre_l5_pbp_offensive_possessions,
        pf.pre_l10_pbp_offensive_possessions,
        pf.pre_l5_pbp_defensive_possessions,
        pf.pre_l10_pbp_defensive_possessions,
        pf.pre_l10_pbp_on_off_rating,
        pf.pre_l10_pbp_on_def_rating,
        pf.pre_l5_pbp_stint_count,
        pf.pre_l5_pbp_distinct_lineups,
        pf.pre_l5_pbp_avg_stint_seconds,
        pf.pre_l5_pbp_max_stint_seconds,
        pf.pre_l5_pbp_primary_lineup_share,

        pf.pre_role2_last_fga_per_100_possessions,
        pf.pre_role2_l3_fga_per_100_possessions,
        pf.pre_role2_l5_fga_per_100_possessions,
        pf.pre_role2_l10_fga_per_100_possessions,
        pf.pre_role2_fga_per_100_trend_l3_vs_l10,
        pf.pre_role2_last_scoring_opportunities_per_100,
        pf.pre_role2_l5_scoring_opportunities_per_100,
        pf.pre_role2_l10_scoring_opportunities_per_100,
        pf.pre_role2_l3_team_fga_share,
        pf.pre_role2_l5_team_fga_share,
        pf.pre_role2_l10_team_fga_share,
        pf.pre_role2_team_fga_share_trend_l3_vs_l10,
        pf.pre_role2_l5_team_scoring_opportunity_share,
        pf.pre_role2_last_option_rank,
        pf.pre_role2_l5_option_rank,
        pf.pre_role2_l5_primary_option_rate,
        pf.pre_role2_l5_top2_option_rate,
        pf.pre_role2_l5_teammate_contexts,
        pf.pre_role2_l5_mean_teammate_off_fga_uplift,
        pf.pre_role2_l5_max_teammate_off_fga_uplift,
        pf.pre_role2_l10_max_teammate_off_fga_uplift,

        pf.pre_last_top_pair_shared_minutes,
        pf.pre_last_top_pair_net_rating,
        pf.pre_l5_top_pair_shared_minutes,
        pf.pre_l5_top_pair_net_rating,
        pf.pre_l5_top_pair_repeat_rate,
        pf.pre_last_top_triple_shared_minutes,
        pf.pre_last_top_triple_net_rating,
        pf.pre_l5_top_triple_shared_minutes,
        pf.pre_l5_top_triple_net_rating,
        pf.pre_l5_top_triple_repeat_rate,
        pf.pre_last_top_lineup_shared_minutes,
        pf.pre_last_top_lineup_net_rating,
        pf.pre_l5_top_lineup_shared_minutes,
        pf.pre_l5_top_lineup_net_rating,
        pf.pre_l5_top_lineup_repeat_rate,

        coalesce(hcf.pre_context_event_count, 0)
            as pre_context_event_count,
        coalesce(hcf.pre_self_context_event_count, 0)
            as pre_self_context_event_count,
        coalesce(hcf.pre_teammate_context_event_count, 0)
            as pre_teammate_context_event_count,
        coalesce(hcf.pre_team_context_event_count, 0)
            as pre_team_context_event_count,
        coalesce(hcf.pre_context_official_event_count, 0)
            as pre_context_official_event_count,
        coalesce(hcf.pre_context_reported_event_count, 0)
            as pre_context_reported_event_count,
        coalesce(hcf.pre_context_weak_event_count, 0)
            as pre_context_weak_event_count,
        coalesce(hcf.pre_context_max_source_confidence, 0)
            as pre_context_max_source_confidence,
        coalesce(hcf.pre_context_max_severity, 0)
            as pre_context_max_severity,
        coalesce(hcf.pre_self_out_score, 0)
            as pre_self_out_score,
        coalesce(hcf.pre_self_doubt_score, 0)
            as pre_self_doubt_score,
        coalesce(hcf.pre_self_return_score, 0)
            as pre_self_return_score,
        coalesce(hcf.pre_self_role_up_score, 0)
            as pre_self_role_up_score,
        coalesce(hcf.pre_self_role_down_score, 0)
            as pre_self_role_down_score,
        coalesce(hcf.pre_teammate_out_score_sum, 0)
            as pre_teammate_out_score_sum,
        coalesce(hcf.pre_teammate_doubt_score_sum, 0)
            as pre_teammate_doubt_score_sum,
        coalesce(hcf.pre_teammate_out_vacated_minutes_l5, 0)
            as pre_teammate_out_vacated_minutes_l5,
        coalesce(hcf.pre_teammate_out_vacated_fga_l5, 0)
            as pre_teammate_out_vacated_fga_l5,
        coalesce(hcf.pre_teammate_doubt_vacated_minutes_l5, 0)
            as pre_teammate_doubt_vacated_minutes_l5,
        coalesce(hcf.pre_teammate_doubt_vacated_fga_l5, 0)
            as pre_teammate_doubt_vacated_fga_l5,
        coalesce(hcc.pre_context_collection_runs, 0)
            as pre_context_collection_runs,
        coalesce(hcc.pre_context_query_count, 0)
            as pre_context_query_count,
        coalesce(hcc.pre_context_successful_query_count, 0)
            as pre_context_successful_query_count,
        coalesce(hcc.pre_context_failed_query_count, 0)
            as pre_context_failed_query_count,
        coalesce(hcc.pre_context_players_queried, 0)
            as pre_context_players_queried,
        coalesce(hcc.pre_context_items_seen, 0)
            as pre_context_items_seen,
        coalesce(hcc.pre_context_inserted_event_count, 0)
            as pre_context_inserted_event_count,
        coalesce(hcc.pre_context_data_available, 0)
            as pre_context_data_available,
        hcc.pre_context_query_success_rate,

        af.pre_acb_l5_games,
        af.pre_acb_l3_minutes,
        af.pre_acb_l5_minutes,
        af.pre_acb_l3_fga,
        af.pre_acb_l5_fga,
        af.pre_acb_l3_points,
        af.pre_acb_l5_points,
        af.pre_acb_l5_starter_rate,
        af.pre_acb_l5_points_per_minute,
        af.pre_acb_l5_fga_per_minute,
        af.pre_acb_games_last_7d,
        af.pre_acb_minutes_last_7d,
        af.pre_days_since_last_acb_game,
        round(
            af.pre_acb_l5_minutes - pf.pre_l5_minutes,
            3
        ) as pre_acb_vs_el_l5_minutes_gap,
        round(
            af.pre_acb_l5_fga - pf.pre_l5_fga,
            3
        ) as pre_acb_vs_el_l5_fga_gap,
        round(
            af.pre_acb_l5_points - pf.pre_l5_points,
            3
        ) as pre_acb_vs_el_l5_points_gap,
        round(
            af.pre_acb_l5_points_per_minute
                - pf.pre_l10_points_per_minute,
            4
        ) as pre_acb_vs_el_points_per_minute_gap,
        round(
            af.pre_acb_l5_fga_per_minute
                - pf.pre_l10_fga_per_minute,
            4
        ) as pre_acb_vs_el_fga_per_minute_gap,
        coalesce(pf.pre_games_last_7d, 0)
            + coalesce(af.pre_acb_games_last_7d, 0)
            as pre_combined_games_last_7d,
        round(
            coalesce(pf.pre_minutes_last_7d, 0)
                + coalesce(af.pre_acb_minutes_last_7d, 0),
            3
        ) as pre_combined_minutes_last_7d,

        tt.pre_l5_off_rating as pre_team_l5_off_rating,
        tt.pre_l5_def_rating as pre_team_l5_def_rating,
        tt.pre_l5_possessions as pre_team_l5_possessions,
        ort.pre_l5_rotation_avg_height_cm
            as pre_opponent_rotation_avg_height_cm,
        ort.pre_l5_rotation_avg_weight_kg
            as pre_opponent_rotation_avg_weight_kg,
        ort.pre_l5_rotation_guard_share
            as pre_opponent_rotation_guard_share,
        ort.pre_l5_rotation_forward_share
            as pre_opponent_rotation_forward_share,
        ort.pre_l5_rotation_center_share
            as pre_opponent_rotation_center_share,
        opt.pre_l5_position_avg_height_cm
            as pre_opponent_same_position_avg_height_cm,
        opt.pre_l5_position_avg_weight_kg
            as pre_opponent_same_position_avg_weight_kg,
        opt.pre_l5_position_rotation_share
            as pre_opponent_same_position_rotation_share,
        round(
            opt.pre_l5_position_avg_height_cm - pf.pre_player_height_cm,
            3
        ) as pre_matchup_height_diff_cm,
        round(
            opt.pre_l5_position_avg_weight_kg - pf.pre_player_weight_kg,
            3
        ) as pre_matchup_weight_diff_kg,
        ot.pre_l5_off_rating as pre_opponent_l5_off_rating,
        ot.pre_l5_def_rating as pre_opponent_l5_def_rating,
        ot.pre_l5_possessions as pre_opponent_l5_possessions,

        round(pf.pre_l3_ts_proxy - pf.pre_l10_ts_proxy, 4)
            as pre_ts_trend_l3_vs_l10,
        round(pf.pre_l3_minutes - pf.pre_l10_minutes, 3)
            as pre_minutes_trend_l3_vs_l10,
        round(pf.pre_l3_fga - pf.pre_l10_fga, 3)
            as pre_fga_trend_l3_vs_l10,
        round(pf.pre_l3_points - pf.pre_l10_points, 3)
            as pre_points_trend_l3_vs_l10,
        round(pf.pre_last_minutes - pf.pre_l10_minutes, 3)
            as pre_last_minutes_delta_vs_l10,
        round(pf.pre_last_fga - pf.pre_l10_fga, 3)
            as pre_last_fga_delta_vs_l10,
        round(
            pf.pre_last_context_neutral_fga - pf.pre_l10_context_neutral_fga,
            3
        ) as pre_last_context_neutral_fga_delta_vs_l10,
        round(
            pf.pre_last_3pa::numeric / nullif(pf.pre_last_fga, 0)
                - pf.pre_l10_3pa_share,
            4
        ) as pre_last_3pa_share_delta_vs_l10,

        pf.points as target_points,
        round(pf.seconds_played::numeric / 60.0, 3) as target_minutes,
        pf.field_goals_attempted as target_fga,
        pf.three_pointers_attempted as target_3pa,
        pf.free_throws_attempted as target_fta,
        pf.is_starter as target_was_starter
    from player_features pf
    left join acb_features af
      on af.season_code = pf.season_code
     and af.gamecode = pf.gamecode
     and af.player_id = pf.player_id
    left join historical_context_features hcf
      on hcf.season_code = pf.season_code
     and hcf.gamecode = pf.gamecode
     and hcf.player_id = pf.player_id
    left join historical_context_collection hcc
      on hcc.season_code = pf.season_code
     and hcc.gamecode = pf.gamecode
     and hcc.player_id = pf.player_id
    left join team_features tt
      on tt.season_code = pf.season_code
     and tt.gamecode = pf.gamecode
     and tt.team_code = pf.team_code
    left join team_features ot
      on ot.season_code = pf.season_code
     and ot.gamecode = pf.gamecode
     and ot.team_code = pf.opponent_team_code
    left join team_rotation_features ort
      on ort.season_code = pf.season_code
     and ort.gamecode = pf.gamecode
     and ort.team_code = pf.opponent_team_code
    left join team_position_features opt
      on opt.season_code = pf.season_code
     and opt.gamecode = pf.gamecode
     and opt.team_code = pf.opponent_team_code
     and opt.position_name = pf.player_position_name
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
