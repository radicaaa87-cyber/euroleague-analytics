# ML data architecture — fast ingest, durable storage, reproducible training

## Goal

The project handles large event streams, historical bookmaker documents and repeated
model training. The data path must minimize repeated network work and repeated heavy
SQL while preserving provenance, reproducibility and the MODEL 9 training contract.

The frozen MODEL 9 feature query and split remain the training baseline. New models
may add learning and diagnostics above that baseline, but they do not silently replace
the ingest, warehouse or feature semantics.

## Layer 1 — immutable source archive

Purpose: acquire source bytes once and preserve them.

Rules:

- Network fetch happens once per source version.
- Every body is content-addressed by checksum.
- Re-runs consume cached/archive bytes, not the network.
- User-supplied PDFs never go through web discovery.
- Raw archive is append-only; changed source bodies create a new version.
- Large play-by-play bodies belong in the archive/cold layer when they are not needed
  for interactive serving.

Outputs:
- EuroLeague API response archive.
- ACB raw source archive.
- Bookmaker PDF archive with SHA-256 and source name.

## Layer 2 — normalized source tables

Purpose: parse immutable source data once into stable source-native rows.

Examples:
- EuroLeague game/event/boxscore rows.
- ACB game/player-game/event rows.
- bookmaker source documents and parsed player-point offers.

Rules:
- Parsing is deterministic and idempotent.
- Batch writes are mandatory for archive-scale loads.
- No per-row network calls.
- No per-row database lookup when the required reference set can be loaded once.
- Source-native ids are preserved.
- Ambiguous identity matches remain unresolved instead of being guessed.

## Layer 3 — canonical/derived warehouse

Purpose: expensive basketball reconstruction happens once, not during every model run.

Examples:
- canonical athlete identities,
- possessions,
- lineup stints,
- player-game history,
- role/rotation/matchup derived history.

Rules:
- Build incrementally by changed game/source object.
- Derived rows must be reproducible from Layer 1 + Layer 2.
- Expensive historical computations are never performed by the MCP query layer.
- Cross-source joins use canonical identity ids, never player names.
- Historical bookmaker game linking is a batch operation against the verified
  warehouse, separate from PDF ingestion.

## Layer 4 — verified training snapshot

Purpose: create a stable, reproducible local PostgreSQL warehouse for model work.

Rules:
- Training never fetches raw APIs or bookmaker PDFs.
- A verified snapshot is created only when source/derived inputs change.
- Repeated model experiments restore the same snapshot from cache.
- Run ANALYZE immediately after snapshot restore so PostgreSQL has correct planner
  statistics.
- MODEL 9 training_dataset_sql is the frozen baseline extraction contract.
- A model-version change must not rebuild or re-fetch data unless its required inputs
  actually changed.
- Blind-season policy remains independent of storage optimizations.

## Layer 5 — model artifacts and validation marts

Purpose: keep expensive model outputs reusable and auditable.

Outputs:
- validation predictions,
- model artifacts,
- situation-signal attribution,
- bookmaker-line evaluation datasets,
- run metadata and metrics.

Rules:
- Each artifact records data snapshot identity, model commit and parameters.
- Bookmaker evaluation rows use canonical athlete_id + season_code + gamecode.
- Raw bookmaker lines are retained even when identity/game resolution fails.
- Model training and bookmaker collection are independent pipelines.

## Performance contract

The pipeline is considered healthy only if work scales with source objects, not with
chat/tool round-trips.

For every batch job record:
- input documents/games,
- parsed rows,
- inserted/updated rows,
- unresolved rows,
- elapsed seconds,
- snapshot/cache hit state,
- errors.

Expected shapes:
- already-owned PDF archive: one local read/parse pass + fixed-number bulk DB writes;
- athlete linking: one candidate load + in-memory matching + one bulk update;
- game linking: one historical candidate extraction + one batch update;
- model training: restore verified snapshot + ANALYZE + frozen MODEL 9 extraction +
  model-specific learning.

## Hot/cold storage rule

The hosted database is not the long-term home for every historical event row.

Current measured warning: acb_event alone is roughly 318 MB in the hosted database,
while the project has a 500 MB storage constraint. Historical high-volume event rows
must therefore be treated as cold/archive candidates once their reproducible compact
player-game/derived representations are verified.

No production rows are deleted merely to save space. Any cold-storage transition
requires:
1. immutable archived source proof,
2. rebuild test,
3. row/count/fingerprint verification,
4. explicit owner approval immediately before destructive production action.

## Change discipline

A model upgrade should normally change Layer 5 first. It may request new features
from Layer 3, but the data acquisition and storage layers stay stable unless a measured
bottleneck or missing evidence requires a separately reviewed change.

If a proposed model change also changes how historical data is fetched, restored or
parsed, treat that as a separate infrastructure change with its own benchmark and test.
