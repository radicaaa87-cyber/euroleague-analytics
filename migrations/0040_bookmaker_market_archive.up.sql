-- Historical bookmaker player-points market archive.
--
-- The source document and every parsed offer retain provenance independently.
-- Historical fetch time is never treated as market time: offer_date and archive
-- capture time are stored separately so future model work cannot leak 2026
-- collection timestamps into a 2024/25 backtest.

create table bookmaker_source_document (
    document_id bigint generated always as identity primary key,
    bookmaker text not null check (
        bookmaker in ('mozzart', 'starbet', 'sportlife', 'other')
    ),
    canonical_url text not null check (
        canonical_url = btrim(canonical_url) and canonical_url <> ''
    ),
    fetched_url text not null check (
        fetched_url = btrim(fetched_url) and fetched_url <> ''
    ),
    discovery_method text not null check (
        discovery_method in ('seed', 'wayback_cdx', 'brave_search', 'manual')
    ),
    document_date date,
    archive_capture_at timestamptz,
    discovered_at timestamptz not null default now(),
    fetched_at timestamptz not null default now(),
    content_sha256 text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    content_length integer not null check (content_length > 0),
    page_count integer check (page_count is null or page_count > 0),
    parser_version text not null,
    parse_status text not null check (
        parse_status in ('parsed', 'no_player_points', 'unsupported', 'error')
    ),
    metadata jsonb not null default '{}'::jsonb,
    unique (bookmaker, canonical_url, content_sha256)
);

create index bookmaker_source_document_date_idx
    on bookmaker_source_document(bookmaker, document_date);

create table bookmaker_player_points_offer (
    offer_id bigint generated always as identity primary key,
    document_id bigint not null references bookmaker_source_document(document_id)
        on delete cascade,
    bookmaker text not null check (
        bookmaker in ('mozzart', 'starbet', 'sportlife', 'other')
    ),
    offer_date date,
    event_time_local time,
    source_timezone text not null default 'Europe/Belgrade',
    source_event_code text,
    participant_text text not null check (
        participant_text = btrim(participant_text) and participant_text <> ''
    ),
    player_name_raw text,
    player_name_normalized text,
    team_name_raw text,
    athlete_id uuid references athlete(athlete_id),
    athlete_match_method text,
    athlete_match_confidence numeric(5,4) check (
        athlete_match_confidence is null
        or athlete_match_confidence between 0 and 1
    ),
    season_code text,
    gamecode integer check (gamecode is null or gamecode > 0),
    game_match_method text,
    game_match_confidence numeric(5,4) check (
        game_match_confidence is null
        or game_match_confidence between 0 and 1
    ),
    points_line numeric(6,2) not null check (points_line >= 0 and points_line < 100),
    under_odds numeric(7,3) check (under_odds is null or under_odds >= 1),
    over_odds numeric(7,3) check (over_odds is null or over_odds >= 1),
    page_number integer check (page_number is null or page_number > 0),
    row_text text not null check (row_text = btrim(row_text) and row_text <> ''),
    row_sha256 text not null check (row_sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz not null default now(),
    unique (document_id, row_sha256)
);

create index bookmaker_player_points_offer_player_idx
    on bookmaker_player_points_offer(athlete_id, offer_date)
    where athlete_id is not null;

create index bookmaker_player_points_offer_game_idx
    on bookmaker_player_points_offer(season_code, gamecode, athlete_id)
    where season_code is not null and gamecode is not null;

create index bookmaker_player_points_offer_market_idx
    on bookmaker_player_points_offer(bookmaker, offer_date, points_line);

create table bookmaker_collection_run (
    collection_id bigint generated always as identity primary key,
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    from_date date,
    to_date date,
    bookmakers text[] not null default '{}'::text[],
    discovery_count integer not null default 0 check (discovery_count >= 0),
    fetched_count integer not null default 0 check (fetched_count >= 0),
    parsed_document_count integer not null default 0 check (parsed_document_count >= 0),
    inserted_offer_count integer not null default 0 check (inserted_offer_count >= 0),
    error_count integer not null default 0 check (error_count >= 0),
    collector_version text not null,
    metadata jsonb not null default '{}'::jsonb
);

create index bookmaker_collection_run_started_idx
    on bookmaker_collection_run(started_at desc);

alter table bookmaker_source_document enable row level security;
alter table bookmaker_player_points_offer enable row level security;
alter table bookmaker_collection_run enable row level security;

revoke all on bookmaker_source_document from anon, authenticated;
revoke all on bookmaker_player_points_offer from anon, authenticated;
revoke all on bookmaker_collection_run from anon, authenticated;

grant select on bookmaker_source_document to el_reader, el_tester;
grant select on bookmaker_player_points_offer to el_reader, el_tester;
grant select on bookmaker_collection_run to el_reader, el_tester;

comment on table bookmaker_source_document is
    'Immutable bookmaker source snapshots keyed by URL plus document SHA-256.';
comment on table bookmaker_player_points_offer is
    'Parsed EuroLeague player-points lines with source-row provenance and optional athlete/game links.';
comment on table bookmaker_collection_run is
    'Audit trail for autonomous bookmaker document discovery, fetch and parsing runs.';
