create table public.model_registry (
  model_family text not null
    check (model_family = btrim(model_family) and model_family <> ''),
  version text not null
    check (version = btrim(version) and version <> ''),
  status text not null default 'candidate'
    check (status in ('candidate', 'validated', 'production', 'retired', 'rejected')),

  artifact_bucket text not null
    check (artifact_bucket = btrim(artifact_bucket) and artifact_bucket <> ''),
  artifact_path text not null
    check (artifact_path = btrim(artifact_path) and artifact_path <> ''),
  artifact_sha256 text not null
    check (artifact_sha256 ~ '^[0-9a-f]{64}$'),
  artifact_size_bytes bigint
    check (artifact_size_bytes is null or artifact_size_bytes > 0),

  git_commit text not null
    check (git_commit ~ '^[0-9a-f]{40}$'),
  framework text not null
    check (framework = btrim(framework) and framework <> ''),
  framework_version text,
  model_class text not null
    check (model_class = btrim(model_class) and model_class <> ''),

  trained_at timestamptz not null,
  registered_at timestamptz not null default now(),
  status_changed_at timestamptz not null default now(),
  promoted_at timestamptz,

  train_seasons text[] not null,
  validation_seasons text[] not null default '{}'::text[],
  blind_test_seasons text[] not null default '{}'::text[],
  feature_names text[] not null,

  selected_params jsonb not null default '{}'::jsonb,
  metrics jsonb not null default '{}'::jsonb,
  training_metadata jsonb not null default '{}'::jsonb,
  notes text,

  primary key (model_family, version),

  constraint model_registry_train_seasons_nonempty
    check (cardinality(train_seasons) > 0),
  constraint model_registry_feature_names_nonempty
    check (cardinality(feature_names) > 0),
  constraint model_registry_production_promoted
    check (status <> 'production' or promoted_at is not null)
);

comment on table public.model_registry is
  'Immutable-version registry for trained analytics models. Artifacts live outside Postgres; this table stores identity, lineage, metrics and the active lifecycle state.';

comment on column public.model_registry.model_family is
  'Stable family such as player_points, game_winner or game_spread.';
comment on column public.model_registry.version is
  'Immutable semantic model version within the family.';
comment on column public.model_registry.artifact_path is
  'Object path in artifact_bucket. A new model version must use a new path; model artifacts are not overwritten.';
comment on column public.model_registry.metrics is
  'Validation/blind-test metrics captured when this exact artifact was registered.';
comment on column public.model_registry.training_metadata is
  'Additional reproducibility metadata such as row counts, split details and feature schema hash.';

create unique index model_registry_one_production_per_family_idx
  on public.model_registry (model_family)
  where status = 'production';

create index model_registry_status_idx
  on public.model_registry (status, model_family);

alter table public.model_registry enable row level security;

grant select on table public.model_registry to el_reader, el_tester;
