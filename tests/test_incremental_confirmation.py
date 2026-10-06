"""Disposable-schema proof for incremental derived database writes."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import euroleague.incremental_confirmation as confirmation
from euroleague.incremental_confirmation import (
    LOCAL_CONFIRMATION_DATABASE,
    LOCAL_CONFIRMATION_PORT,
    ConfirmationTargetError,
    ProductionBaselineMismatch,
    RelationFingerprint,
    SchemaScopeError,
    assert_local_confirmation_target,
    assert_production_baseline_matches,
    assert_same_fingerprints,
    current_derived_writer,
    fingerprint_relations,
    load_confirmation_raw_rows,
    load_test_database_settings,
    managed_schema,
    measure_database_size,
    prepare_confirmation_session,
    production_baseline_fingerprints,
    run_confirmation,
    run_guarded_step,
)


def _sql_text(query) -> str:
    if hasattr(query, "as_string"):
        return query.as_string()
    return " ".join(str(query).split())


class Cursor:
    def __init__(self, connection) -> None:
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

    def fetchone(self):
        if self.last_query == "SELECT current_schema()":
            return (self.connection.current_schema,)
        if self.last_query == "SELECT current_database()":
            return (self.connection.database_name,)
        if self.last_query == "SELECT pg_database_size(current_database())":
            return (self.connection.database_sizes.pop(0),)
        if self.last_query.startswith("SELECT count(*) FROM pg_namespace"):
            return (int(self.last_params[0] in self.connection.schemas),)
        marker = "/* fingerprint:"
        if marker in self.last_query:
            name = self.last_query.split(marker, 1)[1].split(" */", 1)[0]
            return self.connection.fingerprint_answers[name]
        raise AssertionError(f"No answer configured for {self.last_query}")


class Connection:
    def __init__(
        self,
        *,
        current_schema: str | None = None,
        database_name: str = LOCAL_CONFIRMATION_DATABASE,
        port: int = LOCAL_CONFIRMATION_PORT,
        database_sizes: list[int] | None = None,
        fingerprint_answers: dict[str, tuple[int, str]] | None = None,
    ) -> None:
        self.current_schema = current_schema
        self.database_name = database_name
        self.port = port
        self.info = SimpleNamespace(host="localhost", port=port)
        self.database_sizes = list(database_sizes or [])
        self.fingerprint_answers = fingerprint_answers or {}
        self.schemas: set[str] = set()
        self.executions: list[tuple[str, object]] = []

    def cursor(self):
        return Cursor(self)

    @contextmanager
    def transaction(self):
        yield


def test_confirmation_refuses_to_write_when_current_schema_is_not_expected() -> None:
    """Break caught: a wrong search_path sends confirmation rows into public."""
    connection = Connection(current_schema="public", database_sizes=[300_000_000])
    action_called = False

    def action() -> None:
        nonlocal action_called
        action_called = True

    with pytest.raises(SchemaScopeError, match="confirm_single_test"):
        run_guarded_step(connection, "confirm_single_test", "raw load", action, [])

    assert action_called is False


@pytest.mark.parametrize(
    ("database_name", "port"),
    (("postgres", LOCAL_CONFIRMATION_PORT), (LOCAL_CONFIRMATION_DATABASE, 5432)),
)
def test_confirmation_refuses_any_target_except_local_test_database(
    database_name: str, port: int
) -> None:
    """Break caught: confirmation DDL or loads reach production or the wrong local database."""
    connection = Connection(database_name=database_name, port=port)

    with pytest.raises(ConfirmationTargetError, match="No confirmation write was attempted"):
        assert_local_confirmation_target(connection)

    assert connection.executions == [("SELECT current_database()", None)]


def test_local_confirmation_records_sizes_above_retired_production_stop() -> None:
    """Break caught: the retired 460 MB production stop aborts the disposable local run."""
    connection = Connection(database_sizes=[486_427_795])

    reading = measure_database_size(connection, "after local derived load")

    assert reading.bytes == 486_427_795


def test_confirmation_session_uses_utc_for_timezone_stable_content_hashes() -> None:
    """Break caught: timestamptz JSON renders differently across otherwise equal databases."""
    connection = Connection()

    prepare_confirmation_session(connection)

    assert connection.executions == [
        ("SELECT current_database()", None),
        ("SET TIME ZONE 'UTC'", None),
    ]


def test_confirmation_settings_use_only_the_explicit_local_test_url() -> None:
    """Break caught: the warehouse-writing confirmation resolves DATABASE_URL production."""
    settings = load_test_database_settings(
        {
            "DATABASE_URL": "postgresql://prod:secret@production.example:5432/postgres",
            "EL_TEST_DATABASE_URL": ("postgresql://local:secret@localhost:5433/euroleague_test"),
        }
    )

    assert settings.host == "localhost"
    assert settings.port == 5433
    assert settings.database == "euroleague_test"


def test_production_baseline_comparison_refuses_a_content_difference() -> None:
    """Break caught: local counts match production while persisted row content differs."""
    observed = {
        "raw_game": RelationFingerprint(330, "different-content"),
    }

    with pytest.raises(ProductionBaselineMismatch, match=r"raw_game.*checksum"):
        assert_production_baseline_matches("E2024", observed)


def test_production_baseline_uses_the_original_gate_snapshot_definitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a new checksum query is compared to constants captured another way."""
    calls: list[tuple[str, str]] = []

    def raw_snapshot(connection, season_code: str):
        calls.append(("raw", season_code))
        return {
            "raw_game": SimpleNamespace(count=330, checksum="raw-checksum"),
            "raw_api_fetch": SimpleNamespace(count=999, checksum="not-in-baseline"),
        }

    def derived_snapshot(connection, season_code: str):
        calls.append(("derived", season_code))
        return {
            "game_event": SimpleNamespace(count=176_483, checksum="event-checksum"),
            "lineup": SimpleNamespace(count=5_985, checksum="not-in-baseline"),
        }

    monkeypatch.setattr(confirmation, "warehouse_snapshot", raw_snapshot)
    monkeypatch.setattr(confirmation, "derived_snapshot", derived_snapshot)

    observed = production_baseline_fingerprints(object(), "E2024")

    assert calls == [("raw", "E2024"), ("derived", "E2024")]
    assert observed == {
        "raw_game": RelationFingerprint(330, "raw-checksum"),
        "game_event": RelationFingerprint(176_483, "event-checksum"),
    }


def test_confirmation_raw_load_includes_points_before_derived_fingerprinting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: local raw_shot stays empty and falsely disagrees with production."""
    calls: list[str] = []

    def load_raw(connection, cache, season_code: str, *, progress):
        calls.append("raw")
        # The loader reports parsed events under `events_parsed` since
        # migration 0023 removed the `raw_event` table it used to count.
        return {"events_parsed": 10}

    def load_shots(connection, cache, season_code: str, *, progress):
        calls.append("shots")
        return {"raw_shot": 4}

    monkeypatch.setattr(confirmation, "load_cached_season", load_raw)
    monkeypatch.setattr(confirmation, "load_cached_shots", load_shots)

    counts = load_confirmation_raw_rows(object(), object(), "E2024")

    assert calls == ["raw", "shots"]
    assert counts == {"events_parsed": 10, "raw_shot": 4}


def test_confirmation_checks_production_baseline_before_starting_batched_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Break caught: a real local/production mismatch is ignored while later work continues."""
    calls: list[str] = []
    fingerprints = {"game_event": RelationFingerprint(2, "same")}

    class Cache:
        def read_schedule_json(self, season_code: str):
            return {
                "data": [
                    {"gameCode": 1, "played": True},
                    {"gameCode": 2, "played": True},
                ]
            }

    @contextmanager
    def schema_context(connection, schema_name: str):
        yield

    def guarded(connection, schema_name: str, phase: str, action, readings):
        calls.append(phase)
        return action()

    def raw_rows(connection, cache, season_code: str):
        calls.append(f"raw:{season_code}")
        return {}

    def old_raw_rows(connection, cache, season_code: str, *, progress):
        calls.append(f"old-raw:{season_code}")
        return {}

    def baseline(connection, season_code: str):
        calls.append(f"baseline:{season_code}")
        return fingerprints

    def assert_baseline(season_code: str, observed):
        calls.append(f"baseline-assert:{season_code}")

    def writer(connection, dimensions, events, remaining, season_code, gamecodes):
        scope = "single" if gamecodes is None else f"batch:{list(gamecodes)}"
        calls.append(f"writer:{scope}")
        return {}

    monkeypatch.setattr(confirmation, "build_dimensions", lambda *args: object())
    monkeypatch.setattr(confirmation, "build_game_events", lambda *args: ())
    monkeypatch.setattr(confirmation, "build_remaining_rows", lambda *args: object())
    monkeypatch.setattr(confirmation, "managed_schema", schema_context)
    monkeypatch.setattr(confirmation, "run_guarded_step", guarded)
    monkeypatch.setattr(confirmation, "apply_current_migrations", lambda connection: None)
    monkeypatch.setattr(confirmation, "load_confirmation_raw_rows", raw_rows)
    monkeypatch.setattr(confirmation, "load_cached_season", old_raw_rows)
    monkeypatch.setattr(confirmation, "fingerprint_relations", lambda *args, **kwargs: fingerprints)
    monkeypatch.setattr(
        confirmation,
        "game_event_update_statistics",
        lambda *args: {"n_tup_upd": 0, "n_dead_tup": 0},
    )
    monkeypatch.setattr(
        confirmation,
        "measure_database_size",
        lambda connection, phase: confirmation.SizeReading(phase, 1),
    )
    monkeypatch.setattr(confirmation, "production_baseline_fingerprints", baseline)
    monkeypatch.setattr(confirmation, "assert_production_baseline_matches", assert_baseline)
    monkeypatch.setattr(confirmation, "_write_artifact", lambda *args: None)
    monkeypatch.setattr(confirmation, "prepare_confirmation_session", lambda connection: None)

    run_confirmation(
        object(),
        Cache(),
        "E2024",
        1,
        writer,
        tmp_path / "artifact.json",
        "testrun",
        progress=lambda _: None,
    )

    assert "old-raw:E2024" not in calls
    assert calls.index("baseline:E2024") < calls.index("E2024 batched raw load")
    assert calls.index("baseline-assert:E2024") < calls.index("E2024 batched raw load")


def test_confirmation_writer_uses_the_parent_first_option_a_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: the post-refactor gate still exercises the obsolete two-stage writer."""
    calls: list[tuple[str, object]] = []

    def option_a(connection, dimensions, events, remaining, season_code, *, gamecodes):
        calls.append(("option-a", gamecodes))
        return {"game_event": 1}

    def old_path(*args, **kwargs):
        calls.append(("old-path", None))
        return {}

    monkeypatch.setattr(confirmation, "load_derived_rows", option_a, raising=False)
    monkeypatch.setattr(confirmation, "load_phase5_base_rows", old_path, raising=False)
    monkeypatch.setattr(confirmation, "load_remaining_rows", old_path, raising=False)

    counts = current_derived_writer(
        object(),
        confirmation.DimensionRows((), (), ()),
        (),
        confirmation.RemainingDerivedRows((), (), (), (), ()),
        "E2026",
        [51],
    )

    assert counts == {"game_event": 1}
    assert calls == [("option-a", [51])]


def test_confirmation_drops_the_schema_when_a_load_callback_fails() -> None:
    """Break caught: a failed confirmation leaves a populated schema behind."""
    connection = Connection(database_sizes=[300_000_000])

    def fail() -> None:
        raise RuntimeError("load failed")

    with (
        pytest.raises(RuntimeError, match="load failed"),
        managed_schema(connection, "confirm_batched_failure"),
    ):
        run_guarded_step(
            connection,
            "confirm_batched_failure",
            "first batch",
            fail,
            [],
        )

    assert connection.schemas == set()


def test_fingerprints_use_real_primary_key_order_and_include_event_attachments() -> None:
    """Break caught: equal counts hide different persisted content or attachments."""
    expected_names = {
        "game_event",
        "lineup",
        "lineup_stint",
        "player_game_minutes",
        "game_quality",
        "possession",
        "game_event_attachment",
    }
    answers = {name: (index, f"checksum-{name}") for index, name in enumerate(expected_names, 1)}
    connection = Connection(current_schema="confirm_single_test", fingerprint_answers=answers)

    observed = fingerprint_relations(
        connection,
        "confirm_single_test",
        "E2024",
        gamecodes=[3, 1],
    )

    assert set(observed) == expected_names
    assert observed["game_event_attachment"] == RelationFingerprint(
        answers["game_event_attachment"][0],
        "checksum-game_event_attachment",
    )
    fingerprint_sql = {
        query.split("/* fingerprint:", 1)[1].split(" */", 1)[0]: query
        for query, _ in connection.executions
        if "/* fingerprint:" in query
    }
    assert "ORDER BY season_code, gamecode, ingest_index" in fingerprint_sql["game_event"]
    assert "ORDER BY lineup_id" in fingerprint_sql["lineup"]
    assert "ORDER BY season_code, gamecode, player_id" in fingerprint_sql["player_game_minutes"]
    assert "home_lineup_id" in fingerprint_sql["game_event_attachment"]
    assert "possession_index" in fingerprint_sql["game_event_attachment"]


def test_second_batch_must_not_change_first_batch_fingerprints() -> None:
    """Break caught: appending later games mutates rows from the first batch."""
    before = {
        "game_event": RelationFingerprint(10, "same"),
        "possession": RelationFingerprint(4, "before"),
    }
    after = {
        "game_event": RelationFingerprint(10, "same"),
        "possession": RelationFingerprint(4, "after"),
    }

    with pytest.raises(AssertionError, match="possession"):
        assert_same_fingerprints(before, after, "first batch after second batch")


def test_disposable_target_prefers_the_real_environment_over_the_env_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI has no `.env`, so the variable has to be readable from the environment.

    `DatabaseSettings.from_env` already states the rule this follows: a real
    environment variable always wins over the file, because in CI the value
    comes from the workflow and a stray `.env` must never override it. Without
    this, `scripts/migration_gate.py` cannot run anywhere but a developer's
    machine, which is why the 0013 rehearsal was done by hand.
    """
    monkeypatch.setattr(
        "euroleague.incremental_confirmation.load_env_file",
        lambda: {"EL_TEST_DATABASE_URL": "postgresql://f:f@localhost:5433/euroleague_test"},
    )
    monkeypatch.setenv("EL_TEST_DATABASE_URL", "postgresql://e:e@127.0.0.1:5433/euroleague_test")

    settings = load_test_database_settings()

    assert settings.host == "127.0.0.1", "the .env file overrode the environment"


def test_disposable_target_still_refuses_production_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading the environment must not become a way around the port and name check.

    This is the failure the check exists for: a workflow that sets the variable
    from the wrong secret would otherwise point an up/down/up/down cycle at the
    real warehouse, and that cycle ends in `drop`.
    """
    monkeypatch.setattr("euroleague.incremental_confirmation.load_env_file", dict)
    # A well-formed production pooler URL, username included, so that the
    # refusal below comes from the database and port check rather than from the
    # pooler username validation, which would fire first on a malformed one.
    monkeypatch.setenv(
        "EL_TEST_DATABASE_URL",
        "postgresql://postgres.pctiewdpstnwcutrvegu:secret"
        "@aws-0-eu-central-1.pooler.supabase.com:5432/postgres",
    )

    with pytest.raises(ValueError, match="euroleague_test"):
        load_test_database_settings()
