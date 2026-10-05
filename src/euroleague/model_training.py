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
            p.field_goals_attempted,
            p.three_pointers_attempted,
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
        where p.seconds_official > 0
          and not p.excluded_by_default
    ),
    player_role_baseline as (
        select
            *,
            lag(is_starter) over role_all as role_prev_starter,
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
            end as starter_demotion_event
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
                as pre_l5_pbp_primary_lineup_share
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
            opt.position_feature_cutoff_time
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
        pf.pre_days_rest,
        pf.pre_games_last_7d,
        pf.pre_games_last_14d,
        pf.pre_minutes_last_7d,
        pf.pre_minutes_last_14d,
        pf.pre_l3_ts_proxy,
        pf.pre_l5_ts_proxy,
        pf.pre_l10_ts_proxy,
        pf.pre_l10_points_per_minute,
        pf.pre_l10_fga_per_minute,

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

        pf.points as target_points,
        round(pf.seconds_played::numeric / 60.0, 3) as target_minutes,
        pf.field_goals_attempted as target_fga,
        pf.three_pointers_attempted as target_3pa,
        pf.free_throws_attempted as target_fta,
        pf.is_starter as target_was_starter
    from player_features pf
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
