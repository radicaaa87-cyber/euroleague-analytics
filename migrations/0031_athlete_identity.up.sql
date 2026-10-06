-- Canonical athlete identities across source-native competition ids.
-- Source ids are never rewritten; links retain the evidence used to approve them.

create table athlete (
    athlete_id uuid primary key default gen_random_uuid(),
    display_name text not null check (display_name = btrim(display_name) and display_name <> ''),
    birth_date date,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table athlete_source_identity (
    source text not null check (source in ('EL', 'ACB')),
    source_player_id text not null check (
        source_player_id = btrim(source_player_id) and source_player_id <> ''
    ),
    athlete_id uuid not null references athlete(athlete_id) on delete cascade,
    display_name text not null check (display_name = btrim(display_name) and display_name <> ''),
    birth_date date,
    height_cm integer check (height_cm is null or height_cm between 120 and 250),
    country_code text,
    team_source_id text,
    match_status text not null check (
        match_status in ('auto_link', 'manual_verified', 'review')
    ),
    evidence jsonb not null default '{}'::jsonb,
    first_seen_at timestamptz,
    last_seen_at timestamptz,
    created_at timestamptz not null default now(),
    primary key (source, source_player_id)
);

create index athlete_source_identity_athlete_idx
    on athlete_source_identity(athlete_id);

alter table athlete enable row level security;
alter table athlete_source_identity enable row level security;

comment on table athlete is
    'Warehouse-level person identity shared across competitions; never a source id.';
comment on table athlete_source_identity is
    'One source-native player id linked to a canonical athlete with explicit evidence.';
