"""Tests for MCP connection lifecycle, error recovery, and query execution."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest

from euroleague.mcp import db as mcp_db
from euroleague.mcp.db import (
    READ_ONLY_STATEMENT,
    ReadOnlyConnectionManager,
    ReadOnlyEnforcementError,
)
from euroleague.mcp.identity import IDENTITY
from euroleague.mcp.protocol import Tool, handle_message, serve
from euroleague.mcp.tools import build_registry


class TrackingCursor:
    """Cursor double that records execution and tracks enter/exit."""

    def __init__(
        self, answer: dict[str, Any] | None = None, raise_on_execute: Exception | None = None
    ) -> None:
        self.answer = answer if answer is not None else {"rows": []}
        self.raise_on_execute = raise_on_execute
        self.executed_statements: list[tuple[str, Any]] = []
        self.closed = False
        self.entered = False

    def __enter__(self) -> TrackingCursor:
        self.entered = True
        return self

    def __exit__(self, *args: Any) -> None:
        self.closed = True

    def execute(self, statement: str, params: Any = ()) -> None:
        self.executed_statements.append((statement, params))
        if self.raise_on_execute:
            raise self.raise_on_execute

    def fetchone(self) -> tuple[str]:
        return ("on",)

    def fetchall(self) -> list[tuple]:
        return []


class TrackingConnection:
    """Connection double tracking cursor generation and closure."""

    def __init__(
        self,
        cursors: list[TrackingCursor] | None = None,
        default_answer: dict[str, Any] | None = None,
        raise_on_cursor: Exception | None = None,
    ) -> None:
        self.cursors = list(cursors or [])
        self.default_answer = default_answer if default_answer is not None else {"rows": []}
        self.raise_on_cursor = raise_on_cursor
        self.created_cursors: list[TrackingCursor] = []
        self.closed = False
        self.close_count = 0

    def cursor(self) -> TrackingCursor:
        if self.closed:
            raise psycopg.InterfaceError("connection already closed")
        if self.raise_on_cursor:
            raise self.raise_on_cursor
        cur = self.cursors.pop(0) if self.cursors else TrackingCursor(answer=self.default_answer)
        self.created_cursors.append(cur)
        return cur

    def close(self) -> None:
        self.closed = True
        self.close_count += 1


class FakeSettings:
    """Settings double providing the database URL."""

    host: str = "test.invalid"
    port: int = 5432

    def url(self) -> str:
        return "postgresql://test.invalid/warehouse"


# --- Requirement 1: Constructing manager & registry does not connect ---


def test_constructing_manager_and_registry_does_not_open_database_connection() -> None:
    factory = MagicMock(
        side_effect=AssertionError("Connection factory must not be called at build time")
    )
    manager = ReadOnlyConnectionManager(factory)
    registry = build_registry(manager.run)

    assert factory.call_count == 0
    assert len(registry) == 17
    assert "el_get_possessions" in registry


# --- Requirement 2: Connection reused across successful calls, new cursor per call ---


def test_connection_reused_across_multiple_successful_calls() -> None:
    conn = TrackingConnection()
    factory = MagicMock(return_value=conn)
    manager = ReadOnlyConnectionManager(factory)

    def query_one(cursor: Any, args: dict[str, Any]) -> dict[str, Any]:
        return {"query": "one", "args": args}

    def query_two(cursor: Any, args: dict[str, Any]) -> dict[str, Any]:
        return {"query": "two", "args": args}

    res1 = manager.run(query_one, {"a": 1})
    assert res1 == {"query": "one", "args": {"a": 1}}
    assert factory.call_count == 1
    assert len(conn.created_cursors) == 1
    assert conn.created_cursors[0].closed is True
    assert conn.closed is False

    res2 = manager.run(query_two, {"b": 2})
    assert res2 == {"query": "two", "args": {"b": 2}}
    assert factory.call_count == 1  # Reused!
    assert len(conn.created_cursors) == 2
    assert conn.created_cursors[1].closed is True
    assert conn.closed is False


# --- Requirement 3: Manager shutdown closes connection once, repeated close harmless ---


def test_manager_close_is_idempotent() -> None:
    conn = TrackingConnection()
    factory = MagicMock(return_value=conn)
    manager = ReadOnlyConnectionManager(factory)

    manager.run(lambda cur, args: {"ok": True}, {})
    assert conn.closed is False
    assert conn.close_count == 0

    manager.close()
    assert conn.closed is True
    assert conn.close_count == 1

    manager.close()  # Repeated close
    assert conn.close_count == 1


# --- Requirement 4: Entry point closes manager on EOF and on exception ---


def _load_entry_point() -> Any:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "mcp_server.py"
    spec = importlib.util.spec_from_file_location("mcp_server_entry_lifecycle", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_entry_point_closes_manager_on_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp_server = _load_entry_point()

    conn = TrackingConnection()
    manager = ReadOnlyConnectionManager(lambda: conn)
    close_mock = MagicMock(wraps=manager.close)
    manager.close = close_mock

    monkeypatch.setattr(mcp_server, "ReadOnlyConnectionManager", lambda f: manager)
    monkeypatch.setattr(mcp_server.DatabaseSettings, "from_env", lambda: FakeSettings())

    stdin = io.StringIO("")  # EOF immediately
    stdout = io.StringIO()
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stdout", stdout)

    code = mcp_server.main()
    assert code == 0
    assert close_mock.call_count == 1


def test_entry_point_closes_manager_on_serve_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp_server = _load_entry_point()

    conn = TrackingConnection()
    manager = ReadOnlyConnectionManager(lambda: conn)
    close_mock = MagicMock(wraps=manager.close)
    manager.close = close_mock

    monkeypatch.setattr(mcp_server, "ReadOnlyConnectionManager", lambda f: manager)
    monkeypatch.setattr(mcp_server.DatabaseSettings, "from_env", lambda: FakeSettings())

    def explode_serve(*args: Any) -> None:
        raise RuntimeError("simulated server crash")

    monkeypatch.setattr(mcp_server, "serve", explode_serve)

    with pytest.raises(RuntimeError, match="simulated server crash"):
        mcp_server.main()

    assert close_mock.call_count == 1


# --- Requirement 5 & 7: First OperationalError causes one replacement connection & retry ---


def test_operational_error_causes_replacement_connection_and_retry() -> None:
    broken_cursor = TrackingCursor(
        raise_on_execute=psycopg.OperationalError("server closed the connection unexpectedly")
    )
    conn1 = TrackingConnection(cursors=[broken_cursor])
    conn2 = TrackingConnection(default_answer={"recovered": True})

    connections = [conn1, conn2]
    factory = MagicMock(side_effect=lambda: connections.pop(0))
    manager = ReadOnlyConnectionManager(factory)

    def query(cursor: Any, args: dict[str, Any]) -> dict[str, Any]:
        cursor.execute("SELECT 1")
        return cursor.answer

    result = manager.run(query, {})
    assert result == {"recovered": True}
    assert factory.call_count == 2
    assert conn1.closed is True
    assert conn2.closed is False
    assert len(conn2.created_cursors) == 1


# --- Requirement 6: First InterfaceError causes replacement connection & retry ---


def test_interface_error_causes_replacement_connection_and_retry() -> None:
    broken_cursor = TrackingCursor(
        raise_on_execute=psycopg.InterfaceError("connection already closed")
    )
    conn1 = TrackingConnection(cursors=[broken_cursor])
    conn2 = TrackingConnection(default_answer={"recovered": True})

    connections = [conn1, conn2]
    factory = MagicMock(side_effect=lambda: connections.pop(0))
    manager = ReadOnlyConnectionManager(factory)

    def query(cursor: Any, args: dict[str, Any]) -> dict[str, Any]:
        cursor.execute("SELECT 1")
        return cursor.answer

    result = manager.run(query, {})
    assert result == {"recovered": True}
    assert factory.call_count == 2
    assert conn1.closed is True
    assert conn2.closed is False


# --- Requirement 8: Second retryable failure propagates after two attempts and clears cache ---


def test_second_retryable_failure_propagates_after_two_attempts_and_clears_cache() -> None:
    broken_cursor1 = TrackingCursor(
        raise_on_execute=psycopg.OperationalError("broken connection 1")
    )
    conn1 = TrackingConnection(cursors=[broken_cursor1])

    broken_cursor2 = TrackingCursor(
        raise_on_execute=psycopg.OperationalError("broken connection 2")
    )
    conn2 = TrackingConnection(cursors=[broken_cursor2])

    connections = [conn1, conn2]
    factory = MagicMock(side_effect=lambda: connections.pop(0))
    manager = ReadOnlyConnectionManager(factory)

    def query(cursor: Any, args: dict[str, Any]) -> dict[str, Any]:
        cursor.execute("SELECT 1")
        return cursor.answer

    with pytest.raises(psycopg.OperationalError, match="broken connection 2"):
        manager.run(query, {})

    assert factory.call_count == 2
    assert conn1.closed is True
    assert conn2.closed is True
    assert manager._connection is None


# --- Requirement 9: Non-connection exception is attempted once and does not retry ---


def test_non_connection_exception_does_not_retry() -> None:
    conn = TrackingConnection()
    factory = MagicMock(return_value=conn)
    manager = ReadOnlyConnectionManager(factory)

    def bad_query(cursor: Any, args: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("Invalid filter parameter")

    with pytest.raises(ValueError, match="Invalid filter parameter"):
        manager.run(bad_query, {})

    assert factory.call_count == 1
    assert len(conn.created_cursors) == 1
    assert conn.closed is False  # Connection stays cached and healthy


def test_programming_error_does_not_retry() -> None:
    broken_cursor = TrackingCursor(raise_on_execute=psycopg.ProgrammingError("syntax error in SQL"))
    conn = TrackingConnection(cursors=[broken_cursor])
    factory = MagicMock(return_value=conn)
    manager = ReadOnlyConnectionManager(factory)

    def query(cursor: Any, args: dict[str, Any]) -> dict[str, Any]:
        cursor.execute("BAD SQL")
        return {}

    with pytest.raises(psycopg.ProgrammingError, match="syntax error"):
        manager.run(query, {})

    assert factory.call_count == 1
    assert len(conn.created_cursors) == 1


# --- Requirement 10: connect passes autocommit=True, prepare_threshold=None, verifies read-only ---


def test_connect_passes_autocommit_and_prepare_threshold_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor_statements: list[str] = []

    class MockCursor:
        def __enter__(self) -> MockCursor:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def execute(self, stmt: str) -> None:
            cursor_statements.append(stmt)

        def fetchone(self) -> tuple[str]:
            return ("on",)

    class MockConn:
        def __init__(self) -> None:
            self.closed = False

        def cursor(self) -> MockCursor:
            return MockCursor()

        def close(self) -> None:
            self.closed = True

    calls: list[dict[str, Any]] = []

    def mock_connect(url: str, **kwargs: Any) -> MockConn:
        calls.append({"url": url, **kwargs})
        return MockConn()

    monkeypatch.setattr(mcp_db.psycopg, "connect", mock_connect)

    connection = mcp_db.connect(FakeSettings())
    assert connection.closed is False
    assert len(calls) == 1
    assert calls[0] == {
        "url": "postgresql://test.invalid/warehouse",
        "autocommit": True,
        "prepare_threshold": None,
    }
    assert cursor_statements == [READ_ONLY_STATEMENT, "show transaction_read_only"]


def test_connect_raises_when_transaction_read_only_is_not_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MockCursor:
        def __enter__(self) -> MockCursor:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def execute(self, stmt: str) -> None:
            pass

        def fetchone(self) -> tuple[str]:
            return ("off",)

    class MockConn:
        def __init__(self) -> None:
            self.closed = False

        def cursor(self) -> MockCursor:
            return MockCursor()

        def close(self) -> None:
            self.closed = True

    mock_conn = MockConn()
    monkeypatch.setattr(mcp_db.psycopg, "connect", lambda url, **kwargs: mock_conn)

    with pytest.raises(ReadOnlyEnforcementError, match="transaction_read_only is 'off'"):
        mcp_db.connect(FakeSettings())

    assert mock_conn.closed is True


# --- Requirement 11: initialize and tools/list do not connect, first tools/call does ---


def test_initialize_and_tools_list_do_not_connect_first_call_does() -> None:
    conn = TrackingConnection(default_answer={"items": ["E2024"]})
    factory = MagicMock(return_value=conn)
    manager = ReadOnlyConnectionManager(factory)

    def dummy_query(cursor: Any, args: dict[str, Any]) -> dict[str, Any]:
        return {"result": "success"}

    tools = {
        "el_dummy": Tool(
            name="el_dummy",
            description="Dummy tool for lifecycle verification.",
            input_schema={"type": "object", "properties": {}},
            handler=lambda args: manager.run(dummy_query, args),
        )
    }

    # 1. initialize
    reply1 = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        },
        tools,
        IDENTITY,
    )
    assert reply1 is not None and "result" in reply1
    assert factory.call_count == 0

    # 2. tools/list
    reply2 = handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        tools,
        IDENTITY,
    )
    assert reply2 is not None and "result" in reply2
    assert factory.call_count == 0

    # 3. tools/call
    reply3 = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "el_dummy", "arguments": {}},
        },
        tools,
        IDENTITY,
    )
    assert reply3 is not None and "result" in reply3
    assert factory.call_count == 1
    assert conn.closed is False


# --- Requirement 12: JSON-RPC response formats and pure stdout ---


def test_json_rpc_preserves_content_structured_content_and_pure_stdout() -> None:
    conn = TrackingConnection(default_answer={"possessions": 42})
    factory = MagicMock(return_value=conn)
    manager = ReadOnlyConnectionManager(factory)

    def dummy_query(cursor: Any, args: dict[str, Any]) -> dict[str, Any]:
        return {"count": 42, "season": args.get("season")}

    tools = {
        "el_poss": Tool(
            name="el_poss",
            description="Possession tool.",
            input_schema={
                "type": "object",
                "properties": {"season": {"type": "string"}},
                "required": ["season"],
            },
            handler=lambda args: manager.run(dummy_query, args),
        )
    }

    stdin = io.StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "el_poss", "arguments": {"season": "E2024"}},
            }
        )
        + "\n"
    )
    stdout = io.StringIO()

    serve(stdin, stdout, tools, IDENTITY)

    lines = [line.strip() for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(lines) == 2

    # Verify each line is valid JSON
    r1 = json.loads(lines[0])
    assert r1["id"] == 1
    assert "protocolVersion" in r1["result"]

    r2 = json.loads(lines[1])
    assert r2["id"] == 2
    assert r2["result"]["isError"] is False
    assert r2["result"]["structuredContent"] == {"count": 42, "season": "E2024"}
    assert json.loads(r2["result"]["content"][0]["text"]) == {"count": 42, "season": "E2024"}


# --- Requirement 13: Python 3.14 version guard ---


def test_mcp_server_entry_point_parses_on_python_39() -> None:
    """ast.parse on Python 3.9 syntax level proves the file can deliver its own
    message on older Pythons."""
    import ast

    path = Path(__file__).resolve().parent.parent / "scripts" / "mcp_server.py"
    source = path.read_text(encoding="utf-8")
    parsed = ast.parse(source, feature_version=(3, 9))
    assert parsed is not None


def test_check_python_version_returns_message_for_older_versions() -> None:
    mcp_server = _load_entry_point()
    msg = mcp_server.check_python_version((3, 13, 0))
    assert msg is not None
    assert "3.14" in msg
    assert "SyntaxError" in msg


def test_check_python_version_returns_none_for_current_and_compatible_versions() -> None:
    import sys

    mcp_server = _load_entry_point()
    assert mcp_server.check_python_version(sys.version_info) is None
    assert mcp_server.check_python_version((3, 14, 0)) is None
    assert mcp_server.check_python_version((3, 15, 0)) is None
