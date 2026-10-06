-- Lightweight full-season schedule mirror for forward-looking model context.
--
-- raw_game intentionally contains only played games because it hangs off
-- boxscore/PBP ingestion.  Pregame collection needs upcoming fixtures too, so
-- this table mirrors only schedule facts and never implies that a game has
-- source boxscore or derived rows.

create table season_schedule_game (
    season_code text not null,
    gamecode integer not null check (gamecode > 0),
    competition_code text,
    phase_code text,
    phase_name text,
    round_number integer,
    round_name text,
    played boolean not null default false,
    game_status text,
    utc_date timestamptz,
    home_team_code text not null check (home_team_code = btrim(home_team_code) and home_team_code <> ''),
    home_team_name text,
    away_team_code text not null check (away_team_code = btrim(away_team_code) and away_team_code <> ''),
    away_team_name text,
    venue_name text,
    schedule_refreshed_at timestamptz not null default now(),
    primary key (season_code, gamecode),
    check (home_team_code <> away_team_code)
);

create index season_schedule_game_upcoming_idx
    on season_schedule_game(utc_date, season_code, gamecode)
    where not played;

alter table season_schedule_game enable row level security;

revoke all on season_schedule_game from anon, authenticated;
grant select on season_schedule_game to el_reader, el_tester;

comment on table season_schedule_game is
    'Full schedule mirror including unplayed games, used only for forward-looking pregame context.';
