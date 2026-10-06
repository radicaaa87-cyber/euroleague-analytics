"""Disposable-schema proof that incremental derived writes equal one pass."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
from os import environ
from pathlib import Path
from typing import Any, Protocol

from psycopg import sql

from euroleague.cache import ResponseCache
from euroleague.compaction import E2024_BASELINE, E2025_BASELINE, compare_fingerprints
from euroleague.config import DatabaseSettings, load_env_file
from euroleague.derived import (
    DimensionRows,
    GameEventRow,
    RemainingDerivedRows,
    build_dimensions,
    build_game_events,
    build_remaining_rows,
)
from euroleague.derived_load import load_derived_rows
from euroleague.gate import derived_snapshot, warehouse_snapshot
from euroleague.load import load_cached_season, load_cached_shots

LOCAL_CONFIRMATION_DATABASE = "euroleague_test"
LOCAL_CONFIRMATION_PORT = 5433
LOCAL_CONFIRMATION_HOSTS = {"localhost", "127.0.0.1", "::1"}
TEST_URL_ENV_VAR = "EL_TEST_DATABASE_URL"


def load_test_database_settings(values: dict[str, str] | None = None) -> DatabaseSettings:
    """Build settings only from the disposable-database variable.

    LIVES IN THE PACKAGE, NOT IN `scripts/`. A test importing from `scripts.`
    resolves only when the working directory happens to be on `sys.path`, which
    is true under `python -m pytest` and false under the bare `pytest` that CI
    runs. `tests/conftest.py` documents that exact trap; this module is
    installed by `pip install -e .`, so both the CLI and the tests import it the
    same way from any directory.

    It refuses anything but the disposable database on its own port, so a
    confirmation run cannot be pointed at production by a stray variable.

    WHERE THE VALUE COMES FROM. A real environment variable wins over `.env`,
    the same rule `DatabaseSettings.from_env` follows and for the same reason:
    in CI the value comes from the workflow and there is no `.env` file at all,
    while on a developer's machine a stray file must not override an explicit
    variable. Reading the environment widens where the value may come from, not
    what it may say - the database and port check below is unchanged, and it is
    what stops a wrong secret pointing an up/down/up/down cycle, which ends in
    `drop`, at the real warehouse.
    """
    if values is None:
        env_values = dict(load_env_file())
        from_environment = environ.get(TEST_URL_ENV_VAR)
        if from_environment:
            env_values[TEST_URL_ENV_VAR] = from_environment
    else:
        env_values = values
    settings = DatabaseSettings.from_url(env_values.get(TEST_URL_ENV_VAR, ""))
    if (
        settings.database != LOCAL_CONFIRMATION_DATABASE
        or settings.port != LOCAL_CONFIRMATION_PORT
        or settings.host not in LOCAL_CONFIRMATION_HOSTS
    ):
        raise ValueError(
            f"EL_TEST_DATABASE_URL must name {LOCAL_CONFIRMATION_DATABASE!r} on local host "
            f"and port {LOCAL_CONFIRMATION_PORT}; received host {settings.host!r}, "
            f"database {settings.database!r} on port {settings.port}."
        )
    return settings


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MIGRATIONS_ROOT = REPO_ROOT / "migrations"


class ConfirmationTargetError(RuntimeError):
    """Raised before a write when confirmation is not on the disposable local target."""


class ProductionBaselineMismatch(RuntimeError):
    """Raised when a fresh local build differs from the recorded production content."""


class SchemaScopeError(RuntimeError):
    """Raised before a write when search_path does not select the owned schema."""


def assert_local_confirmation_target(connection: Any) -> None:
    """Refuse every write unless the connection target is the disposable local database.

    Docker port publishing means PostgreSQL can listen on 5432 inside the service
    while the client deliberately connects through localhost:5433.  The safety
    boundary is therefore the client connection target, not inet_server_port().
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        database_name = cursor.fetchone()[0]

    info = connection.info
    host = str(info.host)
    port = int(info.port)
    if (
        database_name != LOCAL_CONFIRMATION_DATABASE
        or host not in LOCAL_CONFIRMATION_HOSTS
        or port != LOCAL_CONFIRMATION_PORT
    ):
        raise ConfirmationTargetError(
            f"Expected {LOCAL_CONFIRMATION_DATABASE!r} on local host and port "
            f"{LOCAL_CONFIRMATION_PORT}; received host {host!r}, database "
            f"{database_name!r} on port {port}. No confirmation write was attempted."
        )


def prepare_confirmation_session(connection: Any) -> None:
    """Pin timezone-sensitive JSON fingerprints to production's UTC session setting."""
    assert_local_confirmation_target(connection)
    with connection.cursor() as cursor:
        cursor.execute("SET TIME ZONE 'UTC'")


def assert_production_baseline_matches(
    season_code: str,
    observed: dict[str, RelationFingerprint],
) -> None:
    """Require the local snapshot to equal the recorded production snapshot exactly."""
    baselines = {"E2024": E2024_BASELINE, "E2025": E2025_BASELINE}
    try:
        expected = baselines[season_code]
    except KeyError as error:
        raise ValueError(f"No production baseline is recorded for {season_code}.") from error
    comparable = {
        table: (fingerprint.count, fingerprint.checksum) for table, fingerprint in observed.items()
    }
    mismatches = compare_fingerprints(expected, comparable)
    if mismatches:
        detail = "; ".join(str(mismatch) for mismatch in mismatches)
        raise ProductionBaselineMismatch(
            f"{season_code} local build differs from the production baseline: {detail}"
        )


def production_baseline_fingerprints(
    connection: Any, season_code: str
) -> dict[str, RelationFingerprint]:
    """Recompute checksums with the unchanged functions used to capture production."""
    baselines = {"E2024": E2024_BASELINE, "E2025": E2025_BASELINE}
    try:
        baseline_tables = set(baselines[season_code])
    except KeyError as error:
        raise ValueError(f"No production baseline is recorded for {season_code}.") from error
    snapshots = {
        **warehouse_snapshot(connection, season_code),
        **derived_snapshot(connection, season_code),
    }
    return {
        table: RelationFingerprint(fingerprint.count, fingerprint.checksum)
        for table, fingerprint in snapshots.items()
        if table in baseline_tables
    }


def load_confirmation_raw_rows(
    connection: Any, cache: ResponseCache, season_code: str
) -> dict[str, int]:
    """Load every raw relation covered by the recorded production baselines."""
    counts = load_cached_season(connection, cache, season_code, progress=lambda _: None)
    counts.update(load_cached_shots(connection, cache, season_code, progress=lambda _: None))
    return counts


@dataclass(frozen=True)
class RelationFingerprint:
    count: int
    checksum: str


@dataclass(frozen=True)
class SizeReading:
    phase: str
    bytes: int


@dataclass(frozen=True)
class SeasonConfirmation:
    season_code: str
    split_after: int
    single: dict[str, RelationFingerprint]
    first_before_second: dict[str, RelationFingerprint]
    first_after_second: dict[str, RelationFingerprint]
    batched: dict[str, RelationFingerprint]
    production_baseline: dict[str, RelationFingerprint]
    sizes: tuple[SizeReading, ...]
    game_event_updates: dict[str, int]


class DerivedWriter(Protocol):
    def __call__(
        self,
        connection: Any,
        dimensions: DimensionRows,
        events: tuple[GameEventRow, ...],
        remaining: RemainingDerivedRows,
        season_code: str,
        gamecodes: Sequence[int] | None,
    ) -> dict[str, int]: ...


def _assert_schema_name(schema_name: str) -> None:
    if not schema_name or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in schema_name
    ):
        raise ValueError(f"Unsafe temporary schema name {schema_name!r}.")
    if not schema_name.startswith(("confirm_single_", "confirm_batched_")):
        raise ValueError(f"Confirmation schema name has an unexpected prefix: {schema_name!r}.")


def assert_current_schema(connection: Any, expected_schema: str) -> None:
    """Refuse a write unless the connection resolves to the owned schema."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        observed = cursor.fetchone()[0]
    if observed != expected_schema:
        raise SchemaScopeError(
            f"Expected current_schema() to be {expected_schema!r}; received {observed!r}. "
            "No confirmation write was attempted."
        )


def measure_database_size(connection: Any, phase: str) -> SizeReading:
    """Read the exact current-database byte count without applying a production ceiling."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_database_size(current_database())")
        database_bytes = int(cursor.fetchone()[0])
    return SizeReading(phase, database_bytes)


def run_guarded_step(
    connection: Any,
    schema_name: str,
    phase: str,
    action: Callable[[], Any],
    readings: list[SizeReading],
) -> Any:
    """Check schema and size on both sides of one database load step."""
    assert_local_confirmation_target(connection)
    assert_current_schema(connection, schema_name)
    readings.append(measure_database_size(connection, f"before {phase}"))
    result = action()
    assert_current_schema(connection, schema_name)
    readings.append(measure_database_size(connection, f"after {phase}"))
    return result


@contextmanager
def managed_schema(connection: Any, schema_name: str):
    """Create one owned schema and drop it even when the enclosed load fails."""
    _assert_schema_name(schema_name)
    created = False
    try:
        assert_local_confirmation_target(connection)
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
            created = True
            cursor.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
        assert_current_schema(connection, schema_name)
        yield
    finally:
        if created:
            assert_local_confirmation_target(connection)
            with connection.cursor() as cursor:
                cursor.execute("SET search_path TO pg_catalog")
                cursor.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
                )
                cursor.execute(
                    "SELECT count(*) FROM pg_namespace WHERE nspname = %s",
                    (schema_name,),
                )
                remaining = int(cursor.fetchone()[0])
            if remaining:
                raise RuntimeError(f"Temporary schema {schema_name!r} still exists after cleanup.")


def _scope_sql(gamecodes: Sequence[int] | None, *, alias: str = "t") -> tuple[str, tuple]:
    if gamecodes is None:
        return f"{alias}.season_code = %s", ()
    return f"{alias}.season_code = %s AND {alias}.gamecode = ANY(%s)", (list(gamecodes),)


def _fingerprint_query(
    name: str,
    source_sql: str,
    order_sql: str,
) -> str:
    return f"""
        /* fingerprint:{name} */
        WITH scoped AS (
            {source_sql}
        )
        SELECT count(*),
               md5(coalesce(string_agg(
                   md5(to_jsonb(scoped)::text), '' ORDER BY {order_sql}
               ), ''))
        FROM scoped
    """


def fingerprint_relations(
    connection: Any,
    expected_schema: str,
    season_code: str,
    *,
    gamecodes: Sequence[int] | None = None,
) -> dict[str, RelationFingerprint]:
    """Fingerprint every persisted value in its real primary-key order."""
    assert_current_schema(connection, expected_schema)
    scope, extra_params = _scope_sql(gamecodes)
    params = (season_code, *extra_params)
    # Lineups are scoped through lineup_stint, not game_event: every event
    # carries its stint's lineup ids, the two sets are measured equal on E2024
    # and E2025, and lineup_stint keeps its lineup indexes after migration
    # 0022 removed game_event's. See DECISIONS.md item 67.
    lineup_scope, lineup_extra = _scope_sql(gamecodes, alias="stint")
    lineup_params = (season_code, *lineup_extra)
    queries: dict[str, tuple[str, tuple]] = {
        "game_event": (
            _fingerprint_query(
                "game_event",
                f"SELECT t.* FROM game_event t WHERE {scope}",
                "season_code, gamecode, ingest_index",
            ),
            params,
        ),
        "lineup": (
            _fingerprint_query(
                "lineup",
                "SELECT t.* FROM lineup t WHERE EXISTS ("
                "SELECT 1 FROM lineup_stint stint WHERE "
                f"{lineup_scope} AND t.lineup_id IN "
                "(stint.home_lineup_id, stint.away_lineup_id))",
                "lineup_id",
            ),
            lineup_params,
        ),
        "lineup_stint": (
            _fingerprint_query(
                "lineup_stint",
                f"SELECT t.* FROM lineup_stint t WHERE {scope}",
                "season_code, gamecode, stint_index",
            ),
            params,
        ),
        "player_game_minutes": (
            _fingerprint_query(
                "player_game_minutes",
                f"SELECT t.* FROM player_game_minutes t WHERE {scope}",
                "season_code, gamecode, player_id",
            ),
            params,
        ),
        "game_quality": (
            _fingerprint_query(
                "game_quality",
                f"SELECT t.* FROM game_quality t WHERE {scope}",
                "season_code, gamecode",
            ),
            params,
        ),
        "possession": (
            _fingerprint_query(
                "possession",
                f"SELECT t.* FROM possession t WHERE {scope}",
                "season_code, gamecode, possession_index",
            ),
            params,
        ),
        "game_event_attachment": (
            _fingerprint_query(
                "game_event_attachment",
                "SELECT t.season_code, t.gamecode, t.ingest_index, "
                "t.home_lineup_id, t.away_lineup_id, t.stint_index, t.possession_index "
                f"FROM game_event t WHERE {scope}",
                "season_code, gamecode, ingest_index",
            ),
            params,
        ),
    }
    result: dict[str, RelationFingerprint] = {}
    with connection.cursor() as cursor:
        for name, (query, query_params) in queries.items():
            cursor.execute(query, query_params)
            count, checksum = cursor.fetchone()
            result[name] = RelationFingerprint(int(count), str(checksum))
    return result


def assert_same_fingerprints(
    expected: dict[str, RelationFingerprint],
    observed: dict[str, RelationFingerprint],
    comparison: str,
) -> None:
    """Name every missing, extra, or content-different relation."""
    mismatches = {
        name: (expected.get(name), observed.get(name))
        for name in sorted(set(expected) | set(observed))
        if expected.get(name) != observed.get(name)
    }
    if mismatches:
        raise AssertionError(f"Fingerprint mismatch for {comparison}: {mismatches}")


def rehearsal_role_names(schema_name: str) -> tuple[str, str, str]:
    """Return deterministic run-scoped roles for a disposable migration rehearsal.

    One name per persistent role the migrations create: `el_reader` (0013),
    `el_usage_writer` (0016) and `el_tester` (0020). The third was missing
    until 2026-09-07, so a rehearsal schema granted privileges to the real
    `el_tester` and the migration gate could not drop that role afterwards.
    """
    suffix = sha256(schema_name.encode("utf-8")).hexdigest()[:16]
    return (
        f"rehearsal_reader_{suffix}",
        f"rehearsal_usage_writer_{suffix}",
        f"rehearsal_tester_{suffix}",
    )


def rewrite_rehearsal_migration(
    sql_text: str,
    *,
    quoted_schema: str,
    reader_role: str,
    usage_writer_role: str,
    tester_role: str,
) -> str:
    """Retarget public-schema DDL and persistent roles to one disposable run."""
    rewritten = sql_text.replace("public.", f"{quoted_schema}.")
    rewritten = rewritten.replace("schema public", f"schema {quoted_schema}")
    rewritten = rewritten.replace("search_path = public", f"search_path = {quoted_schema}")
    rewritten = rewritten.replace(
        "create role el_usage_writer with login;",
        """do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'el_usage_writer') then
        create role el_usage_writer with login;
    end if;
end
$$;""",
    )
    rewritten = rewritten.replace("el_usage_writer", usage_writer_role)
    rewritten = rewritten.replace("el_tester", tester_role)
    return rewritten.replace("el_reader", reader_role)


def apply_current_migrations(connection: Any) -> None:
    """Apply every committed up migration to the selected temporary schema."""
    migrations = sorted(MIGRATIONS_ROOT.glob("*.up.sql"))
    if not migrations:
        raise RuntimeError(f"No up migrations found in {MIGRATIONS_ROOT}.")
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        cur_schema = cursor.fetchone()[0]
        for migration in migrations:
            sql_text = migration.read_text(encoding="utf-8")
            if cur_schema and cur_schema not in ("public", "pg_catalog"):
                quoted_schema = sql.Identifier(str(cur_schema)).as_string(connection)
                reader_role, usage_writer_role, tester_role = rehearsal_role_names(str(cur_schema))
                sql_text = rewrite_rehearsal_migration(
                    sql_text,
                    quoted_schema=quoted_schema,
                    reader_role=reader_role,
                    usage_writer_role=usage_writer_role,
                    tester_role=tester_role,
                )
            cursor.execute(sql_text)


def current_derived_writer(
    connection: Any,
    dimensions: DimensionRows,
    events: tuple[GameEventRow, ...],
    remaining: RemainingDerivedRows,
    season_code: str,
    gamecodes: Sequence[int] | None,
) -> dict[str, int]:
    """Persist through the parent-first Option A writer under confirmation."""
    return load_derived_rows(
        connection,
        dimensions,
        events,
        remaining,
        season_code,
        gamecodes=gamecodes,
    )


def game_event_update_statistics(connection: Any, schema_name: str) -> dict[str, int]:
    """Read actual tuple-update and remaining-dead-tuple statistics for events."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_stat_force_next_flush()")
        cursor.execute("SELECT pg_stat_clear_snapshot()")
        cursor.execute(
            """
            SELECT coalesce(n_tup_upd, 0), coalesce(n_dead_tup, 0)
            FROM pg_stat_all_tables
            WHERE schemaname = %s AND relname = 'game_event'
            """,
            (schema_name,),
        )
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError(f"No pg_stat_all_tables row found for {schema_name}.game_event.")
    return {"n_tup_upd": int(row[0]), "n_dead_tup": int(row[1])}


def _artifact_payload(
    season_code: str,
    split_after: int,
    readings: list[SizeReading],
    **snapshots: Any,
) -> dict[str, Any]:
    return {
        "season_code": season_code,
        "split_after": split_after,
        "sizes": [asdict(reading) for reading in readings],
        **snapshots,
    }


def _serialise_fingerprints(
    fingerprints: dict[str, RelationFingerprint],
) -> dict[str, dict[str, Any]]:
    return {name: asdict(value) for name, value in fingerprints.items()}


def _write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_confirmation(
    connection: Any,
    cache: ResponseCache,
    season_code: str,
    split_after: int,
    writer: DerivedWriter,
    artifact_path: Path,
    run_id: str,
    *,
    progress: Callable[[str], None] = print,
) -> SeasonConfirmation:
    """Run sequential single-pass and two-batch database builds for one season."""
    prepare_confirmation_session(connection)
    if not run_id or not run_id.isalnum():
        raise ValueError("run_id must contain only letters and digits.")
    schedule = cache.read_schedule_json(season_code)
    gamecodes = sorted(
        int(game["gameCode"]) for game in schedule.get("data") or [] if game.get("played") is True
    )
    if not 0 < split_after < len(gamecodes):
        raise ValueError(
            f"Split {split_after} must fall inside {len(gamecodes)} played {season_code} games."
        )
    first_codes = gamecodes[:split_after]
    second_codes = gamecodes[split_after:]
    dimensions = build_dimensions(cache, season_code)
    events = build_game_events(cache, season_code)
    remaining = build_remaining_rows(cache, season_code)
    readings: list[SizeReading] = []
    readings.append(measure_database_size(connection, f"{season_code} start"))

    single_schema = f"confirm_single_{run_id.lower()}"
    single: dict[str, RelationFingerprint]
    single_stats: dict[str, int]
    with managed_schema(connection, single_schema):
        run_guarded_step(
            connection,
            single_schema,
            f"{season_code} single migrations",
            lambda: apply_current_migrations(connection),
            readings,
        )
        run_guarded_step(
            connection,
            single_schema,
            f"{season_code} single raw load",
            lambda: load_confirmation_raw_rows(connection, cache, season_code),
            readings,
        )
        run_guarded_step(
            connection,
            single_schema,
            f"{season_code} single derived load",
            lambda: writer(connection, dimensions, events, remaining, season_code, None),
            readings,
        )
        single = fingerprint_relations(connection, single_schema, season_code)
        single_stats = game_event_update_statistics(connection, single_schema)
        production_baseline = production_baseline_fingerprints(connection, season_code)
        _write_artifact(
            artifact_path,
            _artifact_payload(
                season_code,
                split_after,
                readings,
                single=_serialise_fingerprints(single),
                single_game_event_stats=single_stats,
                production_baseline=_serialise_fingerprints(production_baseline),
                status="single and production baseline captured before assertion",
            ),
        )
        assert_production_baseline_matches(season_code, production_baseline)
    readings.append(measure_database_size(connection, f"{season_code} after single cleanup"))
    progress(f"{season_code}: single-pass schema captured and dropped")

    batched_schema = f"confirm_batched_{run_id.lower()}"
    with managed_schema(connection, batched_schema):
        run_guarded_step(
            connection,
            batched_schema,
            f"{season_code} batched migrations",
            lambda: apply_current_migrations(connection),
            readings,
        )
        run_guarded_step(
            connection,
            batched_schema,
            f"{season_code} batched raw load",
            lambda: load_confirmation_raw_rows(connection, cache, season_code),
            readings,
        )
        run_guarded_step(
            connection,
            batched_schema,
            f"{season_code} first derived batch",
            lambda: writer(connection, dimensions, events, remaining, season_code, first_codes),
            readings,
        )
        first_before_second = fingerprint_relations(
            connection,
            batched_schema,
            season_code,
            gamecodes=first_codes,
        )
        _write_artifact(
            artifact_path,
            _artifact_payload(
                season_code,
                split_after,
                readings,
                single=_serialise_fingerprints(single),
                single_game_event_stats=single_stats,
                production_baseline=_serialise_fingerprints(production_baseline),
                first_before_second=_serialise_fingerprints(first_before_second),
                status="first batch captured",
            ),
        )
        run_guarded_step(
            connection,
            batched_schema,
            f"{season_code} second derived batch",
            lambda: writer(connection, dimensions, events, remaining, season_code, second_codes),
            readings,
        )
        first_after_second = fingerprint_relations(
            connection,
            batched_schema,
            season_code,
            gamecodes=first_codes,
        )
        assert_same_fingerprints(
            first_before_second,
            first_after_second,
            f"{season_code} first batch after second batch",
        )
        batched = fingerprint_relations(connection, batched_schema, season_code)
        assert_same_fingerprints(single, batched, f"{season_code} single versus batched")
        batched_stats = game_event_update_statistics(connection, batched_schema)
        _write_artifact(
            artifact_path,
            _artifact_payload(
                season_code,
                split_after,
                readings,
                single=_serialise_fingerprints(single),
                single_game_event_stats=single_stats,
                production_baseline=_serialise_fingerprints(production_baseline),
                first_before_second=_serialise_fingerprints(first_before_second),
                first_after_second=_serialise_fingerprints(first_after_second),
                batched=_serialise_fingerprints(batched),
                batched_game_event_stats=batched_stats,
                status="pass before cleanup",
            ),
        )
    readings.append(measure_database_size(connection, f"{season_code} after batched cleanup"))
    updates = {
        "single_n_tup_upd": single_stats["n_tup_upd"],
        "single_n_dead_tup": single_stats["n_dead_tup"],
        "batched_n_tup_upd": batched_stats["n_tup_upd"],
        "batched_n_dead_tup": batched_stats["n_dead_tup"],
    }
    result = SeasonConfirmation(
        season_code=season_code,
        split_after=split_after,
        single=single,
        first_before_second=first_before_second,
        first_after_second=first_after_second,
        batched=batched,
        production_baseline=production_baseline,
        sizes=tuple(readings),
        game_event_updates=updates,
    )
    _write_artifact(
        artifact_path,
        {
            **_artifact_payload(
                season_code,
                split_after,
                readings,
                single=_serialise_fingerprints(single),
                first_before_second=_serialise_fingerprints(first_before_second),
                first_after_second=_serialise_fingerprints(first_after_second),
                batched=_serialise_fingerprints(batched),
                production_baseline=_serialise_fingerprints(production_baseline),
                game_event_updates=updates,
            ),
            "status": "pass and cleaned",
        },
    )
    progress(f"{season_code}: batched schema matched, captured, and dropped")
    return result
