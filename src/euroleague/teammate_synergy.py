"""Leakage-safe teammate pair associations for player-points modelling.

The output is descriptive rather than causal. It combines game-level with/without
role changes with actual shared on-court stint evidence, so a model can tell the
difference between "both appeared in the same game" and "they actually shared
meaningful floor time".
"""

from __future__ import annotations

TEAMMATE_SYNERGY_SQL = """
with cutoff as (
    select coalesce(%s::timestamptz, now()) as at
),
player_games_ranked as (
    select
        p.season_code,
        p.gamecode,
        p.utc_date,
        p.team_code,
        p.seconds_official,
        p.points,
        p.field_goals_attempted,
        row_number() over (
            order by p.utc_date desc, p.gamecode desc
        ) as rn
    from v_player_game p
    cross join cutoff c
    where p.season_code = %s
      and p.player_id = %s
      and p.seconds_official > 0
      and not p.excluded_by_default
      and p.utc_date < c.at
      and (%s::text is null or p.team_code = %s)
),
player_games as (
    select *
    from player_games_ranked
    where rn <= %s
),
teammates as (
    select
        t.player_id as teammate_id,
        max(t.player_name) as teammate_name
    from player_games pg
    join v_player_game t
      on t.season_code = pg.season_code
     and t.gamecode = pg.gamecode
     and t.team_code = pg.team_code
     and t.player_id <> %s
     and t.seconds_official > 0
     and not t.excluded_by_default
    group by t.player_id
),
pair_game_stats as (
    select
        tm.teammate_id,
        tm.teammate_name,
        count(*) filter (where t.player_id is not null) as games_together,
        count(*) filter (where t.player_id is null) as games_without,
        round(
            avg(pg.seconds_official::numeric / 60.0)
                filter (where t.player_id is not null),
            2
        ) as target_avg_minutes_with,
        round(
            avg(pg.seconds_official::numeric / 60.0)
                filter (where t.player_id is null),
            2
        ) as target_avg_minutes_without,
        round(
            60.0 * sum(pg.points) filter (where t.player_id is not null)
            / nullif(
                sum(pg.seconds_official) filter (where t.player_id is not null),
                0
            ),
            4
        ) as target_points_per_minute_with,
        round(
            60.0 * sum(pg.points) filter (where t.player_id is null)
            / nullif(
                sum(pg.seconds_official) filter (where t.player_id is null),
                0
            ),
            4
        ) as target_points_per_minute_without,
        round(
            60.0 * sum(pg.field_goals_attempted)
                filter (where t.player_id is not null)
            / nullif(
                sum(pg.seconds_official) filter (where t.player_id is not null),
                0
            ),
            4
        ) as target_fga_per_minute_with,
        round(
            60.0 * sum(pg.field_goals_attempted)
                filter (where t.player_id is null)
            / nullif(
                sum(pg.seconds_official) filter (where t.player_id is null),
                0
            ),
            4
        ) as target_fga_per_minute_without
    from teammates tm
    cross join player_games pg
    left join v_player_game t
      on t.season_code = pg.season_code
     and t.gamecode = pg.gamecode
     and t.team_code = pg.team_code
     and t.player_id = tm.teammate_id
     and t.seconds_official > 0
     and not t.excluded_by_default
    group by tm.teammate_id, tm.teammate_name
),
pair_stints as (
    select
        tm.teammate_id,
        greatest(ls.duration_seconds_raw, 0)::numeric as shared_seconds,
        case
            when ls.home_lineup_id = target_lineup.lineup_id
                then ls.possessions_home
            else ls.possessions_away
        end::numeric as team_possessions,
        case
            when ls.home_lineup_id = target_lineup.lineup_id
                then ls.possessions_away
            else ls.possessions_home
        end::numeric as opponent_possessions,
        case
            when ls.home_lineup_id = target_lineup.lineup_id
                then ls.home_points
            else ls.away_points
        end::numeric as team_points,
        case
            when ls.home_lineup_id = target_lineup.lineup_id
                then ls.away_points
            else ls.home_points
        end::numeric as opponent_points
    from teammates tm
    join v_lineup_player target_lineup
      on target_lineup.player_id = %s
    join v_lineup_player teammate_lineup
      on teammate_lineup.lineup_id = target_lineup.lineup_id
     and teammate_lineup.player_id = tm.teammate_id
    join lineup_stint ls
      on ls.home_lineup_id = target_lineup.lineup_id
      or ls.away_lineup_id = target_lineup.lineup_id
    join player_games pg
      on pg.season_code = ls.season_code
     and pg.gamecode = ls.gamecode
     and pg.team_code = target_lineup.team_code
),
pair_on_court as (
    select
        teammate_id,
        count(*) as shared_stints,
        round(sum(shared_seconds) / 60.0, 2) as shared_minutes,
        sum(team_possessions) as shared_team_possessions,
        sum(opponent_possessions) as shared_opponent_possessions,
        round(
            100.0 * sum(team_points) / nullif(sum(team_possessions), 0),
            2
        ) as shared_off_rating,
        round(
            100.0 * sum(opponent_points) / nullif(sum(opponent_possessions), 0),
            2
        ) as shared_def_rating
    from pair_stints
    group by teammate_id
),
joined as (
    select
        pgs.*,
        poc.shared_stints,
        poc.shared_minutes,
        poc.shared_team_possessions,
        poc.shared_opponent_possessions,
        poc.shared_off_rating,
        poc.shared_def_rating,
        round(
            poc.shared_off_rating - poc.shared_def_rating,
            2
        ) as shared_net_rating,
        round(
            pgs.target_points_per_minute_with
                - pgs.target_points_per_minute_without,
            4
        ) as target_points_per_minute_delta,
        round(
            pgs.target_fga_per_minute_with
                - pgs.target_fga_per_minute_without,
            4
        ) as target_fga_per_minute_delta,
        round(
            pgs.target_avg_minutes_with
                - pgs.target_avg_minutes_without,
            2
        ) as target_minutes_delta
    from pair_game_stats pgs
    left join pair_on_court poc using (teammate_id)
)
select
    *,
    case
        when games_together < 4
          or coalesce(shared_minutes, 0) < 40
          or games_without < 2
            then 'small_sample'
        when target_points_per_minute_delta >= 0.04
         and coalesce(target_fga_per_minute_delta, 0) >= 0
            then 'positive'
        when target_points_per_minute_delta <= -0.04
         and coalesce(target_fga_per_minute_delta, 0) <= 0
            then 'negative'
        else 'mixed'
    end as scoring_association
from joined
order by
    coalesce(shared_minutes, 0) desc,
    games_together desc,
    teammate_id
limit %s
"""
