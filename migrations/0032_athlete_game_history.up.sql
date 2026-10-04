-- Unified cross-competition player-game history for canonical athletes.
-- The view never matches players by name; it follows athlete_source_identity.

create view v_athlete_game_history
with (security_invoker = true)
as
select
    asi.athlete_id,
    'EL'::text as source,
    pg.season_code,
    substring(pg.season_code from 2)::integer as season_start_year,
    vg.competition_code,
    pg.gamecode::text as source_game_id,
    pg.utc_date,
    pg.team_code as team_source_id,
    pg.opponent_team_code as opponent_source_id,
    pg.player_id as source_player_id,
    pg.player_name as display_name,
    pg.is_starter,
    pg.seconds_official as minutes_seconds,
    pg.points,
    (pg.field_goals_made - pg.three_pointers_made) as two_made,
    (pg.field_goals_attempted - pg.three_pointers_attempted) as two_attempted,
    pg.three_pointers_made as three_made,
    pg.three_pointers_attempted as three_attempted,
    pg.free_throws_made as free_throw_made,
    pg.free_throws_attempted as free_throw_attempted,
    pg.field_goals_attempted,
    pg.assists,
    pg.steals,
    pg.turnovers,
    pg.blocks_favour as blocks,
    pg.fouls_commited as fouls_committed,
    pg.fouls_received,
    pg.plus_minus,
    pg.valuation,
    pg.excluded_by_default
from athlete_source_identity asi
join v_player_game pg
  on asi.source = 'EL'
 and asi.source_player_id = pg.player_id
join v_game vg
  on vg.season_code = pg.season_code
 and vg.gamecode = pg.gamecode

union all

select
    asi.athlete_id,
    'ACB'::text as source,
    ('A' || case
        when extract(month from g.start_at) >= 7 then extract(year from g.start_at)::integer
        else extract(year from g.start_at)::integer - 1
     end::text) as season_code,
    case
        when extract(month from g.start_at) >= 7 then extract(year from g.start_at)::integer
        else extract(year from g.start_at)::integer - 1
    end as season_start_year,
    coalesce(g.competition_name, 'ACB') as competition_code,
    pg.match_id as source_game_id,
    g.start_at as utc_date,
    pg.team_source_id,
    case
        when pg.team_source_id = g.home_team_source_id then g.away_team_source_id
        when pg.team_source_id = g.away_team_source_id then g.home_team_source_id
        else null
    end as opponent_source_id,
    pg.source_player_id,
    pg.display_name,
    pg.is_starter,
    pg.minutes_seconds,
    pg.points,
    pg.two_made,
    pg.two_attempted,
    pg.three_made,
    pg.three_attempted,
    pg.free_throw_made,
    pg.free_throw_attempted,
    (pg.two_attempted + pg.three_attempted) as field_goals_attempted,
    pg.assists,
    pg.steals,
    pg.turnovers,
    pg.blocks,
    pg.fouls_committed,
    pg.fouls_received,
    pg.plus_minus,
    pg.valuation,
    false as excluded_by_default
from athlete_source_identity asi
join acb_player_game pg
  on asi.source = 'ACB'
 and asi.source_player_id = pg.source_player_id
join acb_game g
  on g.match_id = pg.match_id;

revoke all on v_athlete_game_history from anon, authenticated;
grant select on v_athlete_game_history to el_reader, el_tester;

comment on view v_athlete_game_history is
    'Chronological EL+ACB player-game history linked only through canonical athlete identities.';
