-- Audit whether pregame news/context collection actually ran.
--
-- Zero discovered events is not equivalent to zero collected evidence.  This
-- table records collection coverage independently from pregame_context_event so
-- training can distinguish "searched and found nothing relevant" from "no
-- historical collection existed".

create table pregame_context_collection (
    collection_id bigint generated always as identity primary key,
    season_code text not null,
    gamecode integer not null,
    team_code text not null check (team_code = btrim(team_code) and team_code <> ''),
    collected_at timestamptz not null default now(),
    query_count integer not null check (query_count >= 0),
    successful_query_count integer not null check (
        successful_query_count >= 0 and successful_query_count <= query_count
    ),
    failed_query_count integer not null check (
        failed_query_count >= 0
        and failed_query_count = query_count - successful_query_count
    ),
    players_queried integer not null check (players_queried >= 0),
    items_seen integer not null check (items_seen >= 0),
    inserted_event_count integer not null check (inserted_event_count >= 0),
    collector_version text not null default 'rss_v1',
    metadata jsonb not null default '{}'::jsonb
);

create index pregame_context_collection_game_idx
    on pregame_context_collection(season_code, gamecode, team_code, collected_at);

alter table pregame_context_collection enable row level security;

revoke all on pregame_context_collection from anon, authenticated;
grant select on pregame_context_collection to el_reader, el_tester;

comment on table pregame_context_collection is
    'Audit of pregame context collection runs. Separates searched-with-no-signal from missing historical collection.';
