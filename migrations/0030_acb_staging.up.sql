-- ACB staging layer for source-native domestic-league ingestion.
-- No existing EuroLeague table is changed. These tables are private staging
-- surfaces and intentionally receive no anon/authenticated policies.

create table acb_raw_snapshot (
    match_id text not null check (match_id = btrim(match_id) and match_id <> ''),
    endpoint text not null check (endpoint in ('header', 'boxscore', 'play_by_play')),
    content_sha256 text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    fetched_at timestamptz not null default now(),
    payload jsonb not null,
    primary key (match_id, endpoint, content_sha256)
);

create table acb_game (
    match_id text primary key check (match_id = btrim(match_id) and match_id <> ''),
    competition_id integer,
    competition_name text,
    start_at timestamptz,
    home_team_source_id text,
    away_team_source_id text,
    home_team_name text,
    away_team_name text,
    home_score integer check (home_score is null or home_score >= 0),
    away_score integer check (away_score is null or away_score >= 0),
    match_finished boolean not null default false,
    fetched_at timestamptz not null default now(),
    raw_header jsonb
);

create table acb_player_game (
    match_id text not null references acb_game(match_id) on delete cascade,
    source_player_id text not null check (
        source_player_id = btrim(source_player_id) and source_player_id <> ''
    ),
    team_source_id text not null check (
        team_source_id = btrim(team_source_id) and team_source_id <> ''
    ),
    display_name text not null check (display_name = btrim(display_name) and display_name <> ''),
    jersey_number text,
    is_starter boolean not null default false,
    minutes_seconds integer not null check (minutes_seconds between 0 and 3600),
    points integer not null check (points >= 0),
    two_made integer not null check (two_made >= 0),
    two_attempted integer not null check (two_attempted >= two_made),
    three_made integer not null check (three_made >= 0),
    three_attempted integer not null check (three_attempted >= three_made),
    free_throw_made integer not null check (free_throw_made >= 0),
    free_throw_attempted integer not null check (free_throw_attempted >= free_throw_made),
    offensive_rebounds integer not null default 0 check (offensive_rebounds >= 0),
    defensive_rebounds integer not null default 0 check (defensive_rebounds >= 0),
    assists integer not null default 0 check (assists >= 0),
    steals integer not null default 0 check (steals >= 0),
    turnovers integer not null default 0 check (turnovers >= 0),
    blocks integer not null default 0 check (blocks >= 0),
    fouls_committed integer not null default 0 check (fouls_committed >= 0),
    fouls_received integer not null default 0 check (fouls_received >= 0),
    plus_minus integer not null default 0,
    valuation integer not null default 0,
    raw_line jsonb not null,
    primary key (match_id, source_player_id)
);

create index acb_player_game_source_player_idx
    on acb_player_game(source_player_id);

create table acb_event (
    match_id text not null references acb_game(match_id) on delete cascade,
    ingest_index integer not null check (ingest_index >= 0),
    source_order integer,
    play_type integer,
    event_kind text not null,
    source_player_id text,
    local boolean,
    quarter integer check (quarter is null or quarter >= 0),
    minute integer check (minute is null or minute >= 0),
    second integer check (second is null or second between 0 and 59),
    score_home integer check (score_home is null or score_home >= 0),
    score_away integer check (score_away is null or score_away >= 0),
    raw_event jsonb not null,
    primary key (match_id, ingest_index)
);

create index acb_event_source_player_idx
    on acb_event(source_player_id)
    where source_player_id is not null;

alter table acb_raw_snapshot enable row level security;
alter table acb_game enable row level security;
alter table acb_player_game enable row level security;
alter table acb_event enable row level security;

comment on table acb_raw_snapshot is
    'Immutable ACB source bodies for one match endpoint, keyed by SHA-256.';
comment on table acb_game is
    'Source-native ACB game staging row. Does not reuse EuroLeague game ids.';
comment on table acb_player_game is
    'Source-native ACB player box score, preserving ACB player ids.';
comment on table acb_event is
    'ACB play-by-play in source array order; ingest_index is the stable row key.';
