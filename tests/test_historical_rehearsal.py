"""Tests for the R-12 historical-season warehouse rehearsal engine."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from euroleague.cache import ResponseCache
from euroleague.historical_rehearsal import (
    GameSkippingCache,
    RelationSizeMetric,
    assert_loaded_counts,
    assert_rehearsal_target_safe,
    assert_season_loaded_counts,
    assert_shared_union_counts,
    calculate_storage_projections,
    compute_exclusion_breakdown,
    load_persistent_warehouse,
    managed_rehearsal_schema,
    measure_schema_relations,
    run_historical_rehearsal,
    verify_cache_integrity,
)
from euroleague.incremental_confirmation import (
    LOCAL_CONFIRMATION_DATABASE,
    LOCAL_CONFIRMATION_PORT,
    ConfirmationTargetError,
    rehearsal_role_names,
    rewrite_rehearsal_migration,
)


def _sql_text(query: Any) -> str:
    if hasattr(query, "as_string"):
        return query.as_string()
    return " ".join(str(query).split())


class DummyCursor:
    def __init__(self, connection: DummyConnection) -> None:
        self.connection = connection
        self.last_query = ""
        self.last_params = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, query, params=None) -> None:
        self.last_query = " ".join(_sql_text(query).split())
        self.last_params = params
        self.connection.executions.append((self.last_query, params))
        if self.last_query.startswith("CREATE SCHEMA"):
            name = self.last_query.split('"')[1]
            self.connection.schemas.add(name)
        elif self.last_query.startswith("SET search_path TO"):
            quoted = self.last_query.split('"')
            self.connection.current_schema = (
                quoted[1] if len(quoted) > 1 else self.last_query.rsplit(" ", 1)[1]
            )
        elif self.last_query.startswith("DROP SCHEMA"):
            name = self.last_query.split('"')[1]
            self.connection.schemas.discard(name)
            if self.connection.current_schema == name:
                self.connection.current_schema = None
        elif self.last_query.startswith("DROP ROLE"):
            name = self.last_query.split('"')[1]
            self.connection.roles.discard(name)

    def fetchone(self):
        if self.last_query == "SELECT current_database()":
            return (self.connection.database_name,)
        if self.last_query == "SELECT current_schema()":
            return (self.connection.current_schema,)
        if self.last_query == "SHOW server_version":
            return ("18.6",)
        if "pg_total_relation_size" in self.last_query:
            return (1_000_000, 500_000, 1_500_000)
        if "SELECT count(*) FROM pg_namespace" in self.last_query:
            return (int(self.last_params[0] in self.connection.schemas),)
        if self.last_query.startswith("SELECT count(*) FROM"):
            name = self.last_query.split('"')[-2]
            return (self.connection.relation_rows[name],)
        if "bad_team_minutes" in self.last_query or "unpaired" in self.last_query:
            return (0,)
        if "FROM game_quality" in self.last_query:
            return (0, 0, 0, 0, 0)
        return (0,)

    def fetchall(self):
        if "FROM pg_class" in self.last_query:
            return [(name,) for name in sorted(self.connection.relation_rows)]
        if "FROM pg_roles" in self.last_query:
            requested = set(self.last_params[0])
            return [(name,) for name in sorted(self.connection.roles & requested)]
        if "quarantine_reasons" in self.last_query:
            return []
        return []

    def copy(self, statement: str):
        self.connection.copies.append(statement)

        class CopyContext:
            def __enter__(self_ctx):
                return self_ctx

            def __exit__(self_ctx, *args):
                return None

            def write_row(self_ctx, row):
                pass

        return CopyContext()


class DummyConnection:
    def __init__(
        self,
        database_name: str = LOCAL_CONFIRMATION_DATABASE,
        port: int = LOCAL_CONFIRMATION_PORT,
    ) -> None:
        self.database_name = database_name
        self.port = port
        self.info = SimpleNamespace(host="localhost", port=port)
        self.current_schema: str | None = None
        self.schemas: set[str] = set()
        self.executions: list[tuple[str, object]] = []
        self.copies: list[str] = []
        self.relation_rows: dict[str, int] = {}
        self.roles: set[str] = set()

    def cursor(self):
        return DummyCursor(self)

    def transaction(self):
        class TxContext:
            def __enter__(self_ctx):
                return self_ctx

            def __exit__(self_ctx, *args):
                return None

        return TxContext()

    def close(self) -> None:
        pass


def test_guard_refuses_production_or_unauthorised_database() -> None:
    """Break caught: rehearsal connects to production or wrong port."""
    prod_conn = DummyConnection(database_name="postgres", port=5432)
    with pytest.raises(ConfirmationTargetError, match="No confirmation write was attempted"):
        assert_rehearsal_target_safe(prod_conn)

    wrong_port_conn = DummyConnection(database_name=LOCAL_CONFIRMATION_DATABASE, port=5432)
    with pytest.raises(ConfirmationTargetError, match="No confirmation write was attempted"):
        assert_rehearsal_target_safe(wrong_port_conn)

    safe_conn = DummyConnection()
    assert_rehearsal_target_safe(safe_conn)


def test_managed_schema_removes_schema_and_roles_created_by_the_run() -> None:
    """Break caught: a successful or failed rehearsal leaves cluster state behind."""
    connection = DummyConnection()

    role_names = set(rehearsal_role_names("rehearse_success"))
    with managed_rehearsal_schema(connection, "rehearse_success"):
        assert "rehearse_success" in connection.schemas
        connection.roles.update(role_names)
    assert "rehearse_success" not in connection.schemas
    assert not connection.roles

    with (
        pytest.raises(RuntimeError, match="synthetic failure"),
        managed_rehearsal_schema(connection, "rehearse_failure"),
    ):
        raise RuntimeError("synthetic failure")
    assert "rehearse_failure" not in connection.schemas


def test_rehearsal_roles_are_run_scoped_and_bounded() -> None:
    """Break caught: a rehearsal mutates the persistent application roles."""
    first = rehearsal_role_names("rehearse_e2023_20260831202754")
    second = rehearsal_role_names("rehearse_e2023_20260831202755")

    assert first != second
    assert len(first) == 3, "reader, usage writer and tester: one per persistent role"
    assert set(first).isdisjoint({"el_reader", "el_usage_writer", "el_tester"})
    assert all(len(role_name) <= 63 for role_name in first)


def test_rehearsal_migration_rewrites_every_public_schema_reference() -> None:
    """Break caught: a temporary role receives privileges on the real public schema."""
    source = """
    create role el_usage_writer with login;
    grant usage on schema public to el_reader;
    create role el_tester with login;
    alter role el_tester bypassrls;
    grant select on all tables in schema public to el_tester;
    create table public.example (id integer);
    set search_path = public, pg_temp;
    """

    rewritten = rewrite_rehearsal_migration(
        source,
        quoted_schema='"rehearse_e2023_test"',
        reader_role="rehearsal_reader_test",
        usage_writer_role="rehearsal_usage_writer_test",
        tester_role="rehearsal_tester_test",
    )

    assert "schema public" not in rewritten
    assert "public.example" not in rewritten
    assert "search_path = public" not in rewritten
    assert "el_reader" not in rewritten
    assert "el_usage_writer" not in rewritten
    # Migration 0020's role. On 2026-09-06 it was the one persistent role the
    # rewrite missed: a rehearsal schema granted to the real el_tester, and the
    # migration gate's `down 0020` then could not drop the role.
    assert "el_tester" not in rewritten
    assert "rehearsal_tester_test bypassrls" in rewritten
    assert 'schema "rehearse_e2023_test"' in rewritten


def test_cache_integrity_verification_checks_expected_files(tmp_path: Path) -> None:
    """Break caught: missing or corrupted cache passes as complete."""
    cache_root = tmp_path / "cache"
    season_root = cache_root / "E2023"
    season_root.mkdir(parents=True)
    schedule_data = {
        "data": [
            {"gameCode": 1, "played": True},
            {"gameCode": 2, "played": False},
        ]
    }
    (season_root / "schedule.json").write_text(json.dumps(schedule_data))

    cache = ResponseCache(cache_root)

    with pytest.raises(RuntimeError, match="missing"):
        verify_cache_integrity(cache, "E2023")

    for endpoint in ("Boxscore", "PlaybyPlay", "Points"):
        ep_dir = season_root / endpoint
        ep_dir.mkdir(parents=True, exist_ok=True)
        (ep_dir / "1.json").write_text(json.dumps({"Rows": [], "Stats": []}))

    completeness = verify_cache_integrity(cache, "E2023")
    assert completeness.scheduled_games == 2
    assert completeness.played_games == 1
    assert completeness.response_files == 3


def test_exclusion_breakdown_computation() -> None:
    """Break caught: exclusion rate or reason aggregation is inaccurate."""
    qualities = [
        SimpleNamespace(
            gamecode=1,
            excluded_by_default=False,
            quarantine_reasons=(),
        ),
        SimpleNamespace(
            gamecode=2,
            excluded_by_default=True,
            quarantine_reasons=("possession_gate",),
        ),
        SimpleNamespace(
            gamecode=3,
            excluded_by_default=True,
            quarantine_reasons=("off_court_attribution", "possession_gate"),
        ),
    ]
    breakdown = compute_exclusion_breakdown(qualities, scheduled_games=3)

    assert breakdown.scheduled_games == 3
    assert breakdown.played_games == 3
    assert breakdown.loaded_games == 3
    assert breakdown.excluded_games == 2
    assert breakdown.covered_games == 1
    assert round(breakdown.exclusion_rate_pct, 2) == 66.67
    assert breakdown.reasons == {"possession_gate": 2, "off_court_attribution": 1}


def test_storage_projections_calculation() -> None:
    """Break caught: extrapolation or per-game arithmetic is incorrect."""
    sizes = {
        "raw_game": RelationSizeMetric("raw_game", 100_000, 50_000, 0, 150_000, 331),
        "game_event": RelationSizeMetric("game_event", 800_000, 400_000, 0, 1_200_000, 170_000),
    }
    proj = calculate_storage_projections(sizes, played_games=331)

    assert proj.season_total_bytes == 1_350_000
    assert proj.bytes_per_game == pytest.approx(1_350_000 / 331, rel=1e-4)
    assert proj.projected_23_seasons_bytes == pytest.approx((1_350_000 / 331) * 5950, rel=1e-4)
    assert proj.projected_hot_window_bytes == pytest.approx((1_350_000 / 331) * 1112, rel=1e-4)
    assert proj.supabase_free_tier_bytes == 500_000_000
    assert proj.usable_budget_bytes == 474_311_115


def test_schema_measurement_includes_empty_warehouse_tables() -> None:
    """Break caught: the physical-size total silently omits empty real tables."""
    connection = DummyConnection()
    connection.current_schema = "rehearse_e2023_test"
    connection.relation_rows = {"raw_game": 331, "season_progress": 0}

    measured = measure_schema_relations(connection)

    assert set(measured) == {"raw_game", "season_progress"}
    assert measured["raw_game"].row_count == 331
    assert measured["season_progress"].row_count == 0


def test_loaded_count_reconciliation_refuses_missing_or_mismatched_rows() -> None:
    """Break caught: a partial database load is reported as a successful rehearsal."""
    measured = {
        "raw_game": RelationSizeMetric("raw_game", 1, 1, 0, 2, 330),
    }
    with pytest.raises(AssertionError, match="raw_game"):
        assert_loaded_counts({"raw_game": 331}, measured)

    with pytest.raises(AssertionError, match="game_event"):
        assert_loaded_counts({"raw_game": 330, "game_event": 10}, measured)


def test_committed_evidence_labels_multi_season_numbers_as_estimates() -> None:
    """Break caught: one measured season is overclaimed as a 23-season measurement."""
    evidence = json.loads(
        Path("docs/evidence/historical_rehearsal_E2023.json").read_text(encoding="utf-8")
    )
    report = Path("docs/HISTORICAL_WAREHOUSE_REHEARSAL_REPORT.md").read_text(encoding="utf-8")

    assert len(evidence["relation_sizes"]) == 23
    assert evidence["relation_sizes"]["season_progress"]["row_count"] == 0
    assert "linear estimate" in report.lower()
    assert "not a physical measurement" in report.lower()
    assert "proves conclusively" not in report.lower()
    assert "fits comfortably" not in report.lower()


def test_rehearsal_with_dummy_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Break caught: schema setup, migrations, or size query logic fails under connection."""
    real_cache = ResponseCache("exploration/cache")
    if not (Path("exploration/cache/E2023/schedule.json")).exists():
        pytest.skip("E2023 cache not present")

    import euroleague.historical_rehearsal as hr

    monkeypatch.setattr(hr, "apply_current_migrations", lambda conn: None)
    monkeypatch.setattr(hr, "load_confirmation_raw_rows", lambda conn, cache, sc: {})
    monkeypatch.setattr(
        hr,
        "load_derived_rows",
        lambda conn, dims, evts, rem, sc, gamecodes=None: {},
    )

    conn = DummyConnection()
    conn.relation_rows = {
        "raw_game": 331,
        "raw_boxscore_player": 7_883,
        "raw_boxscore_team": 1_324,
        "raw_shot": 50_159,
        "player": 296,
        "team": 18,
        "team_season": 18,
        "lineup": 5_817,
        "lineup_stint": 13_697,
        "game_event": 172_265,
        "player_game_minutes": 7_883,
        "game_quality": 331,
        "possession": 47_460,
        "season_progress": 0,
    }
    result = run_historical_rehearsal(
        real_cache,
        connection=conn,
        season_code="E2023",
        run_id="testdummy",
    )
    assert result.season_code == "E2023"
    assert result.postgres_version == "18.6"
    assert "euroleague_test:5433" in (result.database_target or "")
    assert result.exclusions.played_games == 331
    assert result.exclusions.loaded_games == 331
    assert result.exclusions.excluded_games == 25
    assert round(result.exclusions.exclusion_rate_pct, 2) == 7.55
    assert result.raw_counts["raw_game"] == 331
    # The event stream is stored once, in `game_event`, since migration 0023
    # dropped `raw_event` (DECISIONS.md item 68): the parsed event count is
    # reconciled through the derived counts and has no raw entry any more.
    assert "raw_event" not in result.raw_counts
    assert result.derived_counts["game_event"] == 172_265
    assert result.derived_counts["possession"] == 47_460
    assert any("CREATE SCHEMA" in query for query, _ in conn.executions)
    assert any("DROP SCHEMA" in query for query, _ in conn.executions)
    assert "game_event" in result.relation_sizes

    # Test serialization
    output_file = tmp_path / "rehearsal_result.json"
    output_file.write_text(result.to_json())
    loaded = json.loads(output_file.read_text())
    assert loaded["season_code"] == "E2023"
    assert loaded["exclusions"]["excluded_games"] == 25


SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "rehearse_historical_warehouse.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "rehearse_historical_warehouse_under_test", SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["rehearse_historical_warehouse_under_test"] = module
    spec.loader.exec_module(module)
    return module


def test_cli_argument_parsing() -> None:
    """Break caught: CLI fails to parse arguments or defaults correctly."""
    script = _load_script()
    opts = script.parse_arguments([])
    assert opts.season_code == "E2023"
    assert opts.cache_dir == "exploration/cache"
    assert not opts.quiet

    custom = script.parse_arguments(["-s", "E2022", "-q", "-o", "out.json"])
    assert custom.season_code == "E2022"
    assert custom.quiet
    assert custom.output == Path("out.json")


def test_cli_execution_with_dummy_db(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Break caught: CLI main function fails with database connection."""
    if not (Path("exploration/cache/E2023/schedule.json")).exists():
        pytest.skip("E2023 cache not present")

    script = _load_script()
    conn = DummyConnection()
    conn.relation_rows = {
        "raw_game": 331,
        "raw_boxscore_player": 7_883,
        "raw_boxscore_team": 1_324,
        "raw_shot": 50_159,
        "player": 296,
        "team": 18,
        "team_season": 18,
        "lineup": 5_817,
        "lineup_stint": 13_697,
        "game_event": 172_265,
        "player_game_minutes": 7_883,
        "game_quality": 331,
        "possession": 47_460,
    }
    monkeypatch.setattr(script.psycopg, "connect", lambda *args, **kwargs: conn)
    import euroleague.historical_rehearsal as hr

    monkeypatch.setattr(hr, "apply_current_migrations", lambda c: None)
    monkeypatch.setattr(hr, "load_confirmation_raw_rows", lambda c, cache, sc: {})
    monkeypatch.setattr(
        hr,
        "load_derived_rows",
        lambda c, dims, evts, rem, sc, gamecodes=None: {},
    )

    out_file = tmp_path / "cli_rehearsal.json"
    exit_code = script.main(["--season-code", "E2023", "--output", str(out_file), "--quiet"])
    assert exit_code == 0
    assert out_file.exists()
    data = json.loads(out_file.read_text())
    assert data["season_code"] == "E2023"
    assert data["exclusions"]["played_games"] == 331


# ---------------------------------------------------------------------------
# Persistent local warehouse (DECISIONS.md item 84)
# ---------------------------------------------------------------------------

E2023_RELATION_ROWS = {
    "raw_game": 331,
    "raw_boxscore_player": 7_883,
    "raw_boxscore_team": 1_324,
    "raw_shot": 50_159,
    "player": 296,
    "team": 18,
    "team_season": 18,
    "lineup": 5_817,
    "lineup_stint": 13_697,
    "game_event": 172_265,
    "player_game_minutes": 7_883,
    "game_quality": 331,
    "possession": 47_460,
    "season_progress": 0,
}


def _stub_database_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    import euroleague.historical_rehearsal as hr

    monkeypatch.setattr(hr, "apply_current_migrations", lambda conn: None)
    monkeypatch.setattr(hr, "load_confirmation_raw_rows", lambda conn, cache, sc: {})
    monkeypatch.setattr(
        hr,
        "load_derived_rows",
        lambda conn, dims, evts, rem, sc, gamecodes=None: {},
    )


def test_managed_schema_keeps_schema_and_roles_only_after_success() -> None:
    """Break caught: keep mode drops a finished load, or keeps a half-loaded one."""
    connection = DummyConnection()
    kept_roles = set(rehearsal_role_names("warehouse_kept"))
    with managed_rehearsal_schema(connection, "warehouse_kept", keep=True):
        connection.roles.update(kept_roles)
    assert "warehouse_kept" in connection.schemas
    assert kept_roles <= connection.roles
    assert not any(query.startswith("DROP") for query, _ in connection.executions)

    with (
        pytest.raises(RuntimeError, match="synthetic failure"),
        managed_rehearsal_schema(connection, "warehouse_failed", keep=True),
    ):
        raise RuntimeError("synthetic failure")
    assert "warehouse_failed" not in connection.schemas


def test_season_count_reconciliation_is_exact_for_season_tables() -> None:
    """Break caught: one season's rows hide behind another season's in a shared schema."""
    assert_season_loaded_counts({"raw_game": 331, "player": 296}, {"raw_game": 331, "player": 500})

    with pytest.raises(AssertionError, match="raw_game"):
        assert_season_loaded_counts({"raw_game": 331}, {"raw_game": 662})

    # Shared tables span seasons, so a season can only require its rows to be
    # present; the exact check for them is the union check below.
    with pytest.raises(AssertionError, match="player"):
        assert_season_loaded_counts({"player": 296}, {"player": 295})


def test_shared_count_reconciliation_requires_the_exact_union() -> None:
    """Break caught: a shared dimension is double-counted or loses rows across seasons."""
    assert_shared_union_counts({"player": {"P1", "P2", "P3"}}, {"player": 3})
    with pytest.raises(AssertionError, match="player"):
        assert_shared_union_counts({"player": {"P1", "P2", "P3"}}, {"player": 4})


def test_persistent_load_refuses_public_and_existing_schemas() -> None:
    """Break caught: the persistent load overwrites public or a previous load silently."""
    connection = DummyConnection()
    cache = ResponseCache("exploration/cache")

    with pytest.raises(ValueError, match="public"):
        load_persistent_warehouse(connection, cache, ["E2023"], schema_name="public")

    connection.schemas.add("warehouse")
    with pytest.raises(RuntimeError, match="--replace"):
        load_persistent_warehouse(connection, cache, ["E2023"], schema_name="warehouse")
    assert "warehouse" in connection.schemas
    assert not any(query.startswith("DROP") for query, _ in connection.executions)


def test_persistent_load_refuses_a_non_disposable_target() -> None:
    """Break caught: the persistent load is pointed at the hosted warehouse."""
    connection = DummyConnection(database_name="postgres", port=5432)
    with pytest.raises(ConfirmationTargetError):
        load_persistent_warehouse(
            connection, ResponseCache("exploration/cache"), ["E2023"], schema_name="warehouse"
        )
    assert not any("CREATE SCHEMA" in query for query, _ in connection.executions)


def test_persistent_load_keeps_schema_and_reports_each_season(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: the load drops its schema, or reports no per-season exclusions."""
    if not Path("exploration/cache/E2023/schedule.json").exists():
        pytest.skip("E2023 cache not present")
    _stub_database_writes(monkeypatch)
    connection = DummyConnection()
    connection.relation_rows = dict(E2023_RELATION_ROWS)

    result = load_persistent_warehouse(
        connection,
        ResponseCache("exploration/cache"),
        ["E2023"],
        schema_name="warehouse",
        reader_password="not-a-real-password",
        progress=lambda message: None,
    )

    assert "warehouse" in connection.schemas
    assert not any(query.startswith("DROP") for query, _ in connection.executions)
    [season] = result.seasons
    assert season.season_code == "E2023"
    assert season.exclusions.loaded_games == 331
    assert season.exclusions.excluded_games == 25
    assert len(season.excluded_games) == 25
    assert all(game["reasons"] for game in season.excluded_games)
    assert season.timings.total_seconds > 0
    reader_role = rehearsal_role_names("warehouse")[0]
    assert result.reader_role == reader_role
    assert any(
        query.startswith(f'ALTER ROLE "{reader_role}" SET search_path TO "warehouse"')
        for query, _ in connection.executions
    )
    assert "not-a-real-password" not in result.to_json()


def test_persistent_load_replace_drops_only_the_named_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: --replace leaves stale seasons behind or drops a neighbouring schema."""
    if not Path("exploration/cache/E2023/schedule.json").exists():
        pytest.skip("E2023 cache not present")
    _stub_database_writes(monkeypatch)
    connection = DummyConnection()
    connection.relation_rows = dict(E2023_RELATION_ROWS)
    connection.schemas.update({"warehouse", "space_e2025"})

    load_persistent_warehouse(
        connection,
        ResponseCache("exploration/cache"),
        ["E2023"],
        schema_name="warehouse",
        replace=True,
        progress=lambda message: None,
    )

    drops = [query for query, _ in connection.executions if query.startswith("DROP SCHEMA")]
    assert drops == ['DROP SCHEMA "warehouse" CASCADE']
    assert {"warehouse", "space_e2025"} <= connection.schemas


def test_rehearsal_cli_keep_schema_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break caught: --keep-schema is accepted but the rehearsal still drops the schema."""
    if not Path("exploration/cache/E2023/schedule.json").exists():
        pytest.skip("E2023 cache not present")
    script = _load_script()
    assert script.parse_arguments(["--keep-schema"]).keep_schema
    assert not script.parse_arguments([]).keep_schema

    connection = DummyConnection()
    connection.relation_rows = dict(E2023_RELATION_ROWS)
    monkeypatch.setattr(script.psycopg, "connect", lambda *args, **kwargs: connection)
    _stub_database_writes(monkeypatch)
    assert script.main(["--keep-schema", "--quiet"]) == 0
    assert any(name.startswith("rehearse_e2023_") for name in connection.schemas)
    assert not any(query.startswith("DROP SCHEMA") for query, _ in connection.executions)


def test_game_skipping_cache_hides_named_games_everywhere(tmp_path: Path) -> None:
    """Break caught: a skipped game still reaches one builder while the others drop it."""
    season_root = tmp_path / "E2020"
    season_root.mkdir()
    (season_root / "schedule.json").write_text(
        json.dumps({"data": [{"gameCode": 1, "played": True}, {"gameCode": 2, "played": True}]})
    )
    for endpoint in ("Boxscore", "PlaybyPlay", "Points"):
        (season_root / endpoint).mkdir()
        for gamecode in (1, 2):
            (season_root / endpoint / f"{gamecode}.json").write_text("{}")

    cache = GameSkippingCache(tmp_path, {"E2020": {2}})

    assert [g["gameCode"] for g in cache.read_schedule_json("E2020")["data"]] == [1]
    assert cache.gamecodes("E2020", "Boxscore") == [1]
    assert not cache.exists("E2020", "PlaybyPlay", 2)
    with pytest.raises(LookupError, match="skipped"):
        cache.read_json("E2020", "Boxscore", 2)
    assert verify_cache_integrity(cache, "E2020").played_games == 1
    # Another season is untouched, and the unfiltered schedule is still reachable.
    assert cache.skipped("E2021") == set()
    assert len(cache.full_schedule_data("E2020")) == 2


def test_persistent_load_reports_skipped_games_and_the_true_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a skipped game silently vanishes from the per-season report."""
    if not Path("exploration/cache/E2023/schedule.json").exists():
        pytest.skip("E2023 cache not present")
    _stub_database_writes(monkeypatch)
    rows = dict(E2023_RELATION_ROWS)
    connection = DummyConnection()
    # Built counts for E2023 without game 1 are not known here, so the row
    # reconciliation is stubbed; the report is what this test is about.
    import euroleague.historical_rehearsal as hr

    monkeypatch.setattr(hr, "assert_season_loaded_counts", lambda expected, observed: None)
    monkeypatch.setattr(hr, "assert_shared_union_counts", lambda expected, observed: None)
    connection.relation_rows = rows

    result = load_persistent_warehouse(
        connection,
        GameSkippingCache("exploration/cache", {"E2023": {1}}),
        ["E2023"],
        schema_name="warehouse",
        progress=lambda message: None,
    )

    [season] = result.seasons
    assert season.skipped_games == [1]
    assert season.exclusions.scheduled_games == 331
    assert season.exclusions.loaded_games == 330


LOAD_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "load_local_warehouse.py"


def test_local_warehouse_cli_parses_seasons_and_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Break caught: the multi-season command ignores its seasons or writes no evidence."""
    if not Path("exploration/cache/E2023/schedule.json").exists():
        pytest.skip("E2023 cache not present")
    spec = importlib.util.spec_from_file_location(
        "load_local_warehouse_under_test", LOAD_SCRIPT_PATH
    )
    script = importlib.util.module_from_spec(spec)
    sys.modules["load_local_warehouse_under_test"] = script
    spec.loader.exec_module(script)

    opts = script.parse_arguments(["E2020", "e2021", "--schema", "fantasy"])
    assert opts.seasons == ["E2020", "E2021"]
    assert opts.schema == "fantasy"
    assert not opts.replace
    assert opts.skip_games == {}

    skipping = script.parse_arguments(
        ["E2020", "E2022", "--skip-game", "E2020:16", "--skip-game", "E2020:127,273"]
    )
    assert skipping.skip_games == {"E2020": {16, 127, 273}}
    with pytest.raises(SystemExit):
        script.parse_arguments(["E2020", "--skip-game", "E2021:3"])  # season not loaded
    with pytest.raises(SystemExit):
        script.parse_arguments(["E2020", "--skip-game", "E2020"])  # no gamecode

    connection = DummyConnection()
    connection.relation_rows = dict(E2023_RELATION_ROWS)
    monkeypatch.setattr(script.psycopg, "connect", lambda *args, **kwargs: connection)
    monkeypatch.setattr(
        script,
        "load_test_database_settings",
        lambda: SimpleNamespace(
            host="localhost", port=5433, database="euroleague_test", url=lambda: "unused"
        ),
    )
    _stub_database_writes(monkeypatch)
    out_file = tmp_path / "local_warehouse.json"
    assert script.main(["E2023", "--quiet", "--output", str(out_file)]) == 0
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["schema_name"] == "warehouse"
    assert data["seasons"][0]["exclusions"]["excluded_games"] == 25
