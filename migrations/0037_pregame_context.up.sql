-- Leakage-safe 72-hour pregame news/context archive.
-- Each row is evidence discovered before an EL game.  The collector and every
-- model consumer must additionally enforce published_at < tipoff and the
-- 72-hour lower bound against v_game.utc_date.

create table pregame_context_event (
    context_id bigint generated always as identity primary key,
    season_code text not null,
    gamecode integer not null,
    team_code text not null check (team_code = btrim(team_code) and team_code <> ''),
    player_id text,
    event_type text not null check (
        event_type in (
            'availability_out',
            'availability_doubt',
            'return',
            'role_up',
            'role_down',
            'rotation_change',
            'roster_change',
            'other'
        )
    ),
    role_direction smallint not null check (role_direction between -1 and 1),
    severity numeric(5,4) not null check (severity between 0 and 1),
    source_confidence numeric(5,4) not null check (source_confidence between 0 and 1),
    role_impact_score numeric(7,5) generated always as (
        role_direction::numeric * severity * source_confidence
    ) stored,
    source_name text,
    source_url text not null check (source_url = btrim(source_url) and source_url <> ''),
    publisher_url text,
    headline text not null check (headline = btrim(headline) and headline <> ''),
    summary text,
    published_at timestamptz not null,
    fetched_at timestamptz not null default now(),
    query_text text,
    classification_method text not null default 'keyword_v1',
    metadata jsonb not null default '{}'::jsonb,
    check (player_id is null or player_id = btrim(player_id))
);

create unique index pregame_context_event_dedupe_idx
    on pregame_context_event (
        season_code,
        gamecode,
        team_code,
        coalesce(player_id, ''),
        source_url
    );

create index pregame_context_event_game_idx
    on pregame_context_event(season_code, gamecode, team_code, published_at);

create index pregame_context_event_player_idx
    on pregame_context_event(player_id, published_at)
    where player_id is not null;

create view v_pregame_player_context_features
with (security_invoker = true)
as
select
    season_code,
    gamecode,
    team_code,
    player_id,
    count(*) as context_event_count,
    max(published_at) as context_feature_cutoff_time,
    max(source_confidence) as context_max_source_confidence,
    max(severity) as context_max_severity,
    round(sum(greatest(role_impact_score, 0)), 5) as context_role_up_score,
    round(sum(greatest(-role_impact_score, 0)), 5) as context_role_down_score,
    round(
        max(severity * source_confidence)
            filter (where event_type = 'availability_out'),
        5
    ) as context_out_score,
    round(
        max(severity * source_confidence)
            filter (where event_type = 'availability_doubt'),
        5
    ) as context_doubt_score,
    round(
        max(severity * source_confidence)
            filter (where event_type = 'return'),
        5
    ) as context_return_score
from pregame_context_event
where player_id is not null
group by 1, 2, 3, 4;

create view v_pregame_team_context_features
with (security_invoker = true)
as
select
    season_code,
    gamecode,
    team_code,
    count(*) as team_context_event_count,
    max(published_at) as team_context_feature_cutoff_time,
    max(source_confidence) as team_context_max_source_confidence,
    max(severity) as team_context_max_severity,
    round(sum(greatest(role_impact_score, 0)), 5) as team_role_up_score,
    round(sum(greatest(-role_impact_score, 0)), 5) as team_role_down_score
from pregame_context_event
group by 1, 2, 3;

alter table pregame_context_event enable row level security;

revoke all on pregame_context_event from anon, authenticated;
revoke all on v_pregame_player_context_features from anon, authenticated;
revoke all on v_pregame_team_context_features from anon, authenticated;

grant select on pregame_context_event to el_reader, el_tester;
grant select on v_pregame_player_context_features to el_reader, el_tester;
grant select on v_pregame_team_context_features to el_reader, el_tester;

comment on table pregame_context_event is
    'Timestamped 72h pregame web/news evidence. Post-tipoff evidence must never be used as a model feature.';
