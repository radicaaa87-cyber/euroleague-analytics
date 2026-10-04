"""The assembled ASGI application: it starts, it reports health, and it answers its host.

The host check here is not pedantry. The SDK enables DNS-rebinding protection by
default and allows no hostname, so a deployed server that does not name its own
public host refuses every request with HTTP 421 - a status almost nothing
surfaces usefully, and which looks like the server being down rather than
misconfigured. This was found by a smoke test, not by reading the code.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from starlette.testclient import TestClient

import euroleague.mcp.http_app as http_app
from euroleague.mcp.http_app import (
    ANONYMOUS_SUBJECT,
    _call_record,
    auth_from_env,
    build_app,
    caller_subject,
    run_with_row_budget,
)
from euroleague.mcp.ratelimit import RequestCap

HOST = "testserver"


def _runner(query: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("no test in this module should reach the database")


def test_the_app_builds() -> None:
    assert build_app(_runner) is not None


def test_healthz_reports_status_version_and_tool_count() -> None:
    with TestClient(build_app(_runner, allowed_hosts=[HOST])) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["name"] == "euroleague-analytics"
    assert body["tools"] == 17
    assert body["version"]


def test_healthz_names_a_version_so_a_report_can_identify_what_served_it() -> None:
    from euroleague.mcp.identity import SERVER_INFO

    with TestClient(build_app(_runner, allowed_hosts=[HOST])) as client:
        response = client.get("/healthz")
    assert response.json()["version"] == SERVER_INFO["version"]


def test_a_named_host_is_not_rejected() -> None:
    """The 421 regression: naming the host must make the MCP endpoint reachable."""
    with TestClient(build_app(_runner, allowed_hosts=[HOST])) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert response.status_code != 421, "the server refused its own configured hostname"


def test_an_unnamed_host_is_refused() -> None:
    """Protection stays on: a host we did not name must not be served."""
    with TestClient(build_app(_runner, allowed_hosts=["euroleague.example.com"])) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert response.status_code == 421


@pytest.mark.parametrize("path", ["/healthz"])
def test_health_needs_no_authentication(path: str) -> None:
    """A platform health check cannot present a token, so this route must stay open."""
    with TestClient(build_app(_runner, allowed_hosts=[HOST])) as client:
        assert client.get(path).status_code == 200


def test_the_app_accepts_a_request_cap() -> None:
    assert build_app(_runner, allowed_hosts=[HOST], cap=RequestCap()) is not None


def test_an_unidentified_caller_falls_back_to_one_shared_bucket() -> None:
    """Outside a request there is no token, and an unknown caller must not get its own
    allowance - that would let the cap be sidestepped by not authenticating."""
    assert caller_subject() == ANONYMOUS_SUBJECT


def test_a_logged_tool_call_records_no_arguments_and_no_payload() -> None:
    """The arguments name the players and teams a tester asked about.

    Logging them would turn an operational record into a record of what each
    person was researching, which is not what it is for.
    """
    record = _call_record("el_get_game", "ok", time.monotonic())
    assert set(record) == {"tool", "outcome", "ms"}
    assert record["tool"] == "el_get_game"
    assert record["outcome"] == "ok"


def test_a_logged_tool_call_reports_a_non_negative_duration() -> None:
    assert _call_record("el_get_game", "ok", time.monotonic())["ms"] >= 0


def test_a_failed_call_logs_the_exception_type_and_not_its_message() -> None:
    """Query errors quote the caller's arguments back at them.

    `queries.py` raises messages like "must be true or false, not {value!r}",
    so logging the message would record what a tester was asking about. The
    class name says what went wrong without saying what was asked.
    """
    record = _call_record("el_get_game", "error", time.monotonic(), "ValueError")
    assert record["error_type"] == "ValueError"
    assert set(record) == {"tool", "outcome", "ms", "error_type"}


def test_a_successful_call_records_no_error_type() -> None:
    assert "error_type" not in _call_record("el_get_game", "ok", time.monotonic())


def test_the_http_tool_path_charges_the_callers_row_budget_after_the_query() -> None:
    calls: list[str] = []

    class Budget:
        def run(self, subject: str, query: Any) -> dict[str, Any]:
            calls.append(subject)
            response = query()
            response["row_budget"] = {"remaining_rows": 49_999}
            return response

    response = run_with_row_budget(
        Budget(),
        "client-123",
        lambda arguments: {"row_count": 1, "arguments": arguments},
        {"season": "E2024"},
    )

    assert calls == ["client-123"]
    assert response["arguments"] == {"season": "E2024"}
    assert response["row_budget"]["remaining_rows"] == 49_999


def test_an_authenticated_http_server_builds_a_durable_row_budget_from_its_identity_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    seen: list[object] = []

    def configured_store(values: object) -> object:
        seen.append(values)
        return sentinel

    monkeypatch.setattr(http_app, "postgres_usage_store_from_env", configured_store)

    _, auth_settings = auth_from_env(
        {
            "MCP_ISSUER_URL": "https://issuer.example.com",
            "MCP_RESOURCE_URL": "https://warehouse.example.com/mcp",
            "MCP_INTROSPECTION_URL": "https://issuer.example.com/introspect",
            "MCP_CLIENT_ID": "client-id",
            "MCP_CLIENT_SECRET": "client-secret",
            "MCP_USAGE_DATABASE_URL": "postgresql://el_usage_writer:writer-secret@example.com:5432/postgres",
        }
    )
    assert build_app(_runner, allowed_hosts=[HOST], auth_settings=auth_settings) is not None
    assert seen


AUTH_ENVIRONMENT = {
    "MCP_ISSUER_URL": "https://issuer.example.com",
    "MCP_RESOURCE_URL": "https://warehouse.example.com/mcp",
    "MCP_INTROSPECTION_URL": "https://issuer.example.com/introspect",
    "MCP_CLIENT_ID": "client-id",
    "MCP_CLIENT_SECRET": "client-secret",
    "MCP_USAGE_DATABASE_URL": "postgresql://el_usage_writer:writer-secret@example.com:5432/postgres",
}


def test_the_advertised_authorization_server_is_the_provider_by_default() -> None:
    """With no registration shim configured, discovery points straight at the provider."""
    _, auth_settings = auth_from_env(dict(AUTH_ENVIRONMENT))

    assert str(auth_settings.issuer_url).rstrip("/") == "https://issuer.example.com"


def test_the_advertised_authorization_server_becomes_this_server_when_the_shim_is_on() -> None:
    """A URL-only client must be sent to our registration endpoint, not the provider's.

    The provider's registration endpoint answers 400 by design. Advertising it is
    what makes ChatGPT fail before a human sees a login screen.
    """
    verifier, auth_settings = auth_from_env(
        dict(AUTH_ENVIRONMENT, MCP_OAUTH_PROXY_CLIENT_ID="shared-client-id")
    )

    assert str(auth_settings.issuer_url).rstrip("/") == "https://warehouse.example.com"
    # The tokens still come from the provider, so the check on them must not move.
    assert verifier.issuer_url == "https://issuer.example.com"
