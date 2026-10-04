# Roadmap

Where the project is, what comes next, and what must be true before each phase
starts. Read this with `DECISIONS.md` and `AGENTS.md`.

**Deadline that matters:** the first E2026 game is scheduled for 2026-09-24.
The authoritative remaining-session sequence is at the end of this file.

---

## Done

**Phase 0 — reconnaissance and design.** Complete.

- `exploration/FINDINGS.md` — single-game API reconnaissance
- `exploration/SEASON_SWEEP.md` — full-season validation, 330 games, 176,483 events
- `exploration/SCHEMA_PROPOSAL.md` — schema design, approved
- `DECISIONS.md` — the live decision log; the old count here was stale
  throughout Phases 2 through 8, so follow the file rather than a copied total

Established: lineup reconstruction is viable (99.54% of player-games reproduce
official minutes to the second), overtime is safe to model, the clock must be
consumed unmodified, and the failure surface is a single known defect.

**Phase 1 — close the two open items.** Complete.

- `exploration/OPEN_ITEMS.md` — empirical measurements and recommendations
- Item 7: 0 of 60 sampled historical responses changed; immutable,
  checksum-addressed versions and per-game derived rebuilds approved
- Item 8: 82.434 logical bytes/event with the optional text and 51.572 without;
  all seasons targeted, optional event text dropped, physical-size gate
  required before production backfill. The season count was amended from an
  unmeasured 19 to a measured 23 on 2026-08-10 — see `DECISIONS.md` item 8.

---

## Next

### Phase 2 — scaffolding, schema and migrations

Three pieces, in this order.

**2a — repository scaffolding. Complete.** A `src/euroleague/` package with the
cache reader and the database settings, a `tests/` tree with nine fixture games
selected by defect, four pinned dependencies, ruff, and a GitHub Actions
workflow that runs lint, format and tests on every push. 19 tests, no network
and no database, green on Linux and Python 3.14.

Public at `github.com/egemeny13/euroleague-analytics`.

**2b — the Supabase project.** Done: `euroleague-analytics`,
`pctiewdpstnwcutrvegu`, eu-central-1. See `DECISIONS.md` item 12 for the two
free-tier constraints it carries.

**2c — the migrations.** Numbered `up`/`down` SQL files in `migrations/`,
implementing `SCHEMA_PROPOSAL.md` **as amended by `DECISIONS.md`**. Do not
deviate from either without stopping for a decision. Three amendments are easy
to miss because the proposal still shows the superseded version:

- `raw_event` has no `player_name`, `dorsal` or `playinfo` — item 8.
- `raw_api_response` is content-addressed with a version history and a
  current-version pointer, and holds **no response bodies** — items 7 and 9.
- `competition_code` exists everywhere it is needed, though only EuroLeague
  will be loaded — item 11.

**Gate: passed 2026-08-09.** `scripts/migration_gate.py` ran the full cycle —
up, down, up, down — against the empty project. 16 tables created, removed and
recreated identically, leaving the database empty. The three migrations were
then applied and recorded through the Supabase MCP.

That gate could only be run once. After Phase 4 the database holds data and
"rolls back cleanly" can never again be tested honestly against production; a
future schema change must be gated on a fresh empty database instead.

**Fresh-database gate now exists, 2026-08-19.** A disposable PostgreSQL 17.6
instance—the same major.minor as production—runs locally on port 5433 with the
empty `euroleague_test` database. All eight migrations have now completed an
up/down/up cycle there and reproduced an identical **16-table, 7-view** schema.
Migrations 0004 through 0007 had never been exercised through that complete
reversal cycle before this date. Production was not used for the test and
remains read-only for local database gates.

Also verified: RLS denies the public REST endpoint. With one row present, the
owning role saw 1 and `anon` saw 0.

### Phase 3 — the test suite. Complete.
Promote the throwaway checks in `exploration/sweep_season.py` into a permanent
test suite: the four tripwires that currently never fire, the three that
quarantine games, and the box-score reconciliation across 50+ games.

**Gate — the hardest rule in this roadmap: do not load a single row into the
warehouse before this phase is green.** A warehouse filled with unvalidated
data cannot be debugged, because there is no baseline to compare against.

**Gate: passed 2026-08-09.** The permanent cache-only library and tests reproduce
the complete E2024 baseline: 330 games and 176,483 events; 9 games/36 player rows
mismatching raw minutes by exactly 60 seconds; 2 games/4 rows after the narrow
overtime correction; 0 on-court violations; 7 off-court attributions; and 0
player or team points mismatches. The correction re-times 32 substitution rows,
moves no lineup, and is enabled only because it strictly improves agreement with
the official box score. See `docs/PHASE_3_REPORT.md`.

### Phase 4 — raw ingest. Complete; size gate exceeded.
E2024 was loaded from the existing cache into the raw layer with no EuroLeague
API requests. Every raw table reconciles per game, all 661 archive checksums
match, and a second complete load left counts and content fingerprints unchanged.

The physical-size condition did not pass. Immediately after full compaction,
billing-aware whole-database growth projected to 725,786,624 bytes across 19
E2024-sized seasons; routine vacuum metadata makes the operational projection
728,276,992 bytes. Both exceed the 474,311,115-byte usable budget. The original
post-reload measurement was higher because it included dead space; all readings
and the correction are preserved in `docs/PHASE_4_REPORT.md`. No hot-window
size has been chosen. The owner must make that Decision 8 follow-up before
production backfill.

### Phase 5 — derived layer: lineups and stints. Complete.
Build `game_event`, `lineup`, `lineup_stint`, `player_game_minutes`,
`game_quality`.

**Gate: passed 2026-08-10.** All lineup invariants are green across 330 games and
the quarantine list matches `SEASON_SWEEP.md` exactly — 2 games failing minutes
after correction (43, 98), 7 failing attribution (23, 63, 72, 131, 139, 242,
323), 0 failing on-court. The live gate re-checks those populations against the
database rather than against the builder, so agreement is not assumed.
Persisted: 176,483 `game_event` rows one-for-one with `raw_event`, 5,985
lineups, 13,927 stints, 7,863 player-game minute rows, 330 quality rows.
`possession` is deliberately empty. A second complete load left every content
fingerprint unchanged. See `docs/PHASE_5_REPORT.md`.

**How it proceeded while marked blocked.** This phase was gated on the owner
resolving Phase 4's failed size gate, and that decision is still open. Phase 5
ran anyway, scoped hard to E2024, with no fetch, no second season and no
backfill — nothing the blocked decision governs. Recorded here because the block
was real and was passed, not because passing it was authorised in advance.

**Decision provenance:** nobody approved this exception in advance. This is the
precedent against which the rule forbidding self-granted roadmap-gate exemptions
was written.

**Storage: resolved 2026-08-19.** Loading the derived layer took the
billing-aware 19-season projection from 725,786,624 bytes to 1,797,734,400
against a 474,311,115-byte budget, and `test_live_phase_4_gate` was left
deliberately red on that assertion until a hot window was chosen.

The window was chosen on 2026-08-18 (E2024, E2025, E2026 — `DECISIONS.md`
item 20 as amended) and the compaction that made it fit ran the same day,
taking the database from 454,859,573 to 291,380,021 bytes
(`docs/STORAGE_COMPACTION_RESULT.md`). Under Condition B the gate now asserts
**that** window: 732 loaded games plus a complete 380-game E2026 priced at the
measured per-game rate, projecting 429,307,113 bytes against the unchanged
474,311,115-byte budget. **It is green, and the budget was not touched.**

The 23-season assertion is kept, inverted, as a standing measurement: the full
backfill must continue *not* to fit. If it ever starts fitting, the reasoning
behind Decision 20 has changed and somebody should look.

**Two further gates were found red the same day, both predating the storage
work and both the same staleness:** they asserted E2024 was the only season in
the warehouse, which stopped being true when E2025 was loaded. `describe_warehouse`
was correctly reporting both. Re-pinned per season, which is stronger than what
they replaced — E2024's games, exclusions and team count are now asserted
exactly rather than against cross-season totals that grow with every load.

**The Phase 5 physical-size gate is now measured per game, not per season**
(owner's decision, 2026-08-19). It memorised six exact byte totals taken when
E2024 was the only season loaded, so it broke on E2025 arriving — correct growth
it could not distinguish from a regression — and would have gone red weekly once
E2026 began loading. It now asserts bytes per game inside a measured band, which
holds through a live season. What that band cannot see is uniform growth under
2.5%, about 6.4 MB across 732 games; the fixed-budget window projection in
`test_live_phase_4_gate` is the check that catches that by another route.

**The capacity figure is provisional and borderline.** Roughly 5 complete
E2024-sized seasons fit. Two reasons not to choose a window on that number yet:
`possession` is empty, so the per-season cost is going to rise in Phase 6; and
whole-database readings drift by a few hundred kilobytes on their own, which is
the same order as the distance between the 5-season and 4-season answers.

### Phase 6 — possessions
The fragile phase. Free-throw trip grouping is the project's only remaining
major inference and it breaks in exactly the situations that matter: and-ones,
technical fouls, substitutions injected mid-sequence. Test those cases
specifically, not the common case.

Remember: every offensive foul already carries its own `TO` row. Count the `TO`
and ignore the `OF`, or the season gains 1,185 phantom turnovers.

**The definitions are approved.** `docs/PHASE_6_POSSESSION_DEFINITIONS.md`,
approved 2026-08-10, is the specification: possessions continue through
offensive rebounds, the and-one ends at the basket, and a free-throw trip is
broken by a ball-touching event or a new foul. Two questions were withdrawn from
approval because they asked the owner to choose between unmeasured
possibilities — the period-end markers (M1) and team rebounds (M2).

**Free-throw trip grouping is done.** `docs/FREE_THROW_TRIP_GROUPING_REPORT.md`.
The approved rule reproduces the five approved trip-length bins exactly and is
pinned per season. One correction came out of review: the single-award flag is a
one-sided test and was renamed to say so, because a short group can still hold
two foul awards — games 120, 159 and 60 are hand-verified fixtures. Whether to
split those groups changes the approved Section 4 rule and **needs the owner's
decision**; it matters because a technical free throw does not end a possession.

**M1 and M2 are done.** `docs/PHASE_6_M1_M2_MEASUREMENTS.md`, both measured over
E2024 and E2025 independently.

- **M1:** no period is lost or invented. `BP` is exactly one per period in both
  seasons. The 14 surplus end markers in E2024 are 12 overtime games marking
  their last period `EP` then `EG`, plus 2 duplicate `EG` rows; E2025 is 17 + 4.
  Close a period on the array structure, never by counting end markers.
- **M2:** team rebounds behave exactly like player rebounds on who had the ball
  before and who has it after, so they end and continue possessions the same
  way. They differ only in being booked on the same clock second as the miss,
  the signature of a dead ball. The end-of-period hypothesis is refuted.

With both answered, the counting rule can now be written.

**Phase 6 is complete, with a named and quarantined residual.**
`docs/PHASE_6_POSSESSIONS_REPORT.md`. Each team's total is built independently
from the five approved endings. 47,831 E2024 possessions are persisted, with
`possession_index` on 109,312 `game_event` rows, and a second load leaves every
fingerprint unchanged.

- **Gate: 314 of 330 (E2024) and 385 of 402 (E2025).** The 16 failing E2024
  games and 17 failing E2025 games exceed `POSSESSION_GATE_TOLERANCE`, which is
  2. The E2024 failures are quarantined in `game_quality` as `possession_gate`
  and excluded by default, joining the 7 attribution and 2 minutes failures.
  The gate test itself is unchanged and still red. By the owner's decision on
  2026-08-11, the named failures are quarantined rather than blocking the phase.
- **Possession counts have no external ground truth.** Nobody publishes a
  comparable EuroLeague count. The real test is mechanical: each team's total
  is counted independently from the five approved endings, and the two totals
  must agree within the tolerance of 2. The failure direction is a missing
  ending for one team. Five candidate causes have been measured and eliminated;
  the residual is not explained.
- **The point check is an exhaustiveness check, not possession validation.**
  Possession points plus off-possession points equal the official final score in
  all 330 E2024 and all 402 E2025 games. This proves the attribution code drops,
  double-counts and invents no point, including around and-ones and technical
  free throws. It would remain green if a possession boundary moved and the
  points moved into the adjacent possession.
- **Straddle rate: 6.10%** — 2,917 of 47,831, published as `DECISIONS.md` item 5
  requires. E2024 only; re-measure when the lineup layer's scope widens.
- **Storage re-measured: 4 seasons fit, not 5.** Possessions cost about 14.2 MB
  a season, taking the compacted public relations from 90,570,752 to
  104,783,872 bytes.

Five candidate causes of the residual are measured and eliminated, so the next
attempt needs a new instrument: the free-throw suppression rule, unresolved
missed shots, end-of-period double closing, trip splitting, and the free-throw
award split, which was built on 2026-08-11 and moves the combined total by zero.
The direction is known — the failures are a **missing ending for one team**.

**The trap is named.** Ending a possession whenever the next ball event belongs
to the other team would pass the gate almost everywhere and prove nothing,
because it forces the very alternation the gate exists to test. That is the hole
Decision 6 closes.

**A latent schema defect is recorded.** `game_event_possession_fkey` is composite
and declared `ON DELETE SET NULL`, so a delete tries to null `season_code` too.
Decision 22's parent-first writer avoids firing the action by deleting child
events before possession parents, but the constraint itself is still wrong; a
later migration should scope the action to `possession_index`.

### Phase 7 — the MCP server. Complete.

17 read-only `el_` tools now expose warehouse coverage, games, team and player
statistics, lineups, on/off splits, possessions, source-ordered play by play, shot data
with coordinates (`el_get_shot_data`), fouls by type reconciled to the box score
(`el_get_fouls`), referee season aggregates unpivoted from the schedule's officiating
crew (`el_get_referee_stats`), and roster biography linked to the box-score player by
observed stat lines, never by name (`el_get_roster`). They aggregate through ten
versioned views; no table or external dependency was added. Counting statistics come from the official box score, while
possessions, pace, lineups, on/off, clutch filters, per-100 rates, and shot coordinates remain
the validated derived layer. The stdio entry point answers real MCP requests and keeps diagnostics off
protocol stdout.

**Gate: passed 2026-08-13.** All 18 live checks pass. The tools reconcile to
47,831 E2024 possessions, 2,917 substitution-straddling possessions, 24 games
excluded by default, and 330 games whose two team lines reproduce the official
final score. The gate also found and fixed a query that trusted the source API's
unreliable `IsPlaying` flag; player participation now follows positive official
seconds. The normal database-free suite remains green. See
`docs/PHASE_7_REPORT.md`.

Every response reports coverage and exclusions. The shared envelope refuses to
build a response containing a minute- or second-derived value without declaring
whether its basis is corrected, raw, or official. `raw_shot` is populated (holding
51,193 E2024 rows and 64,137 E2025 rows, measured 2026-08-22 against production via
`select season_code, count(*) from raw_shot group by 1 order by 1`), and E2025 is loaded
(402 games, measured 2026-08-22 against production via `select season_code, count(*) from raw_game group by 1 order by 1`).
`el_get_shot_data` serves shot coordinates across both loaded seasons.

Three earlier issues remain open and visible: the storage hot-window decision,
the named Phase 6 possession-gate residual, and the composite
`game_event_possession_fkey` defect. Phase 7 discloses their effects but does not
quietly redefine or repair them.

### Phase 8 — evaluations. Complete.

`evaluation.xml` holds ten complex, realistic, verifiable questions the server
must answer. Each is independent, read-only, needs several tool calls, and has
one stable correct answer computed outside the tool path first.

This phase is not optional. It is what separates this from every other
EuroLeague repo on GitHub, and it is the thing worth showing a club.

**Gate: passed 2026-08-13.** All 15 checks pass, and the Phase 7 gate's 18 stayed
green beside them. `tests/test_phase_8_evaluations.py` re-earns every published
answer on demand along two independent paths — the ground-truth SQL recorded in
the file, and the `el_` handlers a model would actually call — and both must
agree with the number printed in `<expected_answer>`. A verified-once file is a
claim; a re-checked one is a regression suite. See `docs/PHASE_8_REPORT.md`.

The gate found two things worth naming:

- **A published rate was wrong.** The straddle rate for the default-covered
  population is 6.07%, not the 6.06% the file claimed — 2,687 of 44,301 is
  6.0653%. Counts were right, rounding was not. Corrected in both places. The
  all-games 6.10% from Phase 6 is unaffected.
- **`el_find_games` served a null winner for all 330 games.** `raw_game`
  deliberately holds null, because the source schedule repeats the season champion
  in every row; the derived layer had simply never computed the replacement.
  Migration `0005_game_winner` derives it from the official final score, which all
  660 team-game lines already reconcile against. `DECISIONS.md` item 19.

Also fixed here: `ruff format --check .` failed on a committed plan document and
would have turned CI red on the next push. `docs` is now excluded from ruff for
the same reason `exploration` already was.

### Block B — incremental live-season writes. Complete.

Block B's real database gate ran on 2026-08-19 against the disposable PostgreSQL
17.6 instance, never against production. The pre-Option-A writer and the final
Option A writer each loaded E2024 (330 games, split 137/193) and E2025 (402
games, split 201/201) both in one pass and incrementally. For every relation,
row counts and primary-key-ordered content fingerprints matched; all four event
attachment columns matched separately; and each first batch was unchanged after
the second batch landed. Both fresh local builds reproduced all ten recorded
production checksums per season exactly after the session timezone was fixed to
UTC, proving the checksum definition was the same rather than merely similar.

Decision 22 implements Option A: all event references are attached on the first
insert, parent rows precede child events, and one game is one transaction. The
database measured zero `game_event` updates for both seasons, versus 529,449 for
E2024 and 668,928 for E2025 under the former writer. Controlled derived-phase
growth fell from 174,964,736 to 81,272,832 bytes for E2024 (**53.55%**) and from
219,619,328 to 99,450,880 bytes for E2025 (**54.72%**). The complete evidence and
blind spots are in `docs/INCREMENTAL_DERIVED_CONFIRMATION_RESULT.md` and
`docs/BLOCK_B_COMPLETION_REPORT.md`.

The gate proves PostgreSQL persistence semantics for two complete cached
seasons and two split points. It does not exercise Supabase RLS roles, its
pooler, production grants, arbitrary future split points, concurrent readers or
writers, crash recovery, or real E2026 payloads.

---

## Historical closeout snapshot — 2026-08-13

This section records what was true at the Phase 8 closeout. It is retained as
evidence, not as the current work queue; use the final section of this file for
current status and ordering.

The phase sequence and Block B are complete. What remains is a set of named,
open conditions and defects; Block B introduced no unresolved owner decision:

1. **The storage hot window — implemented as E2026, E2025, E2024.** Decision
   20's Conditions A and B are closed. Condition C still forbids pre-building a
   derived-only tier, and Condition D still requires re-projection if E2026's
   scheduled 380 games changes. The compacted projection leaves 72,008,225 bytes
   of headroom; Decision 22 removes the recurring attachment-update churn that
   threatened it.
2. **The Phase 6 possession residual** — 16 E2024 games quarantined as
   `possession_gate`. Five candidate causes measured and eliminated.
3. **The composite `game_event_possession_fkey`** — declared `ON DELETE SET NULL`
   across a composite key. Option A avoids the broken action in the normal
   writer, but a later owner-approved migration should still scope it to
   `possession_index`.
4. **Decision 17's condition exercised** — approved in `DECISIONS.md` and
   commit `11e3080`: any shot query including free throws starts from
   `game_event`, with `raw_shot` left-joined only to attach coordinates.
   `raw_shot` is populated with 51,193 E2024 and 64,137 E2025 rows (measured
   2026-08-22 against production) and served via `el_get_shot_data`, with free-throw
   labelling gated by migration 0007 (`docs/SHOT_DATA_TOOL_REPORT.md`).

**Published 2026-08-13.** `origin/master` and the local branch are the same
commit, so Phases 5 through 8 are on GitHub and the repository exists somewhere
other than the owner's machine. CI ran on that commit and reported
`280 passed, 61 deselected in 6.18s` with lint and format clean.

**What CI green does not cover, stated so it is not over-read.** Those 61
deselected tests are the `warehouse` and `full_season` marks, which need the
response cache and the live database — neither of which CI can reach. One of
them, `test_live_phase_4_gate`, is deliberately red and will stay red until the
hot-window decision is made. A green CI run means the database-free suite
passes; it is not a statement that every gate in this roadmap passes.

Verified at the same time: the response cache has never been committed — the
largest blob in the whole history is a 172 KB fixture — and no key material,
token or populated connection string appears in any of the 61 commits.

---

## Out of scope for this repository

Content generation, graphics, and social posting are a separate downstream
project that consumes this warehouse. Do not build them here.

---

## First prompt for a new agent

```
Read AGENTS.md, DECISIONS.md, ROADMAP.md, and the three documents in
exploration/.

Before any work, answer three questions so I can confirm the handover
landed. Answer from the documents, not from general knowledge:

  1. Name the three rules that, if broken, would corrupt data silently —
     no error, no obvious symptom. State each in your own words and say
     what the symptom would look like downstream.
  2. What do `raw` and `corrected` mean here, and which is the default
     for minutes?
  3. Why is `numberofplay` stored at all, given it must never be used
     for ordering?

Then begin the next unfinished phase as described in ROADMAP.md.
```

---

## Historical long-lead snapshot — 2026-08-10

This is the fetch estimate as it was recorded on 2026-08-10. E2025 has since
been fetched, loaded, and verified; the estimate remains useful for future
archive expansion but is not an active instruction.

**The season count is now measured.** On 2026-08-10 one schedule request per
candidate season code established that the API serves **E2003 through E2026**:
E2003–E2025 are complete (**23 seasons**), and E2026 is the 2026-27 season with
380 games scheduled and none played. Probing began at E2003, so 23 is a floor.
Full table and caveats in `DECISIONS.md` item 8.

That measurement also corrected the size unit. **Seasons are not all
E2024-sized**: E2024 is 330 games, E2025 is 402 after the expansion to 20 teams.
Across E2003–E2025 the API serves **5,950 played games** in total.

Fetching remains the slowest thing in the project, at 9.0 seconds per response
and two responses per game. E2024 (330 games) is already cached and **E2025 is
being fetched now**, about 2.0 hours. The remaining 21 seasons are 5,218 games,
or roughly **26 hours**. It depends on no schema, no test and no decision, so it
can run in the background from now.

`exploration/fetch_season.py` is the prototype that produced the current cache.
It takes the season from the `EL_SEASON` environment variable and defaults to
`E2024`, and it skips any response already on disk, so an interrupted run
resumes for free.

**The production fetcher has replaced it**: `scripts/fetch_archive.py`, over
`src/euroleague/fetch.py`. It writes exact response bytes through an atomic
rename, appends one audit line per received response to
`<cache>/fetch_log.jsonl`, holds the nine-second cadence, honours `Retry-After`,
remembers permanent 404s across restarts, and resumes from the cache. Run one
fetcher at a time; two will earn HTTP 429s.

It also fetches `Points`, which the prototype never did. Two consequences worth
knowing before the first long run:

- **A finished season's schedule is reused; an unfinished one is re-fetched.**
  A cached schedule that still lists unplayed games would otherwise hide every
  game played since it was written — silently, with no missing file to notice.
  The cost is one request per run per unfinished season. When the refreshed body
  differs, the superseded body is kept beside it under its checksum, because a
  re-fetch is an audit and never an overwrite.
- **`Points` parsed and ingested into `raw_shot`.** `raw_shot` is populated
  (51,193 E2024 rows and 64,137 E2025 rows, measured 2026-08-22 against production).
  Decision 17 was implemented and verified in `docs/SHOT_DATA_TOOL_REPORT.md`:
  `el_get_shot_data` queries `game_event` and left-joins `raw_shot` for coordinates
  (`v_shot_data`), satisfying the condition.

---

## Live Season Plan: Blocks C, D, and E (2026-08-23)

Following the compaction and incremental loader work in Blocks A and B (`docs/STORAGE_COMPACTION_REPORT.md`, `docs/E2026_LIVE_SEASON_PLAN.md`):

- **Block C — Automated Scheduled Pipeline**: Complete and verified (`docs/BLOCK_C_REPORT.md`). Scheduled fetch, incremental load, derived rebuild, and validation gates run on GitHub Actions (`.github/workflows/e2026-live.yml`).
- **Block D — Pre-season Rosters**: Complete and production-verified (`docs/PRESEASON_ROSTER_INGESTION_REPORT.md`). Migration 0012, reviewed release, exact-byte archive, 203-row zero-game E2026 load, public-role isolation, and an unchanged idempotency rerun all passed.
- **Block E — Multi-season Serving & Maintenance**: Migrations 0008-0010, truthful zero-game E2026 progress, public-view security hardening, release verification, and Decision 18 live re-measurement are complete. The re-measurement passed four factors and opened separate lineup and clutch performance decisions.

### Open Items Carried into Live Season
1. **The 16-game E2024 possession residual**: Quarantined under `possession_gate` and disclosed on every tool response.
2. **The composite `game_event_possession_fkey` constraint**: Migration 0008 is applied and rollback-probed; only nullable `possession_index` is now cleared.
3. **Storage headroom monitoring**: Verified 3-season window (E2024, E2025, E2026) within the 500 MB ceiling.
4. **Season progress activation**: Migration 0009 is applied; E2026 truthfully records 380 scheduled and 0 loaded games. E2024/E2025 remain unknown because no truthful historical load timestamp exists.
5. **E2024 archive recoverability**: 330 parsed `Points` games have no production archive entries; detection is implemented and repair remains owner-attended.
6. **Applied-source activation**: Migration 0010 is reconciled with the
   equivalent production table. It has zero rows because no historical applied
   checksum version was provable; future successful E2026 writes will create
   their own markers.
7. **Public-view security**: Complete. All seven warehouse views are security
   invokers with no `anon` or `authenticated` grants; production advisor errors
   are closed.

---

## Current verified state and ordered remaining work — 2026-08-23

Core phases 0-8 and live-season Blocks A-C are complete. Block D has completed
reconnaissance, roster ingestion, and its migration gates. Block E public-view
Orders 7a, 7b, and 7c resolved both database-execution and user-visible
MCP connection-lifecycle questions without changing Decision 18 thresholds. The
database-free suite, lint, and format were green at the last completed order.
The ten unique commits on

`origin/codex/decision-7-rebuild` are fully explained in
`docs/DECISION_7_BRANCH_RECONCILIATION.md`; remote branch deletion remains an
explicit owner action.

**Release-readiness estimate: approximately 85%.** This is a transparent
planning estimate, not a code-line metric: 80 percentage points are assigned to
the completed core phases and Blocks A-C; 20 points cover activation and Blocks
D/E, of which roughly five are already earned by roster reconnaissance,
freshness/progress implementation, and the timing harness. Historical archive
expansion and EuroCup are deliberately deferred product expansion and are not
included in that percentage.

### Small closeout completed in this session

- `rebuild_revised_game` now runs the same season/scope guards as the other
  derived writers, refuses any empty per-game derived table before deletion,
  and filters `team_season` rows by season as well as team.
- The follow-up inbox is empty, stale README/migration/timing status is corrected,
  and every remaining large item has a one-session draft below.
- No production write, branch integration, push, workflow dispatch, or live API
  sweep is part of this closeout.

### Decision 7 branch reconciliation completed

- Private cache snapshots now bind parsing and applied checksums to the same
  immutable bytes.
- `Points` is required and loaded for new E2026 games; per-game rebuilds replace
  `raw_shot` and prune only unreferenced obsolete identities.
- Migration 0010 makes failed source revisions durably pending across runs.
- All ten remote-only commits are classified in
  `docs/DECISION_7_BRANCH_RECONCILIATION.md`. No merge, push, production write,
  or branch deletion occurred.

### Production migrations and progress activation completed

- Migrations 0008-0010 are recorded in production; the pre-existing Decision 7
  table was reconciled without dropping it or inventing marker rows.
- A rollback-only live delete proved 0008 clears only `possession_index`.
- E2026 Schedule bytes matched the current archive SHA-256 exactly and produced
  the only progress row: 380 scheduled, 0 loaded. Historical seasons remain
  unknown.
- All twenty E2024/E2025 content fingerprints remained unchanged. Full evidence
  is in `docs/PRODUCTION_MIGRATIONS_AND_PROGRESS_REPORT.md`.
- The advisor exposed a separate six-view security blocker; it was not folded
  silently into this migration session and was closed in the next attended
  session.

### Public view security hardening completed

- Migration 0011 makes all seven warehouse views `security_invoker` and removes
  every `anon` and `authenticated` view privilege.
- Direct production role tests deny both public roles with PostgreSQL `42501`;
  `service_role` and the owning MCP role retain unchanged full result sets.
- All seven pre/post row counts, whole-result fingerprints, and the structural
  signature match. The six security advisor ERROR findings are gone.
- Full evidence is in `docs/PUBLIC_VIEW_SECURITY_HARDENING_REPORT.md`.

### Release and GitHub Actions verification completed

- The 45-commit release range was audited, published through PR #2, and merged
  as `133389a`; no direct `master` push occurred.
- Pull-request and post-merge CI succeeded. The offline gate reported 653 passed
  and 83 separately gated tests deselected.
- A real E2026 workflow-dispatch run completed fetch, load, and Decision 7
  settlement with all configured credentials masked.
- Full evidence and the GitHub rendered-summary visibility limit are in
  `docs/RELEASE_AND_ACTIONS_VERIFICATION_REPORT.md`.

### Ordered one-session roadmap

Each row is intentionally a separate session. Do not combine adjacent rows just
because a previous one finishes early; its gate is the next row's precondition.

| Order | Session plan | Why this order | Done when |
|---:|---|---|---|
| 1 | **Complete:** [`01-decision-7-branch-reconciliation.md`](docs/superpowers/plans/2026-08-23-01-decision-7-branch-reconciliation.md) | Establish one canonical rebuild implementation before publishing or deleting a branch. | All ten commits are explained; deletion remains an explicit owner action. |
| 2 | **Complete:** [`02-production-migrations-and-progress-backfill.md`](docs/superpowers/plans/2026-08-23-02-production-migrations-and-progress-backfill.md) | The live workflow and MCP disclosure need schema 0008/0009/0010 before activation. | All three migrations are verified; progress and applied checksums are initialized only where truthful evidence exists. |
| 3 | **Complete:** [`03a-public-view-security-hardening.md`](docs/superpowers/plans/2026-08-23-03a-public-view-security-hardening.md) | The production advisor found six security-definer views with inherited public grants; release must not preserve an unexamined Data API path. | Advisor errors are gone, public-role behavior is explicit, and MCP view results remain unchanged. |
| 4 | **Complete:** [`03-release-and-actions-verification.md`](docs/superpowers/plans/2026-08-23-03-release-and-actions-verification.md) | Publish the local commits through a review branch, never by pushing protected `master`. | PR/merge policy is satisfied and one real workflow summary is inspected. |
| 5 | **Complete:** [`04-e2024-points-archive-repair.md`](docs/superpowers/plans/2026-08-23-04-e2024-points-archive-repair.md) | Close the known recoverability hole without re-fetching source data. The cache was carried to this machine on 2026-08-25 and verified against its transport manifest; source re-fetch was never used. | All 330 objects and index rows verify and reconciliation is clean. |
| 6 | **Complete:** [`05-preseason-roster-ingestion.md`](docs/superpowers/plans/2026-08-23-05-preseason-roster-ingestion.md) | Complete Block D using the endpoint already proved by reconnaissance. | Parser, archive path, ingest, idempotency, and zero-game E2026 gate pass; migration 0012 is rehearsed on a disposable database and applied only with separate owner approval. |
| 7 | **Complete — 1 pass, 2 failures named:** [`06-decision-18-live-remeasurement.md`](docs/superpowers/plans/2026-08-23-06-decision-18-live-remeasurement.md) | Re-earn the view-performance licence against the activated multi-season schema. | Real timings are recorded; every failure is named for a separate optimisation decision. |
| 7a | **Complete:** [`2026-08-24-06a-clutch-measurement-path-decision.md`](docs/superpowers/plans/2026-08-24-06a-clutch-measurement-path-decision.md) | Clutch failed at 152.69-153.41 ms wall clock while PostgreSQL executed in 0.510-0.832 ms; attribute the gap before changing schema. | PostgreSQL execution remains the approved boundary; clutch passed at 0.599-0.810 ms with the 24 ms threshold unchanged. |
| 7b | **Complete:** [`2026-08-24-06b-lineup-on-off-performance-decision.md`](docs/superpowers/plans/2026-08-24-06b-lineup-on-off-performance-decision.md) | Lineup failed both wall-clock and server execution thresholds; use its captured plan to choose query rewrite, index, or aggregate promotion. | The one-scan rewrite preserved the canonical result and passed at 88.509 ms under the unchanged 98 ms gate. |
| 7c | **Complete:** [`2026-08-24-06c-mcp-connection-lifecycle-performance.md`](docs/superpowers/plans/2026-08-24-06c-mcp-connection-lifecycle-performance.md) | Order 7a proved that fresh connection and read-only setup dominate repeated MCP latency; the long-lived serial stdio process currently pays that cost on every tool call. | One lazy verified-read-only connection is reused, bounded reconnect tests pass, the real JSON-RPC path is measured, and the owner accepted the attended evidence without changing Decision 18. |
| 8 | [`07-e2026-opening-week-validation.md`](docs/superpowers/plans/2026-08-23-07-e2026-opening-week-validation.md) | This evidence cannot exist before games are played. Earliest start is 2026-09-24. | Initial load plus +6h/+24h/+72h/+7d settlement evidence and per-season correction safety are recorded. |
| 9 | **Complete:** [`08-possession-residual-investigation.md`](docs/superpowers/plans/2026-08-23-08-possession-residual-investigation.md) | Important quality research, but quarantine makes it non-blocking for launch. | The residual is explained or narrowed by a new falsifiable diagnostic without weakening the gate. |
| 10 | **First batch complete:** [`09-historical-archive-expansion.md`](docs/superpowers/plans/2026-08-23-09-historical-archive-expansion.md) | Long-running backfill belongs after live operations are stable. | E2023 archived and restored byte-for-byte on 2026-08-29; see `docs/HISTORICAL_ARCHIVE_E2023_REPORT.md`. Further batches run from `.github/workflows/historical-archive.yml`, one season per manual dispatch. |
| 11 | [`10-eurocup-onboarding.md`](docs/superpowers/plans/2026-08-23-10-eurocup-onboarding.md) | Decision 11 keeps EuroCup schema-ready but deferred until EuroLeague is operationally proven. | A measured pilot passes competition isolation and storage gates before any full load. |

Orders 1-4 are complete. The owner approved starting Order 6 before Order 5 on
2026-08-24, without weakening or waiving Order 5's gate, and Order 6 is complete
through reviewed production activation and an unchanged idempotency rerun.
Order 5 is now complete on its original gate, unrelaxed: the cache was carried
to this machine and verified against its transport manifest, and 330 `Points`
responses — 16,713,709 exact bytes, 51,193 coordinate rows equal to the E2024
`raw_shot` count — were uploaded, verified per object and indexed with owner
approval on 2026-08-25. Every stored index row matches the checksum inventory
recorded before the first upload, reconciliation is clean for E2024 and E2025,
a fresh restore rebuilt 991 of 991 responses byte-identically, and no warehouse
fact row changed. Evidence is in
`docs/E2024_POINTS_ARCHIVE_REPAIR_REPORT.md`.
Order 7 is complete with one initial pass and two named failures. Order 7a resolved clutch as a measurement-
boundary mismatch without changing its threshold or schema. Order 7b resolved
the remaining performance failure at the database-execution boundary with a
one-scan query rewrite, without an index, table, or threshold change. Order 7c
resolved the user-visible connection lifecycle, demonstrating a 61.9% to 62.4%
same-run reduction in repeated MCP tool calls with 100% deterministic response
equality across all 35 measured calls and no call-six preparation spike observed.
Order 8 is date-gated operational proof (earliest start 2026-09-24). Order 9 is complete: every unit of every game's
possession-count difference is now located in the event stream, the completeness
identity is asserted per game across both cached seasons, and the decomposition
shows 11 of the 31 failing games carry no anomalous site at all. One real defect
was found and fixed - an and-one bonus taken by a substitute counted as a second
possession - moving E2024 from 314 to 316 games inside the gate with no game
regressing in either season. The gate, its tolerance and every quarantine are
unchanged; evidence is in `docs/POSSESSION_RESIDUAL_REPORT.md`. Orders 10-11 are post-release expansion and
may be postponed without weakening the E2026 launch claim.

**Order 9 production reconciliation, 2026-08-26.** A read-only audit found
E2024 already at the corrected 47,829-possession state, including the two
quarantine removals. E2025 alone remained stale at 59,483: game 344 held one
extra derived possession. The owner approved a targeted derived-only rebuild
for that game and explicitly kept the 11 structural no-anomaly games under the
existing conservative possession gate. The live transaction remains subject
to the immediately-before-write approval rule; the owner gave that separate
approval and the transaction completed on 2026-08-26. E2025 game 344 now has
160 possessions, the season has 59,482, and all protected fingerprints and
gates passed. Order 9 is fully reconciled in production.

**Person-game link backfill, 2026-08-29.** Decision 27's observed bridge was
built for both loaded seasons: 17,333 links across 732 games, 461 person codes
against 461 player ids in a perfect bijection with zero cross-game
contradictions, and a `P`-prefix agreement rate of 1.000000 in both seasons. The
convention remains an observation, not a mechanism; Decision 24's prohibition is
unchanged. Seventy of 17,403 box score rows are unlinked, of which twelve belong
to players who took the floor and are unexplained. Evidence is in
`docs/PERSON_GAME_LINK_BACKFILL_REPORT.md`.

---

## Phase 2: from private pilot to public. Written 2026-08-29.

**Phase 1 is complete.** The hosted server runs, access control was observed in
both directions on 2026-08-29 — an allowlisted person admitted, a
non-allowlisted person refused — and the connector uses one shared public client
that does not consume the tenant's application cap. Eight to ten testers can be
invited today.

Two things were deliberately taken off the path:

- **Order 8 is frozen until 2026-09-24.** Live-season evidence cannot exist
  before games are played. It is not a blocker for anything below; it is a date.
- **Compaction is retired as a precondition.** Decision 30. The storage watch
  reports the headroom every night instead.

### The remaining work, in an order that has a reason

The order matters, and it is not the order of difficulty. **P2-4 removes the
only control currently deciding who reaches the warehouse, so nothing about it
is safe until P2-2 and P2-3 are settled.**

| | Work | Who | Why it sits here |
|---|---|---|---|
| **P2-1 — DONE 2026-08-30** | **Load test the hosted server** | Owner, with a script | 40 concurrent POSTs completed with 120/120 successes at that level and p95 3,205.620 ms. See `docs/HOSTED_LOAD_TEST_2026-08-30.md`; persistent SDK GET streams consume Fly request slots, so this is not a 40-connected-user claim. |
| **P2-2** | **Decide the Auth0 tenant** | Owner | `dev-ew0k6i4pmarjvgkn` is labelled DEVELOPMENT and Auth0 does not intend those tenants to carry production traffic. Moving means a new issuer URL, which means redeploying the server with new environment values and rebuilding the API, the application and the Action — essentially redoing the 2026-08-29 Auth0 work. **That cost is the same before or after a public opening; the risk is not.** Decide before. |
| **P2-3** | **Make the server check the token's audience** | Code | Read on 2026-08-29: `src/euroleague/mcp/http_app.py` validates a bearer token against the tenant's JWKS, introspection endpoint or userinfo, but passes `verify_aud=False` and enforces no scope. **Any valid token from that tenant is accepted.** With an allowlist in front, that is tolerable. Without one, on a tenant anyone can sign up to, it means any token issued by that tenant for any purpose opens the warehouse. This is the reason P2-4 cannot come first. |
| **P2-4** | **Retire the invite-only Action** | Owner | It must go, or a public server admits nobody. When it goes, the controls that remain are the ones already built: the per-subject daily row budget (goal 032) and the sweep refusal (goal 033), plus Fly's concurrency limits. Those bound *how much* anyone can take; they do not bound *who*. That is the intended trade of going public, and it should be made knowingly. |
| **P2-5** | **Announce** | Owner | Everything above settled, and the README describing what the server actually is. |

### What Phase 2 does not require

- **Not compaction.** Decision 30.
- **Not the historical archive.** Seasons E2003–E2022 can keep arriving one
  manual dispatch at a time through `.github/workflows/historical-archive.yml`
  and are independent of everything above. They also do not enter the hot window,
  so they change nothing about what the MCP server can answer.
- **Not Order 8.** A public opening before 2026-09-24 is possible; it simply
  cannot claim live-season evidence it does not have yet.


---

## Phase 2, revised. Written 2026-08-30.

Phase 2 above was written on 2026-08-29 for **one public server**. Two decisions
taken on 2026-08-30 change what it aims at, and one of them moves an item that
was previously a blocker.

### The two decisions

**The product is a portfolio project with its hosting costs covered, not a
product for sale.** The owner funds the roughly two dollars a month the public
Fly application costs. The roughly thirty-five dollars a month that all twenty
seasons need in Supabase is to come from one collaborator, not from public
sales. There is no billing to build, no paid tier, and no per-season entitlement
code. An earlier proposal that day - a free public MCP and a paid private one in
a second repository - was rejected on its premise.

**The split is a deployment split, not a code split.** One public repository,
one code base, two deployments that differ only in environment values:

```
                    one repository (public, MIT)
                              |
          same code, different environment values
          +-------------------+-------------------+
          |                                       |
   PUBLIC deployment                       PRIVATE deployment
   Fly app, owner-funded                   Fly app
   Supabase FREE project                   Supabase paid project
   E2024-E2026 hot window                  every season, and the archive
   open, bounded by the row budget         the invite-only Action stays
                                           two people: owner, collaborator
```

**Two Supabase projects rather than one database with two roles.** A single paid
database with a restricted read-only role is the better engineering, and it was
rejected for a reason that outranks that: the collaborator may stop paying, and
the public service must survive it. Two projects means the free one is untouched
when the paid one lapses. It also keeps public traffic away from the database
holding twenty seasons.

**What this costs, stated now rather than discovered later:**

- The nightly E2026 job must load into **both** hot windows. That is a new
  failure mode - one load succeeds, the other does not - and it needs handling,
  not a second command bolted onto the first.
- The archive of roughly 1.2 GB can only live on the paid project, because
  Supabase's free Storage allowance is 1 GB. **The archive is therefore the most
  valuable asset in the project, sitting on a subscription somebody else pays
  for**, and it represents about fifty hours of fetching that cannot be repeated
  quickly. It needs a second home. Cloudflare R2 (10 GB free, S3-compatible, no
  egress charge) fits the existing checksum-addressed storage code most closely.
- Whether E2024-E2026 fits the free 500 MB was not measured here. The owner
  reports that it fits today and expects pressure once E2026 games are played,
  which is at least two to three months away. Dropping the oldest seasons from
  the **hot window** is the lever if it stops fitting; that is a different
  decision from dropping them from the archive.

### What this changes about Phase 2 above

**P2-4 shrinks.** "Retire the invite-only Action" was written when there was one
server. The Action now stays on the private deployment, which has two users, and
is removed from the public deployment only. That is less work than P2-4
described, not more.

**P2-2 is downgraded from a blocker to a re-examination, and the owner decides.**
The sentence that made the Auth0 tenant urgent was that a development tenant
cannot carry public traffic. With the deployments split, the tenant fronting the
private server serves two people, which a development tenant covers comfortably,
and the public deployment holds three seasons in a free database.

The cost of moving is unchanged, and it is not money: Auth0's environment tag
and a second tenant are both free, and the free plan's user allowance is far
beyond this project's scale. What a move costs is **rework that no part of this
repository can reproduce.** A new tenant means a new issuer URL, and that URL is
baked into the server's environment, into the discovery document it publishes,
and into every connector anybody has already added - all of which break at once.
The API, its `read:warehouse` permission, the Native/PKCE application, the
third-party default permission setting and the post-login Action are five
dashboard objects, each of which took a debugging round on 2026-08-29, and none
of which exists as a migration, a test or a diff. **That is what makes it the
riskiest item: not damage, but an unreproducible rebuild whose worst failure is
silent.** A connector that authenticates while the allowlist Action is not
attached looks exactly like a working one.

**P2-3 keeps its priority and changes its justification.** It is no longer
argued for as the thing standing between the warehouse and the public. It is the
precondition of removing the allowlist from the public deployment: the moment
that Action goes, an unaudienced token is the only thing left, and the server
accepts any token its tenant issued for any purpose.

### The work, in order, with what is already done

Done on 2026-08-30, on branch `security/workflow-hardening`:

- `zizmor` run against `.github/workflows/` for the first time. 28 findings, now
  0. The measurement preceded the fix deliberately, and it earned its keep:
  `artipacked` - eight checkouts leaving a credential in the workspace - was not
  on the hand-written list of things to fix.
- Every action reference pinned to a commit SHA, `setup-flyctl@master` among
  them. That one shared a workflow with `FLY_API_TOKEN`.
- Production credentials removed from every job-level `env` block, so
  `pip install` no longer runs package setup code holding the database password.
- Season codes passed through `env` rather than interpolated into shell
  commands, and `validate_season_code()` added at the script entry points. That
  value also reaches an API URL, where a `/` or a `?` changes the request.
- `tests/test_workflow_security.py`: four tests locking all of the above.

Remaining, in the order it should be worked:

| | Work | Why it sits here | Estimate |
|---|---|---|---|
| **R-1** | Protect `master` with a ruleset | The last piece of the hardening above, and the rule CLAUDE.md already states in prose | 15 min |
| **R-2** | Run the restore gate for E2020 and E2021 | Both are archived and neither has passed a gate. E2020's gate failed on a Supabase read timeout on 2026-08-30 and the chain moved on, because the chooser tests whether a season is *fetched*, not whether it *verified* | 20 min |
| **R-3** | Dependabot, CodeQL, Dependency Review | All free on a public repository. Dependency Review matters most here: it is the only mechanism that enforces CLAUDE.md's GPLv3 prohibition, which is otherwise a sentence | 2 h |
| **R-4** | zizmor in CI, Trivy weekly and non-blocking, Docker digest pin | zizmor is the scanner that produced the list above. Trivy is scheduled only and deliberately not a required check: `python:3.14-slim` carries Debian CVEs that cannot be fixed here, and a required check nobody can pass is a check that gets disabled | 2 h |
| **R-5** | Confirm Secret Scanning and Push Protection | Free and on by default for public repositories. A verification, not an installation | 10 min |
| **R-6** | **P2-3**: verify the token audience and enforce a scope | Precondition for R-9 | half a day |
| **R-7 — DONE** | **P2-1**: load test the hosted server | 40 concurrent POSTs measured successfully; the separate SDK session-slot limit is recorded, not guessed | completed 2026-08-30 |
| **R-8** | Build the split: second Supabase project, second Fly app, nightly job loading both | The architecture above | 1 day |
| **R-9** | Remove the allowlist from the public deployment only | After R-6 | 1 h |
| **R-10** | A second home for the archive | Fifty hours of fetching currently has one copy | half a day |
| **R-11** | README, announcement, video | Owner | owner |

**P2-2 sits outside this table deliberately.** It is a decision, not a queued
task, and the paragraph above is the case for re-examining it rather than doing
it.

### Dropped, and why

- **A `docs/` exposure review.** `docs/AUTH0_CONFIGURATION.md` describes the
  server's own unfixed weaknesses in a public repository, and moving it was
  proposed. The owner judged the risk not worth the work, and that judgement
  holds at this scale: the exposure is proportional to what is behind the door,
  and behind it are basketball statistics behind an invite list. Removing the
  file would also not undo publication - the repository is public and its
  history keeps it. R-6 is the fix that actually retires the concern.
- **A legal review of commercial use.** The owner is researching this separately
  and will report the outcome. Nothing above depends on it, because nothing
  above sells anything.

### What none of this establishes

The archive chain is unattended and running: E2019 back to E2003, roughly five
seasons a day measured across four completed seasons, expected to finish about
2026-09-02. That estimate is drawn from seasons larger than the ones remaining,
and it is a projection rather than a measurement.

**Nothing in this section verifies the archive's contents.** The restore gate
proves the bytes come back unchanged; it does not prove they are what the API
would serve today, and R-2 exists because two seasons have not had even that.

### Progress, 2026-08-30, later the same day

Still on branch `security/workflow-hardening`, deliberately unmerged: a merge is
a production release, and the work below belongs in one release rather than
five.

**R-1 done.** Repository ruleset `master is a deploy trigger`, active on the
default branch: pull request required (zero approvals, so a solo maintainer is
not blocked), the `test` check required, force pushes and deletion refused. The
configuration is verified through GitHub's rules endpoint. **Enforcement is
not independently demonstrated**, and deliberately so: the only way to prove a
direct push is refused is to attempt one, which - if the rule failed - would put
commits on master without review and trigger a deploy.

**R-5 done, and it was a verification rather than an installation**, as
predicted. Secret Scanning and Push Protection were already enabled; public
repositories get both by default. Two adjacent settings were found off:
`dependabot_security_updates` was disabled and is now enabled (vulnerability
alerts had to be turned on first, which was also off).
`secret_scanning_validity_checks` was requested and **did not take effect** -
the API accepted the call and the setting still reads disabled. Unresolved.
`secret_scanning_non_provider_patterns` is left off on purpose: it widens
detection at a cost in false positives that a solo maintainer pays personally.

**R-3 done.** `.github/dependabot.yml` covers pip, GitHub Actions and Docker,
weekly, with development dependencies grouped so ruff and pytest churn arrives
as one pull request. `.github/workflows/dependency-review.yml` refuses a pull
request that introduces a high-severity vulnerability or a GPL/AGPL dependency,
which is the first time CLAUDE.md's GPLv3 prohibition exists as a mechanism
instead of a sentence. LGPL is deliberately not denied.

**CodeQL enabled** through default setup, `security-extended`, for Python and
GitHub Actions.

**R-4 done.** zizmor now runs inside `ci.yml` rather than as a workflow of its
own, so it is covered by the one required status check and adds no new moving
part. Trivy is a separate weekly workflow, failing on CRITICAL and reporting
HIGH without failing, and is **not** a required check - a base image carries
CVEs that cannot be fixed from this repository, and a gate nobody can pass is a
gate that gets switched off. The Docker base image is digest-pinned, which
Dependabot now maintains.

**A finding that was not on the list, and its fix changed twice.** The deploy
did not wait for CI. `fly-deploy.yml` triggered on `push` to master, so it and
`CI` started at the same instant - the merge of pull request #25 stamps both
runs `2026-08-29T21:10:31Z` - and a merge whose tests failed would have
deployed anyway, because nothing asked. The first fix was a `workflow_run`
trigger; zizmor rejected it as a dangerous trigger, correctly, since
`workflow_run` runs with secrets in the base repository's context. The deploy is
now a `deploy` job inside `ci.yml` with `needs: test`. **That is a mechanism
rather than a guard**: the earlier shape was configured not to deploy early,
this one cannot.

### A staging environment: agreed in principle, sequenced deliberately

Raised by the owner on 2026-08-30. It is worth having, and it splits into three
pieces that do not have the same value.

- **Gating the deploy on the tests was the urgent part**, and it is done above.
  Nothing about staging would have closed that hole, because the hole was that
  the release asked no questions at all.
- **A staging Fly application is worth building**, as `R-8a`. Nothing currently
  checks, before the live server restarts, that the container builds, boots,
  reads its environment, publishes the expected tool list and answers a real
  JSON-RPC call. Its cost is close to zero if it sets `auto_stop_machines = 'on'`
  - at that roadmap decision's date, production ran always-on at about $2.02
  a month because
  `docs/MCP_CONNECTION_LIFECYCLE_REPORT.md` measured 1,612 ms for a cold first
  call against 606 ms warm, and that measurement is about production latency,
  not about staging. Decision 86 (2026-10-03) supersedes the always-on policy:
  production is now configured to suspend when idle. The historical stdio
  numbers do not measure Fly sleep/wake latency or current prices.
- **A permanently hosted staging database is not worth it yet.** Supabase's free
  plan caps active projects, and the risk it would address - a migration
  behaving differently against real data - already has a rule in CLAUDE.md
  written after production and the repository disagreed twice in two days:
  rehearse on a disposable database, then apply. Making that database permanent
  buys little and probably costs money.

**`R-8a` is sequenced with `R-8`, not before it.** The two-deployment
architecture already creates a second Fly application and a second Supabase
project. Building staging first means laying the same plumbing twice.

### R-6 done in code, and proven against a real token. 2026-08-30.

The server now refuses a token that does not name it. `acceptable_claims()` in
`src/euroleague/mcp/http_app.py` is one function, used by both surviving
verification paths, checking three things: the audience names this resource, the
issuer is the configured authority, and the token carries the required scope.
Trailing slashes are normalised on both sides, because Auth0 publishes `iss`
with one and the configured value conventionally has none - and the comparison
is an equality after normalisation, never a prefix match.

**A second hole was found while fixing the first, and it was not in the
documentation.** `verify_token` had a third path: if JWKS and introspection both
declined, it called the tenant's `/userinfo` endpoint and, on any response
carrying a `sub`, granted access **with no scopes at all**. A userinfo response
proves the bearer exists in the tenant. It carries no audience, so it cannot
show which API the token was minted for, and this tenant lets any client
register itself. Every check added here would have been bypassable by anyone
holding any token from the tenant. The path is deleted, and
`test_the_userinfo_fallback_is_gone` asserts no GET follows a refused
introspection.

`MCP_REQUIRED_SCOPE` defaults to an empty string and can be set to
`read:warehouse` to enable the scope check. The audience and issuer checks have
no such switch: the scope is defence in depth, the audience is the mechanism.

Rejections are now logged with their reason, at WARNING, on the server. The
reason is deliberately absent from the 401 the client receives - an error that
separates "wrong audience" from "unknown token" tells a caller which half of
their guess was right - and it contains no claim values, so a rejection does not
become a second disclosure.

**The owed observation was completed during R-7.** A real process-local Auth0
token named the MCP resource in its audience, carried the configured issuer and
included `read:warehouse`; the deployed rule accepted it. No bearer or personal
claim was written. Enabling the scope requirement is now known not to reject
this token shape, but remains a separate owner-approved production change.

Two pre-existing tests were updated rather than removed. Their introspection
fixtures had no `aud`, which described a token this server should never have
accepted; a real response for an API access token carries one. Coverage went up
rather than sideways: `test_app_with_auth_refuses_a_token_minted_for_another_api`
now asserts the refusal end to end, at the point a client meets it.

### R-6's lockout risk, resolved without a token. 2026-08-30.

The open question was whether a real access token satisfies the new rule, and
the answer turned out to be already recorded rather than needing an experiment.

- **Audience: it matches.** The server publishes
  `resource = https://euroleague-analytics-mcp.fly.dev/mcp`. Auth0's own error
  message, pasted into `docs/AUTH0_CONFIGURATION.md` on 2026-08-29, names the
  API by its identifier: *is not authorized to access resource server
  "https://euroleague-analytics-mcp.fly.dev/mcp"*. The identifier is what lands
  in `aud`, and it is the same string.
- **Issuer: it matches after normalisation.** Auth0 issues `iss` with a trailing
  slash and the configured issuer has none. `_same_url` was written for exactly
  this and a test covers both spellings.
- **Scope: observed, after the default changed because it was unknown.**
  `MCP_REQUIRED_SCOPE` now defaults to empty. `docs/AUTH0_CONFIGURATION.md`
  first proved only that `read:warehouse` **exists** on the API. R-7 then
  observed a real issued token carrying it. Defaulting to a check nothing had
  been observed to pass would still have been the wrong order; the evidence now
  supports an owner-approved production change if wanted.

The distinction between a permission existing and a token carrying it was
glossed when the default was first chosen. It is written down here because it is
the kind of reasoning error that reads as thoroughness.

**What this still does not establish.** One accepted token proves this tenant's
current token shape, not future Auth0 configuration, Production-tenant rate
limits or every client flow. `scripts/check_hosted_token.py` remains the
credential-safe way to repeat the observation after an Auth0 change.

### R-7 measured: 40 POSTs pass, 40 connected SDK clients are not implied. 2026-08-30.

The attended production run completed three waves at concurrency 1, 5, 10, 20
and 40. At 40, all 120 calls succeeded with p50 2,209.658 ms, p95 3,205.620 ms
and max 3,301.359 ms. All response fingerprints matched. The full method,
credential-free raw result and limits are in
`docs/HOSTED_LOAD_TEST_2026-08-30.md`.

The test also found a separate transport ceiling. The installed MCP SDK opens a
persistent GET/SSE request for every initialized session, and Fly counts those
against `hard_limit = 40`. A corrected SDK harness timed out while preparing the
session after 30 were ready, before database work. The successful concurrency
run therefore used independent protocol sessions without the optional GET
stream, so its 40 slots represented 40 tool POSTs. R-8 must account for this
before describing public capacity in users rather than requests.

---

## Phase 2, re-sequenced around the launch. Written 2026-08-30, late.

`DECISIONS.md` item 37 corrects the premise the previous sequence rested on. The
archive is about 118 MB as stored, not 1.2 GB, so it fits the free Storage quota
with roughly eight times the headroom. R-10 stops being a capacity blocker, and
R-8 stops being a precondition for anything the public sees.

### What the goal actually is

The full-archive offering needs a sponsor. A sponsor needs to see a working
thing. So the free offering is not a lesser version of the product - it is the
argument for funding the other one, and it is the only artefact that does any
persuading. Everything below is ordered by that.

### The launch is not blocked by the technical queue

The deployment running today already sits on the free Supabase project with
E2024 and E2025 loaded and E2026 filling nightly. That is the free offering,
complete, with an invite list in front of it.

| | Work | Who | Blocks the launch? |
|---|---|---|---|
| **R-9** | Switch off the invite-only Auth0 Action | owner, ~1 h | **Yes. It is the only one.** |
| R-8 | Second Supabase project, second Fly app | after a sponsor | No |
| R-8a | Staging Fly app | with R-8 | No |
| R-10 | A second home for the archive | insurance, own merits | No |

R-8 and R-9 have swapped their dependency. R-9 used to require R-8 because there
was no public deployment to open; there is one, and it is the deployment we have.

### When to launch

**2026-09-16**, two days before the SuperCup. Owner's decision, 2026-09-02,
replacing the reasoning below. `DECISIONS.md` item 45 records it.

This section previously argued for *a few days after 2026-09-24*, and the
argument is kept rather than deleted, because the decision was taken against it
rather than in ignorance of it:

- *Before the first game there is no basketball audience and nothing to show but
  historical seasons. It is the coldest possible version of "look, this
  works".* — This is the half that changed. 09-16 is not a cold week: it is two
  days before the first EuroLeague competition of the season, and the SuperCup
  supplies the audience the old reasoning said only the regular season could.
- *On it, the nightly pipeline meets real live data for the first time. Being at
  maximum visibility on the night of maximum fragility is a choice, not an
  accident.* — This still holds and is now accepted knowingly. What blunts it:
  every response filters `excluded_by_default`, so a game that fails its
  invariants is quarantined rather than served wrong, and nothing the launch
  claims comes from the live pipeline.
- *A few days after it, the pipeline has proved itself on two or three real game
  nights and the demonstration becomes "here is last night's game, with its
  possessions reconstructed from the play-by-play".* — Still true, and still
  worth doing. It moves from precondition to follow-up: `docs/LAUNCH_PLAN_2026.md`
  puts it in the 09-25 to 09-27 window as a second wave.

### Launch work, and when each piece can start

| Piece | Depends on | Can start |
|---|---|---|
| Website copy, tweet thread, README, sponsor one-pager | nothing | now |
| Free test account flow for a prospective sponsor | the invite list, which already exists | now |
| Short videos | a working public deployment, so R-9 | after R-9, now 2026-09-12 |
| The launch itself | the above, and the material being ready | 2026-09-16 |
| The "here is last night's game" post | two or three clean live game nights | after 2026-09-24, as a follow-up rather than a gate |

#### Priority reset, 2026-09-01

The technical launch queue is now substantially complete. R-12 and R-13 are
complete, R-14's pre-live implementation is complete, and the remaining
operational items are short or date-gated. The historical archive chain may
continue independently; it does not prevent launch-material preparation.

The principal work before launch is therefore the **launch package itself**:

1. rebuild the public website from scratch around the finished product and its
   evidence;
2. rewrite and cut the announcement thread into the final launch narrative;
3. prepare the video storyboards, query scripts, shot list and recording setup
   before the live-season window, then capture the final footage after the
   public flow and live data are ready.

**Repository boundary, recorded 2026-09-02.** Video production, micro-clips,
social cards, original audio, bundled fonts and their licences now live in the
separate
[`euroleague-analytics-launch`](https://github.com/egemeny13/euroleague-analytics-launch)
repository. The deployable website remains here under `site/`; the final thread,
launch narrative, schedule and evidence-backed claims also remain here so their
tests travel with the warehouse evidence. Do not merge the superseded
`create_launch_video_remotion` worktree into this repository. See Decision 47.

The launch repository's `BRIEF.md` is creative working material, not schedule or
product authority. It still names the former approximately 2026-09-27 launch
window; Decision 45 and `docs/LAUNCH_PLAN_2026.md` set 2026-09-16.

The existing `site/` package, `docs/LAUNCH_COPY.md` and related R-11 material are
useful drafts, not accepted final launch assets. The earlier private showcase at
<https://euroleague-mcp-showcase.vercel.app/> is reference material only and is
not the production website or a base that must be preserved. The website may be
rethought from zero.

This entry records priority and scope only. It does not authorize a website
implementation, a deployment, publication of the thread, or video recording.
Those begin as separately attended work. The writing and pre-production are the
long pole, and they wait on no code.

### The historical archive is finished, at E2007. 2026-09-03.

The unattended chain archived **E2021 back to E2007** and stopped. It stopped
because the API stops: E2006 and every older season answer all three game
endpoints with HTTP 200 and zero bytes, measured at five gamecodes per season on
2026-09-03, while E2007 answered 12-13 KB every time. Their schedules still list
played games, which is why Decision 8 read history as reaching E2003.

Three changes followed, recorded as Decision 52: the season list ends at E2007,
an empty body is a failed target rather than something cached and archived, and
the chain's schedule is removed per Decision 31. Nothing about the launch or the
warehouse depends on the four unreachable seasons.

**Archived is not loaded.** E2007-E2021 sit in the immutable archive; the
warehouse still holds E2024, E2025 and the filling E2026. That gap is R-12.

### R-12: load the historical seasons into a warehouse. Rehearsed and measured 2026-08-31.

**Archiving a season does not make it queryable.** The chain stores exact response
bytes with their checksums; the warehouse is built by a separate parse, derive and
load. E2024 and E2025 are loaded. The other twenty-one seasons are archived and
not loaded.

The rehearsal was executed on **2026-08-31** against representative season **`E2023`**
(331 played games, 994 checksum-verified files) on a real disposable PostgreSQL database
(`euroleague_test:5433`) using the repeatable engine in `src/euroleague/historical_rehearsal.py`
and `scripts/rehearse_historical_warehouse.py`.
The complete evidence report is in [`docs/HISTORICAL_WAREHOUSE_REHEARSAL_REPORT.md`](./docs/HISTORICAL_WAREHOUSE_REHEARSAL_REPORT.md)
and [`docs/evidence/historical_rehearsal_E2023.json`](./docs/evidence/historical_rehearsal_E2023.json).

**The three questions are now answered with measured evidence and bounded estimates:**

1. **How long one historical season takes**:
   - One measured end-to-end local build and database load took **77.04 seconds** for 331 games (raw ingest: 18.50s, derived load: 43.06s).
   - A straight per-game extrapolation across 5,950 games is **~23.1 minutes**. This is an estimate, not a measured 23-season run.
2. **What share of its games the gates exclude**:
   - **7.55 %** (25 of 331 games quarantined by default), close to E2024 (7.27%) and E2025 (7.21%). This measures E2023 only and does not establish the rate for earlier seasons.
   - Breakdown: 16 `possession_gate`, 8 `off_court_attribution`, 2 `minutes_mismatch`, 1 `substitution_state`.
3. **What twenty-three seasons cost in the database (`pg_total_relation_size`)**:
   - One season (`E2023`) measures **114,368,512 bytes** across all 23 physical tables and indexes, including empty fixed-overhead tables (**345,524 bytes/game**).
   - Linear E2023-density estimate for the 1,112-game hot-window shape: **384,222,916 bytes**. This does not replace the existing production capacity gate.
   - Linear E2023-density estimate for 5,950 historical games: **2,055,869,022 bytes (~2.06 GB)**, about 4.33 times the usable free-tier budget.
   - **Conclusion**: the E2023 rehearsal strongly supports the paid/dedicated full-history architecture. Only E2023 was physically measured; a 23-season database was not.

### R-13: Auth0 production readiness. Complete 2026-08-31.

**Completed on 2026-08-31.** All required items were executed, verified, and transitioned to Production:

1. **Google OAuth developer keys retired**: Replaced with the project's own Google
   OAuth 2.0 Client credentials on the `google-oauth2` connection.
2. **Identity continuity verified**: Test login verified Google profile data and
   allowlist match with `email_verified: true`.
3. **Custom domain active**: `auth.egemenyucelen.me` configured in Auth0 with
   Auth0-managed SSL; Cloudflare DNS CNAME pointing to
   `dev-ew0k6i4pmarjvgkn-cd-fdgixg6xdbavegzd.edge.tenants.us.auth0.com` (DNS Only).
   Google redirect URIs updated to include `https://auth.egemenyucelen.me/login/callback`.
4. **Tenant support metadata**: Support URL (`https://egemenyucelen.me`) and Support
   Email configured.
5. **Introspection callback cleaned**: Vestigial `http://localhost` callback removed
   from `EuroLeague MCP Introspection`.
6. **Environment Tag set to Production**: Tenant Environment Tag switched from `Development`
   to `Production` in Auth0 Dashboard -> Settings -> General.
7. **Production readiness check reviewed**: Core readiness items (developer keys, custom
   domain, support metadata, localhost callback) passed. Non-applicable enterprise items
   (custom email provider, branded email templates, MFA) confirmed non-blocking for a
   social-only architecture without database authentication or outbound email triggers.
8. **Fly.io redeploy and token gate**: Fly secrets updated (`MCP_ISSUER_URL="https://auth.egemenyucelen.me"`,
   `MCP_INTROSPECTION_URL="https://auth.egemenyucelen.me/oauth/token/introspect"`),
   discovery endpoint verified, and `scripts/check_hosted_token.py` passed with
   a real token minted by `https://auth.egemenyucelen.me/oauth/token`.

Full record and evidence: `docs/AUTH0_CONFIGURATION.md`. R-9 is unblocked.

### R-14: parameterise the competition code. Pre-live work complete (Decisions 39 & 40); 2026-09-18 live rehearsal remaining.

`exploration/SUPERCUP_RECON.md` establishes that the API publishes `SC` as a
competition code, that `SC2026` is a real season with two semi-finals on
2026-09-18, and that the v1 game endpoints are competition-agnostic - proved by
pulling a full play-by-play payload for a played EuroCup game, `U2025`.

**Pre-live implementation complete (Decisions 39 & 40):**
1. **Fetch layer & URL builders (Decision 39):** `validate_season_code()` accepts
   exactly `E####`, `U####`, and `SC####` season codes. All v2 URL builders
   (`_schedule_url`, `_roster_url`, `_game_stats_url`) dynamically derive the
   competition path (`E`, `U`, or `SC`) from the season code, while v1 URL
   query behaviour is preserved and invalid/unsafe inputs remain strictly rejected.
2. **Live pipeline & derived layers:** Fixed the `season_code[0]` assumption in
   `record_season_progress` using `competition_for_season_code(season_code)` so
   `SC2026` propagates as `SC`, `U2025` as `U`, and `E2026` as `E`.
3. **Real non-E fixture verification:** Captured exact-byte public API response
   bodies (`Boxscore`, `PlaybyPlay`, `Points`, `GameStats`) for played EuroCup game `U2025`
   game 1 (`tests/fixtures/games/U2025/`) along with a curated single-game `schedule.json`
   fixture, and proved that the full offline pipeline (cache -> parse -> validate ->
   derived -> lineups -> stints -> possessions -> game quality) executes with zero
   attribution issues, zero on-court violations, and clean game quality on real non-EuroLeague data.
4. **Three-game storage projection (Decision 40):** Priced a 3-game SuperCup tournament
   (2 semi-finals + 1 final) at ~1.09 MB Postgres storage (upper-bound ~1.20 MB) and
   12 per-game gzip objects plus the Schedule object (13 objects in total, estimated ~0.4 MB)
   in Storage. Consumes only ~1.5% of the 72.0 MB / 14.40% measured free-tier headroom,
   safely preserving Decision 20's hot window.
5. **CLI & manual rehearsal workflow (Decision 40):** Parameterised `scripts/fetch_archive.py`
   and `scripts/live_pipeline.py` to support `SC2026` for `--live`. Added the manual-only
   `.github/workflows/supercup-rehearsal.yml` workflow (`workflow_dispatch` only, no
   scheduled cron), sharing the `e2026-live-fetcher` concurrency group with `e2026-live.yml`
   to prevent concurrent writes to production database and archive. Kept settlement rechecks
   in `scripts/settlement_recheck.py` strictly `E2026`-only and preserved the existing
   scheduled `e2026-live.yml` workflow.

**What it buys.** A dress rehearsal on live data six days before the season
opener, while nothing is public. And the only possession-level reconstruction of
the first SuperCup ever played, which is a reason for the launch to be
interesting rather than merely available. The same change opens EuroCup later,
which this project has always intended.

**Remaining date-gated work for 2026-09-18.** All code, tooling, and offline verification
are ready. The only remaining work is the date-gated live rehearsal on 2026-09-18:
manually triggering `.github/workflows/supercup-rehearsal.yml` once the semi-final
games are played, verifying live response payloads from the upstream API, and reviewing
derived game quality records.

**Why it expires.** Undone by 2026-09-17, this item stops being worth anything:
the games are played on the 18th and the rehearsal is gone. It is not deferred
work, it is dated work.

### What none of this establishes

The launch date is a judgement about attention and risk, not a measurement. The
claim that the free offering persuades anybody is untested - no one outside the
invite list has used this server. Free-tier egress under public traffic is
unmeasured, and R-7's forty-request result was measured against a server with a
handful of users, not against a launch.

## Phase 9: external judgement. Opened 2026-09-06.

Phases 1 to 8 are closed, the goal queue is empty, and `docs/SCOPE.md` freezes
version 1 at 17 tools (Decision 65, amended to fourteen by Decision 79).
Every remaining gate in this file has
been a test of the code. This one is a test of the product, and it is the only
phase left.

**The question.** `CONTEXT.md` puts one goal first: would a club's analytics
staff respect this. Nothing so far measures that. The invite list is friends,
the evaluations are the owner's own questions, and the launch thread has not
gone out.

**The work.**

1. Three readers who know the game and read box scores for a living, or close
   to it. Not friends of the project. Each gets the hosted server, the
   `README.md` connection steps, and one sentence: "ask it what you would ask a
   club analyst, and tell me where it is wrong, incomplete, or missing."
2. Every answer they get is checked against the warehouse and against
   euroleague.net, and each remark is sorted with `docs/SCOPE.md`: **wrong**
   becomes a bug goal, **missing** becomes a goal only if it is in the first
   list's spirit, **left out on purpose** gets the reason sent back, and the
   reader's reaction to that reason is recorded too.
3. The 2026-09-18 live rehearsal (R-14) is the first live measurement inside
   this phase, not a separate item.

**Gate.** Three written reports in `docs/evidence/`, each remark sorted, each
"wrong" fixed or recorded as a known limit in `docs/SCOPE.md`. A remark that
cannot be sorted is a defect in `docs/SCOPE.md`, not in the reader.

**What this gate would fail to detect.** Three readers are not a market; they
can say the numbers are right and the tool is usable, not that anyone will
choose it. That claim stays unmade, as the previous section says.

**What fills the queue.** Only what Phase 9 returns. The public API still has
season totals and standings, measured in `exploration/SEASON_ENDPOINT_PROBE.md`;
they are not a backlog, they are the second list in `docs/SCOPE.md`.

## Hot window space work: Tiers A, B and D. 2026-09-07.

The owner asked whether the hot database could shrink without losing an MCP
feature or a gate proof, then approved three of the four tiers in
`docs/superpowers/plans/2026-09-06-hot-window-space-plan.md`. Each is its own
migration, rehearsed on a disposable PostgreSQL 17.11 loaded from the local
cache, and recorded in `docs/evidence/`.

- **Tier A, migration 0021, Decision 66.** Four raw-layer indexes no query
  could use. 19.6 MB on production.
- **Tier B, migration 0022, Decision 67.** Lineup-reference questions moved
  from `game_event` to `lineup_stint`; five derived-layer indexes dropped.
  27.8 MB on production. A full-season derived rebuild went from 209 s to
  80 s on the rehearsal copy.
- **Tier D, migration 0023, Decision 68.** `raw_event` dropped; the event
  stream is stored once and proved against the parsed cache. 70.9 MB on
  production. Four proofs replaced or given up, each named in the decision.
- **Tier C** stays parked: it needs a table rewrite and was judged not worth
  its 28 MB now.

**Applied to production on 2026-09-06 UTC**, from the owner's terminal, one
migration at a time with sizes recorded in `docs/evidence/`:

| Migration | Database before | Database after | Freed |
|---|---:|---:|---:|
| 0021 | 364,326,035 | 344,755,347 | 19,570,688 |
| 0022 | 344,755,347 | 316,984,467 | 27,770,880 |
| 0023 | 316,984,467 | 264,334,483 | 52,649,984 |

100.0 MB in total, 27.4 % of the database, with two seasons loaded. Decision
68's condition held: production's `game_event_source` checksums for E2024
and E2025 equalled the rehearsal baselines on the day of the apply.

**Applied on 2026-09-07 UTC, derived layer expansion (Decisions 70-79).**
Migrations 0020 and 0024-0028 from the owner's terminal, then a per-game
derived rebuild of E2024 and E2025 so possession seconds and free-throw trip
ids filled. Database 264,424,595 to 283,634,835 bytes; the growth is rebuild
bloat plus two integer columns, the treadmill Decision 28 describes. All
eight production fingerprints (possession, game_event, game_event_source,
lineup_stint, both seasons) equal the rehearsal baselines. The nightly
per-game cost line (Decision 69) is the number to watch next.

**What none of this establishes.** The production figures. Every number above
is either the 2026-09-06 production measurement of what the migration removes
or a rehearsal on one fresh season; the whole-database size before and after
each production apply is what gets recorded next. Decision 21's bytes per game
is re-measured once E2026 has games.
