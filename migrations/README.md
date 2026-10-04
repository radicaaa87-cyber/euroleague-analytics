# Migrations

Plain SQL, numbered, each with a matching `down`. Applied through the Supabase
MCP — see `DECISIONS.md` item 10 for why this rather than the Supabase CLI.

| File | What it creates |
|---|---|
| `0001_raw_layer` | The archive (`raw_api_response`, `raw_api_fetch`) and the parsed mirror (`raw_game`, `raw_event`, `raw_boxscore_player`, `raw_boxscore_team`, `raw_shot`) |
| `0002_dimensions` | `player`, `team`, `team_season` |
| `0003_derived_layer` | `lineup`, `lineup_stint`, `possession`, `game_event`, `player_game_minutes`, `game_quality` |
| `0004_query_views` | `v_game`, `v_team_game`, `v_player_game`, `v_lineup_player`, `v_possession`, `v_play_by_play` — read-only views, no tables |
| `0004a_query_views_join_safety` | Fix: `v_player_game` joins `player` with `left join` instead of `inner join`, so a missing dimension row nulls the name instead of deleting the row; documents unenforced join assumptions on `v_team_game` and `v_player_game`. No tables, `create or replace view` only. |
| `0005_game_winner` | Fix: `v_game.winner_team_code` is derived from the official final score instead of passing through `raw_game`, where it is null for every game because the source schedule names the season champion in all 330 rows. See `DECISIONS.md` item 19. No tables, `create or replace view` only. |
| `0006_shot_data_view` | `v_shot_data`, whose complete shot population starts from `game_event` and left-joins `raw_shot` only for real X, Y, and zone values. Free throws remain coordinate-null and `(-1,-1)` is never served. No tables. |
| `0007_shot_data_ft_gate` | Replaces `v_shot_data` so free-throw labelling is derived from event semantics and remains independent of coordinate availability. No tables. |
| `0008_possession_fkey_scope` | Repairs `game_event_possession_fkey` so `ON DELETE SET NULL` targets only nullable `possession_index`, not the non-null season and game key columns. Applied and rollback-probed in production on 2026-08-23. |
| `0009_season_progress` | Adds private `season_progress`, the scheduled-game count and last-load timestamp used to disclose whether a season is complete, in progress, or unknown. Applied on 2026-08-23; E2026 is initialized and historical seasons deliberately remain unknown. |
| `0010_game_source_state` | Adds private per-game provenance for the exact Boxscore, PlaybyPlay, and Points checksums successfully applied to warehouse rows. Reconciled with the equivalent pre-existing production table on 2026-08-23; no unprovable historical marker was inserted. |
| `0011_public_view_security` | Makes all seven warehouse views `security_invoker` and revokes every `anon` and `authenticated` view privilege. Applied on 2026-08-23 UTC after a PostgreSQL 17.11 up/down/up rehearsal and full-result fingerprint comparison. |
| `0012_roster_registration` | Adds the private source-native pre-season registration table approved by Decision 24. Applied on 2026-08-24 as Supabase migration version `20260824122346` after a PostgreSQL 17.6 up/down/up/down rehearsal. |
| `0013_readonly_role` | Adds `el_reader`, the login role the hosted MCP server connects as: `select` on the seven views and the twelve base tables they read, and nothing else. Creates no table and no view. The migration sets **no password**; the owner sets it separately so it never enters version control. Approved by Decision 26. Rehearsed 2026-08-27 — see below. Applied on 2026-08-28 as `20260828203524`, after the full up/down/up/down gate on a disposable PostgreSQL 17.6. |
| `0014_game_officials_view` | Rebuilds `v_game_officials` as its own relation so the referee fields the MCP server exposes come from one place. Applied on 2026-08-28 as `20260828203624`, after the full up/down/up/down gate on a disposable PostgreSQL 17.6. |
| `0015_roster_biography` | Adds `birth_date`, `passport_name` and `passport_surname` to `roster_registration`, nullable, with trimmed-value checks on the two names. Approved by Decision 28's priority set. Applied on 2026-08-28 as `20260828203648`, after the full up/down/up/down gate on a disposable PostgreSQL 17.6. |
| `0016_mcp_row_budget` | Adds the durable per-subject daily row budget: a policy table, a daily aggregate, a 31-day ledger expired on every insert, and the insert-only `el_usage_writer` role. `el_reader` gains no privilege. Applied on 2026-08-28 as `20260828203741`, after the full up/down/up/down gate on a disposable PostgreSQL 17.6. |
| `0017_person_game_link` | Adds `person_game_link`, the within-game observed bridge between the v2 person namespace and the game-source player namespace, plus the security_invoker coverage view `v_person_game_link_coverage`. A foreign key to `raw_boxscore_player` makes a constructed player id fail to insert. Approved by Decision 27. Applied on 2026-08-28 as `20260828203828`, after the full up/down/up/down gate on a disposable PostgreSQL 17.6. The backfill ran on 2026-08-29 with separate owner approval, writing 17,333 links across 732 games; the table measured 3,448,832 bytes, 198.98 bytes per row against the 271 the staging gate projected. See `docs/PERSON_GAME_LINK_BACKFILL_REPORT.md`. |
| `0018_mcp_row_usage_writer_function` | Repair. Adds `record_mcp_row_usage`, a security-definer function that becomes the only write path into `mcp_row_usage`, and withdraws `el_usage_writer`'s direct `insert` grant and policy. 0016's grant could not serve `insert ... returning`, which needs SELECT on every column it returns, so every hosted tool call failed with `permission denied for table mcp_row_usage`. The role ends with strictly less privilege than 0016 gave it. Applied on 2026-08-28 as `20260828210038`. |
| `0019_person_game_link_conflict_view` | Adds `v_person_game_link_conflict`, a security_invoker view that reports every place two link observations disagree about one identity: a person code seen against more than one player id, or a player id seen against more than one person code. Migration 0017 constrains that within one game, which is as far as a table constraint reaches; this covers the space between games. Empty is the healthy state. Reconciled with production drift on 2026-08-29 - the view was applied out of band and the ledger had no record of it - so the up migration uses `create or replace` and was re-applied before being recorded, as `20260829101520`. The re-apply left the view definition md5 unchanged at `282dd5d5`, `security_invoker=true` intact and the grant set byte-identical, which is what established that the drift was a bookkeeping gap and not a shape difference. |
| `0020_tester_role` | Adds `el_tester`, the login role a human tester is given: the same `select` reach as `el_reader` - the seven views and the twelve base tables they read - plus `bypassrls`, and nothing else. Creates no table and no view. Separate from `el_reader` because that one is the hosted server's credential and revoking a leaked tester copy of it would interrupt production. The migration sets **no password**; the owner sets it separately so it never enters version control. Approved by Decision 43. **Rehearsed 2026-09-01, not yet applied.** The full up/down/up/down cycle passed on PostgreSQL 17.11 - the same version as the production 0011 rehearsal - in `.github/workflows/migration-gate.yml` run 33557852446: 23 tables created, removed and recreated identically. The `down` succeeding is what establishes the revoke list is complete, because PostgreSQL refuses to drop a role still holding a privilege. Two limits on that evidence: the container is a stock `postgres:17` without Supabase's extensions, and the database is empty, so the grants are proved to resolve rather than to return correct rows. **Applied on 2026-09-07 UTC as `20260907120441`** from the owner's terminal, ahead of 0024-0026 which grant to the role it creates. No password is set; a tester login still needs one out of band. `docs/evidence/space_0020_tester_role_production_apply.json`. |
| `0021_drop_unused_raw_indexes` | Drops `raw_shot_player_idx`, `raw_event_player_idx`, `raw_event_playtype_idx` and `raw_event_numberofplay_idx`: four raw-layer indexes no query can use, 19.6 MB on production on 2026-09-06. Creates nothing. Approved by Decision 66. Rehearsed 2026-09-06 on a disposable PostgreSQL 17.11 with E2025 loaded: every MCP plan unchanged, the two anti-joins that had used the partial player indexes faster without them, the per-game raw delete on the primary key instead. Evidence in `docs/evidence/space_tier_a_rehearsal_before.json` and `_after.json`. **Applied on 2026-09-06 UTC as `20260906221627`** from the owner's terminal after the approval given in session that day: the database went from 364,326,035 to 344,755,347 bytes, 19,570,688 freed against the 19,579,520 measured. `docs/evidence/space_0021_drop_unused_raw_indexes_production_apply.json`. |
| `0022_drop_event_lineup_and_stint_indexes` | Drops `game_event_home_lineup_idx`, `game_event_away_lineup_idx`, `game_event_stint_idx`, `game_event_possession_idx` and `possession_stint_idx`, 27.8 MB on production on 2026-09-06. Creates nothing. The three queries that asked `game_event` about lineup references now ask `lineup_stint`, in the same change. Approved by Decision 67. Rehearsed 2026-09-07 on a disposable PostgreSQL 17.11 with E2025 loaded: full-season derived rebuild 209 s before, 80 s after; every MCP plan on the primary key instead of the stint index; no new sequential scan over an event-sized table. Evidence in `docs/evidence/space_tier_b_rehearsal_before.json`, `_after.json` and `space_tier_b_lineup_reference_equivalence.json`. **Applied on 2026-09-06 UTC as `20260906221922`** from the owner's terminal: 344,755,347 to 316,984,467 bytes, 27,770,880 freed against the 27.8 MB measured. `docs/evidence/space_0022_drop_event_lineup_and_stint_indexes_production_apply.json`. |
| `0023_drop_raw_event` | Drops `game_event_raw_fkey` and the `raw_event` table, 70.9 MB on production on 2026-09-06. The event stream is stored once, in `game_event`; the gate proves it against the parsed cache and `warehouse_snapshot` hashes its eleven source columns as `game_event_source`. Approved by Decision 68. **The down migration restores the shape, not the rows**, and is valid only on an empty database. **Rehearsed 2026-09-07 on a disposable PostgreSQL 17.11**: the rehearsal's `raw_event` checksums equalled production's before the drop, and the new `game_event_source` baselines were captured from that load. **Applied on 2026-09-06 UTC as `20260906222057`** from the owner's terminal: 316,984,467 to 264,334,483 bytes, 52,649,984 freed (the table had already lost its four indexes to 0021). Decision 68's condition held on the day: production's `game_event_source` for E2024 and E2025 equalled the rehearsal baselines. `docs/evidence/space_0023_drop_raw_event_production_apply.json`. |
| `0024_foul_event_view` | Adds `v_foul_event`, one row per foul event with `foul_kind` (`committed` for the six box-score codes, `bench` for coach/bench pseudo-ids, `drawn` for `RV`) read from `playtype`, never inferred. Approved by Decision 70. Rehearsed 2026-09-07 on the disposable database against `space_e2024` and `space_e2025`: zero reconciliation mismatches on `fouls_commited` and `fouls_received` for both seasons, `docs/evidence/fouls_reconciliation_rehearsal.json`. **Applied on 2026-09-07 UTC as `20260907120509`**, `docs/evidence/space_0024_foul_event_view_production_apply.json`. |
| `0025_referee_game_view` | Adds `v_referee_game`, one row per referee slot per game, unpivoted from `v_game_officials`, with fouls from `raw_boxscore_team` totals and possessions from `v_team_game`. Also grants `el_tester` `select` on `v_game_officials` (0014), which had been granted to `el_reader` only, so `el_get_referee_stats` resolves for testers under `security_invoker`; the down migration revokes it again. Approved by Decision 74. Rehearsed 2026-09-07 on the disposable database against `space_e2024` and `space_e2025`: referee-row count matches the schedule's non-null referee-code slot count and zero foul disagreements against `raw_boxscore_team` for both seasons, `docs/evidence/referee_invariants_rehearsal.json`. **Applied on 2026-09-07 UTC as `20260907120533`**, `docs/evidence/space_0025_referee_game_view_production_apply.json`. |
| `0026_roster_view` | Adds `v_roster`, one row per (season, team, player) that reached a box score, with biography from `roster_registration` linked through `person_game_link` by observed stat lines, never by name (Decision 27). The `registration` CTE's `distinct on` picks the most recent row by `start_at`, tiebroken by `source_registration_id desc`. Also grants `el_reader` and `el_tester` `select` on `roster_registration` (0012, previously granted to neither) and grants `el_tester` `select` on `person_game_link` (0017, previously `el_reader`-only), so `el_get_roster` resolves for both roles under `security_invoker`; the down migration revokes exactly those three grants. Approved by Decision 75. Rehearsed 2026-09-07 on the disposable database against `space_e2024` and `space_e2025` (after loading `roster_registration` and `person_game_link` from the local cache into both, since they were empty there): roster-row count matches the box score's distinct (season, team, player) count exactly and zero rows have a null `birth_date` or `height_cm` for both seasons, re-confirmed after adding the tiebreak, `docs/evidence/roster_view_rehearsal.json`. **Applied on 2026-09-07 UTC as `20260907120604`**, `docs/evidence/space_0026_roster_view_production_apply.json`. |
| `0027_possession_seconds` | Adds nullable `start_seconds_elapsed` and `end_seconds_elapsed` to `possession`, from `elapsed_seconds_raw` at the possession's first and last event. Nullable because production rows already exist and Decision 22 forbids `UPDATE`; the columns are filled by a per-game rebuild through `replace_derived_games`. No hard `end >= start` check constraint - measured against the full E2024 cache: 0.291% of possessions have `end_seconds_elapsed < start_seconds_elapsed`, always inside a documented MARKERTIME backward-clock step (CLAUDE.md), and a hard constraint would abort the rebuild for any such game. Approved by Decision 76. Rehearsed 2026-09-07 on the disposable database against `space_e2024` and `space_e2025`: zero null seconds after a full derived rebuild for both seasons, `docs/evidence/possession_seconds_rehearsal.json`. **Applied on 2026-09-07 UTC as `20260907120628`**, then E2024 and E2025 rebuilt per game with `scripts/rebuild_derived_rows.py --production` (733 s and 872 s; zero null seconds; database 264,424,595 to 283,634,835 bytes, rebuild bloat included). Production `possession` and `game_event` checksums equalled the recaptured baselines for both seasons and `game_event_source` was unchanged: `docs/evidence/space_derived_rebuild_production_baselines.json`. |
| `0028_possession_view_seconds` | Serves the two columns 0027 added through `v_possession`, plus `duration_seconds` computed from them. Same select list as 0004 (security_invoker set by 0011), the three new columns appended at the end. The down migration cannot use `create or replace view` alone - PostgreSQL refuses "cannot drop columns from view" going from 21 columns back to 18 - so it drops and recreates the view, then reapplies the three privilege grants (0011's revoke, 0013's and 0020's `select`) that a drop removes. `scripts/view_migration_gate.py` gave a false FAIL on this migration: its `signature()` helper filters `information_schema.columns` by `table_name` only, and this disposable database also carries `v_possession` in the `space_e2024` and `space_e2025` rehearsal schemas from an unrelated task, so its column list is cross-schema-contaminated once more than one schema holds the view. Gated manually instead: up (21 columns), down (18 columns, exactly the 0004/0011 signature, `el_reader`/`el_tester` `select` restored and `anon`/`authenticated` still absent), up again (21 columns, identical to the first up, grants unchanged) - all filtered explicitly by `table_schema = 'public'`. Approved by Decision 76. Rehearsed together with 0027, same evidence file. **Applied on 2026-09-07 UTC as `20260907120638`**, `docs/evidence/space_0028_possession_view_seconds_production_apply.json`. |
| `0030_acb_staging` | Adds private RLS-protected staging tables for ACB raw snapshots, games, player box scores and source-ordered play-by-play without changing any EuroLeague table. First production smoke import: ACB match `105683` (Supercopa, 2026-09-20) loaded 24 player rows and 620 events; three source bodies were retained by SHA-256. **Applied on 2026-10-04 UTC as `20261004130018`** through the Supabase MCP after owner approval. |
| `0031_athlete_identity` | Adds canonical `athlete` and `athlete_source_identity` tables. Source ids remain opaque and unchanged. Joel Parra is the first verified cross-source link: EL `P007464` ↔ ACB `20212265`. **Applied on 2026-10-04 UTC as `20261004131001`** through the Supabase MCP after owner approval. |
| `0032_athlete_game_history` | Adds `v_athlete_game_history`, a chronological EL+ACB player-game view that joins only through explicit canonical identities, never names. **Applied on 2026-10-04 UTC as `20261004132501`** through the Supabase MCP. |
| `0033_source_native_identity_status` | Adds `source_native` identity status so every observed EL/ACB source id can receive a canonical athlete without pretending that an unverified cross-source match exists. Production was populated with 432 ACB ids and 262 EL ids; only verified cross-source links are merged. **Applied on 2026-10-04 UTC as `20261004132715`** through the Supabase MCP. |

## The 0013 rehearsal, 2026-08-27

`scripts/migration_gate.py` ran the full up/down/up/down cycle including
`0013_readonly_role` against a disposable local PostgreSQL, and passed: 19 tables
created, removed and recreated identically. The `down` succeeding is itself the
evidence that the revoke list is complete, because PostgreSQL refuses to drop a
role that still holds a privilege.

The role was then exercised for real. With every migration applied and a
throwaway password set, `tests/test_readonly_role.py` passed all 14 tests
connected as `el_reader`: all seven `security_invoker` views readable,
`season_progress` and `team_season` readable, and `insert`, `update`, `delete`,
`create table` and a read of the ungranted `lineup_stint` each refused with
`InsufficientPrivilege`. That measures the security-invoker reasoning in the
migration's header rather than only asserting it.

**Two limits on this evidence, stated rather than glossed.** The instance was
**PostgreSQL 16.2**, not the 17.x production runs, because that is what the
disposable server bundled; role and grant semantics are unchanged between them,
but this was not a like-for-like version rehearsal. And the database was
**empty**, so the reads returned no rows — the tests prove the grants resolve,
not that any query returns correct data.

The committed migration set and production both define nineteen tables. See
`docs/PRODUCTION_MIGRATIONS_AND_PROGRESS_REPORT.md` for the rehearsal, drift
reconciliation, and production evidence.

## The gate, and why it expires

`ROADMAP.md` opens this phase with one requirement: the migrations apply
cleanly to an empty database and roll back cleanly. `scripts/migration_gate.py`
runs the full cycle — up, down, up, down — and refuses to start if the public
schema already holds tables.

**That gate can only be run honestly once.** After Phase 4 loads a season,
"rolls back cleanly" would mean destroying real data, so the test can never be
repeated as written. It was run on 2026-08-09 against the empty project and
passed: 16 tables created, removed, and recreated identically.

If a future migration changes the schema, the honest version of this test is a
fresh empty database — a Supabase branch or a local Postgres — not the
production project.

**View-only migrations are the one exception, and only in this exact shape.** A
`create or replace view` that keeps the same column names, types and order writes
no row and drops no table, so its full cycle can be run in place:

```sh
python scripts/view_migration_gate.py 0005_game_winner v_game
python scripts/view_migration_gate.py 0006_shot_data_view v_shot_data --new-view
```

That runs up, down and up again, comparing the view's column signature at every
step and failing if a column moved — because a migration that moves a column is
not view-only and does not qualify. It is how `0005_game_winner` was gated on
2026-08-13. It is not a licence to skip the empty database for anything that
touches a table, and it proves only that the shape is safe: that the new
definition is *correct* is asserted separately, in the phase gate, against the
values the views actually serve.

`--new-view` is for repeating a gate after a create-view migration is already
applied. It first runs down and proves the named view is absent, then performs
up/down/up and leaves it up. Before opening a database connection, the gate
rejects SQL outside the named view's create/comment/drop statements; table DDL,
row writes, and extra objects cannot qualify.

## Conventions

- Lowercase `snake_case` identifiers throughout. Postgres folds unquoted
  identifiers to lowercase, and mixed-case names then need quoting forever.
- `text` rather than `varchar(n)`; `timestamptz` rather than `timestamp` for
  anything comparable across games; `integer` for counts.
- Natural composite primary keys, not surrogate identity columns, on everything
  derived from a payload position. `SCHEMA_PROPOSAL.md` section 8 gives the
  reason: re-ingest the same cached payload and every row lands on the same
  key, so a rebuild does not renumber and derived tables do not need rebuilding
  in lockstep. The two exceptions are `raw_api_response` and `raw_api_fetch`,
  where an observation genuinely has no natural key.
- Foreign key columns are indexed explicitly. Postgres does not do it for you,
  and an unindexed foreign key turns every join and every cascade into a full
  scan.
- Trimming is enforced by check constraints rather than left to the loader.
  Untrimmed identifiers join to nothing and raise no error, which is the exact
  silent failure this project is built to avoid.

## Row level security

Every table has RLS enabled and **no policies**. That is deliberate, and the
Supabase linter's INFO notice about it is the expected state, not a finding.

The pipeline reaches Postgres as the owning role through the session pooler,
and the owner bypasses RLS. Enabling it with no policies denies the `anon` and
`authenticated` roles that back the public PostgREST endpoint, so the REST API
exposes nothing. Verified on 2026-08-09: with one row present, the owner saw 1
and `anon` saw 0.

The warehouse is served through the MCP layer, not through PostgREST. The table
statement above remains true, but it was not sufficient for views: the
2026-08-23 advisor run found six legacy security-definer views with inherited
public grants. Migration 0011 closed that path on 2026-08-23 UTC by applying
both controls independently: every warehouse view is now `security_invoker`,
and neither public API role has a privilege on any of them. Direct role tests
return PostgreSQL `42501` for both `anon` and `authenticated`; the owning MCP
role and `service_role` retain the complete, unchanged result sets. See
`docs/PUBLIC_VIEW_SECURITY_HARDENING_REPORT.md`.
