"""Higher-order teammate combinations for pre-game player-points context.

Triples and exact five-man units are intentionally held to stricter sample
requirements than player pairs. The signals are descriptive, leakage-safe and
based on actual shared floor time before the target tipoff.
"""

from __future__ import annotations

TEAMMATE_TRIPLE_SQL = """
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
teammate_pairs as (
    select
        a.teammate_id as teammate_a_id,
        a.teammate_name as teammate_a_name,
        b.teammate_id as teammate_b_id,
        b.teammate_name as teammate_b_name
    from teammates a
    join teammates b
      on a.teammate_id < b.teammate_id
),
triple_game_stats as (
    select
        tp.teammate_a_id,
        tp.teammate_a_name,
        tp.teammate_b_id,
        tp.teammate_b_name,
        count(*) filter (
            where a.player_id is not null and b.player_id is not null
        ) as games_together,
        count(*) filter (
            where a.player_id is null or b.player_id is null
        ) as games_without_combo,
        round(
            60.0 * sum(pg.points) filter (
                where a.player_id is not null and b.player_id is not null
            )
            / nullif(
                sum(pg.seconds_official) filter (
                    where a.player_id is not null and b.player_id is not null
                ),
                0
            ),
            4
        ) as target_points_per_minute_with,
        round(
            60.0 * sum(pg.points) filter (
                where a.player_id is null or b.player_id is null
            )
            / nullif(
                sum(pg.seconds_official) filter (
                    where a.player_id is null or b.player_id is null
                ),
                0
            ),
            4
        ) as target_points_per_minute_without,
        round(
            60.0 * sum(pg.field_goals_attempted) filter (
                where a.player_id is not null and b.player_id is not null
            )
            / nullif(
                sum(pg.seconds_official) filter (
                    where a.player_id is not null and b.player_id is not null
                ),
                0
            ),
            4
        ) as target_fga_per_minute_with,
        round(
            60.0 * sum(pg.field_goals_attempted) filter (
                where a.player_id is null or b.player_id is null
            )
            / nullif(
                sum(pg.seconds_official) filter (
                    where a.player_id is null or b.player_id is null
                ),
                0
            ),
            4
        ) as target_fga_per_minute_without
    from teammate_pairs tp
    cross join player_games pg
    left join v_player_game a
      on a.season_code = pg.season_code
     and a.gamecode = pg.gamecode
     and a.team_code = pg.team_code
     and a.player_id = tp.teammate_a_id
     and a.seconds_official > 0
     and not a.excluded_by_default
    left join v_player_game b
      on b.season_code = pg.season_code
     and b.gamecode = pg.gamecode
     and b.team_code = pg.team_code
     and b.player_id = tp.teammate_b_id
     and b.seconds_official > 0
     and not b.excluded_by_default
    group by
        tp.teammate_a_id,
        tp.teammate_a_name,
        tp.teammate_b_id,
        tp.teammate_b_name
),
triple_stints as (
    select
        tp.teammate_a_id,
        tp.teammate_b_id,
        ls.gamecode,
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
    from teammate_pairs tp
    join v_lineup_player target_lineup
      on target_lineup.player_id = %s
    join v_lineup_player teammate_a_lineup
      on teammate_a_lineup.lineup_id = target_lineup.lineup_id
     and teammate_a_lineup.player_id = tp.teammate_a_id
    join v_lineup_player teammate_b_lineup
      on teammate_b_lineup.lineup_id = target_lineup.lineup_id
     and teammate_b_lineup.player_id = tp.teammate_b_id
    join lineup_stint ls
      on ls.home_lineup_id = target_lineup.lineup_id
      or ls.away_lineup_id = target_lineup.lineup_id
    join player_games pg
      on pg.season_code = ls.season_code
     and pg.gamecode = ls.gamecode
     and pg.team_code = target_lineup.team_code
),
triple_on_court as (
    select
        teammate_a_id,
        teammate_b_id,
        count(distinct gamecode) as shared_games,
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
    from triple_stints
    group by teammate_a_id, teammate_b_id
),
joined as (
    select
        tgs.*,
        toc.shared_games,
        toc.shared_stints,
        toc.shared_minutes,
        toc.shared_team_possessions,
        toc.shared_opponent_possessions,
        toc.shared_off_rating,
        toc.shared_def_rating,
        round(toc.shared_off_rating - toc.shared_def_rating, 2)
            as shared_net_rating,
        round(
            tgs.target_points_per_minute_with
                - tgs.target_points_per_minute_without,
            4
        ) as target_points_per_minute_delta,
        round(
            tgs.target_fga_per_minute_with
                - tgs.target_fga_per_minute_without,
            4
        ) as target_fga_per_minute_delta
    from triple_game_stats tgs
    left join triple_on_court toc
      using (teammate_a_id, teammate_b_id)
)
select
    *,
    case
        when games_together < 5
          or coalesce(shared_minutes, 0) < 75
          or games_without_combo < 3
            then 'small_sample'
        when target_points_per_minute_delta >= 0.04
         and coalesce(target_fga_per_minute_delta, 0) >= 0
         and coalesce(shared_net_rating, 0) > 0
            then 'positive'
        when target_points_per_minute_delta <= -0.04
         and coalesce(target_fga_per_minute_delta, 0) <= 0
         and coalesce(shared_net_rating, 0) < 0
            then 'negative'
        else 'mixed'
    end as scoring_association
from joined
order by
    case when games_together >= 5
           and coalesce(shared_minutes, 0) >= 75
           and games_without_combo >= 3
         then 0 else 1 end,
    coalesce(shared_minutes, 0) desc,
    games_together desc,
    teammate_a_id,
    teammate_b_id
limit %s
"""


KEY_LINEUP_SQL = """
with cutoff as (
    select coalesce(%s::timestamptz, now()) as at
),
player_games_ranked as (
    select
        p.season_code,
        p.gamecode,
        p.utc_date,
        p.team_code,
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
target_lineups as (
    select distinct
        lp.lineup_id,
        lp.team_code
    from v_lineup_player lp
    where lp.player_id = %s
),
lineup_stints as (
    select
        tl.lineup_id,
        tl.team_code,
        ls.gamecode,
        greatest(ls.duration_seconds_raw, 0)::numeric as shared_seconds,
        case
            when ls.home_lineup_id = tl.lineup_id
                then ls.possessions_home
            else ls.possessions_away
        end::numeric as team_possessions,
        case
            when ls.home_lineup_id = tl.lineup_id
                then ls.possessions_away
            else ls.possessions_home
        end::numeric as opponent_possessions,
        case
            when ls.home_lineup_id = tl.lineup_id
                then ls.home_points
            else ls.away_points
        end::numeric as team_points,
        case
            when ls.home_lineup_id = tl.lineup_id
                then ls.away_points
            else ls.home_points
        end::numeric as opponent_points
    from target_lineups tl
    join lineup_stint ls
      on ls.home_lineup_id = tl.lineup_id
      or ls.away_lineup_id = tl.lineup_id
    join player_games pg
      on pg.season_code = ls.season_code
     and pg.gamecode = ls.gamecode
     and pg.team_code = tl.team_code
),
lineup_summary as (
    select
        lineup_id,
        team_code,
        count(distinct gamecode) as games,
        count(*) as stints,
        round(sum(shared_seconds) / 60.0, 2) as shared_minutes,
        sum(team_possessions) as team_possessions,
        sum(opponent_possessions) as opponent_possessions,
        round(
            100.0 * sum(team_points) / nullif(sum(team_possessions), 0),
            2
        ) as off_rating,
        round(
            100.0 * sum(opponent_points) / nullif(sum(opponent_possessions), 0),
            2
        ) as def_rating
    from lineup_stints
    group by lineup_id, team_code
),
lineup_players as (
    select
        ls.lineup_id,
        jsonb_agg(
            jsonb_build_object(
                'player_id', lp.player_id,
                'player_name', p.display_name
            )
            order by lp.player_id
        ) as players
    from lineup_summary ls
    join v_lineup_player lp using (lineup_id)
    left join player p using (player_id)
    group by ls.lineup_id
)
select
    ls.lineup_id,
    ls.team_code,
    lp.players,
    ls.games,
    ls.stints,
    ls.shared_minutes,
    ls.team_possessions,
    ls.opponent_possessions,
    ls.off_rating,
    ls.def_rating,
    round(ls.off_rating - ls.def_rating, 2) as net_rating,
    case
        when ls.games < 4
          or ls.shared_minutes < 60
          or ls.team_possessions < 50
            then 'small_sample'
        when ls.off_rating - ls.def_rating >= 5
            then 'positive'
        when ls.off_rating - ls.def_rating <= -5
            then 'negative'
        else 'mixed'
    end as lineup_association
from lineup_summary ls
join lineup_players lp using (lineup_id)
order by
    case when ls.games >= 4
           and ls.shared_minutes >= 60
           and ls.team_possessions >= 50
         then 0 else 1 end,
    ls.shared_minutes desc,
    ls.games desc,
    ls.lineup_id
limit %s
"""
