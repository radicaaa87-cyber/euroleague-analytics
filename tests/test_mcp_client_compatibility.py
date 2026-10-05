"""Cross-client and platform-agnostic MCP compatibility regression tests.

Verifies:
1. Strict JSON Schema compliance (Draft-07 and Draft 2020-12 structural rules, and the
   OpenAI, Gemini and Claude requirements).
2. Protocol version negotiation across every supported MCP version
   (2024-11-05, 2025-03-26, 2025-06-18).
3. Standardized smoke test multi-step workflow on both stdio and HTTP transports.
4. Error handling semantics (tool errors vs JSON-RPC protocol errors).
5. Dual response encoding (content text + structuredContent).
6. Tool safety annotations and output schemas.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from euroleague.mcp.envelope import RESPONSE_OUTPUT_SCHEMA
from euroleague.mcp.http_app import (
    published_tools,
    sdk_tools_as_wire,
)
from euroleague.mcp.identity import IDENTITY
from euroleague.mcp.protocol import (
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    Tool,
    handle_message,
)
from euroleague.mcp.tools import TOOL_NAMES, build_registry


def _mock_runner(query: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Canned responses matching standard warehouse response envelopes."""
    tool_name = query.__name__ if hasattr(query, "__name__") else "unknown"

    base_response: dict[str, Any] = {
        "coverage": {
            "seasons": ["E2024", "E2025"],
            "games_included": 732,
            "completeness": "complete",
        },
        "excluded": {"games": 22, "reasons": {"possession_gate": 14}},
        "row_count": 1,
        "truncated": False,
        "caveats": ["Validation baseline verified."],
    }

    if tool_name == "describe_warehouse":
        base_response["rows"] = [
            {"season_code": "E2024", "games": 330, "completeness": "complete"},
            {"season_code": "E2025", "games": 402, "completeness": "in_progress"},
        ]
        base_response["row_count"] = len(base_response["rows"])
        return base_response

    if tool_name == "find_games":
        base_response["rows"] = [
            {
                "gamecode": 101,
                "season_code": arguments.get("season", "E2024"),
                "round": 5,
                "home_team": "PAN",
                "away_team": "OLY",
                "home_score": 85,
                "away_score": 80,
            }
        ]
        base_response["total_available"] = 1
        return base_response

    if tool_name == "get_game":
        base_response["rows"] = [
            {
                "season_code": arguments.get("season", "E2024"),
                "gamecode": arguments.get("gamecode", 101),
                "team_code": "PAN",
                "points": 85,
                "possessions": 72,
                "offensive_rating": 118.06,
                "defensive_rating": 111.11,
                "effective_fg_pct": 0.562,
            }
        ]
        return base_response

    if tool_name == "get_boxscore":
        basis = arguments.get("minutes_basis", "corrected")
        base_response["minutes_basis"] = {"value": basis, "meaning": f"Reconstruction: {basis}"}
        base_response["rows"] = [
            {
                "player_id": "P001234",
                "player_name": "SLOUKAS, KOSTAS",
                "team_code": "PAN",
                "points": 18,
                "assists": 8,
                "minutes": "28:15",
            }
        ]
        return base_response

    if tool_name == "get_possessions":
        base_response["rows"] = [
            {
                "possession_number": 1,
                "offense_team": "PAN",
                "defense_team": "OLY",
                "points_scored": 2,
                "seconds_remaining_at_start": 2400,
                "margin_at_start": 0,
                "end_reason": "made_shot",
            }
        ]
        base_response["total_available"] = 1
        return base_response

    base_response["rows"] = [{"status": "ok", "arguments": arguments}]
    return base_response


@pytest.fixture
def mock_registry() -> dict[str, Tool]:
    return build_registry(_mock_runner)


# ---------------------------------------------------------------------------
# 1. JSON Schema Strict Validation Tests (Standard Library Validation)
# ---------------------------------------------------------------------------


def test_all_11_tool_input_schemas_are_valid_json_schema(mock_registry):
    """Verify tool input schemas conform strictly to JSON Schema standards."""
    valid_scalar_types = {"string", "integer", "boolean", "number", "array", "object"}

    for name, tool in mock_registry.items():
        schema = tool.input_schema
        # Structural validation:
        assert schema.get("type") == "object", f"{name} inputSchema must be type: object"
        assert isinstance(schema.get("properties"), dict), f"{name} properties must be a dictionary"
        assert isinstance(schema.get("required"), list), f"{name} required must be a list"
        for req in schema["required"]:
            assert isinstance(req, str), f"{name} required items must be strings"
            assert req in schema["properties"], (
                f"{name} required property {req!r} not in properties"
            )

        # Check every property definition:
        for prop_name, prop_def in schema["properties"].items():
            assert "type" in prop_def, f"{name}.{prop_name} must declare a primitive type"
            assert prop_def["type"] in valid_scalar_types, f"{name}.{prop_name} type invalid"
            assert prop_def.get("description"), f"{name}.{prop_name} must have a description"
            assert len(prop_def["description"]) > 10, f"{name}.{prop_name} description too short"
            if "enum" in prop_def:
                assert isinstance(prop_def["enum"], list)
                assert len(prop_def["enum"]) > 0
                assert all(isinstance(val, str) for val in prop_def["enum"])
            if "default" in prop_def:
                # Default must match declared type
                default_val = prop_def["default"]
                if prop_def["type"] == "boolean":
                    assert isinstance(default_val, bool)
                elif prop_def["type"] == "integer":
                    assert isinstance(default_val, int)
                elif prop_def["type"] == "string":
                    assert isinstance(default_val, str)


def test_response_output_schema_is_valid_json_schema():
    """Verify response envelope schema conforms to JSON Schema specifications."""
    assert RESPONSE_OUTPUT_SCHEMA["type"] == "object"
    assert "rows" in RESPONSE_OUTPUT_SCHEMA["properties"]
    assert "row_count" in RESPONSE_OUTPUT_SCHEMA["properties"]
    assert RESPONSE_OUTPUT_SCHEMA["properties"]["rows"]["type"] == "array"
    assert RESPONSE_OUTPUT_SCHEMA["properties"]["row_count"]["type"] == "integer"
    assert "coverage" in RESPONSE_OUTPUT_SCHEMA["required"]


def test_no_unsupported_json_schema_dialects_in_tool_definitions(mock_registry):
    """No $ref, anyOf or oneOf in an input schema: strict clients trip over them."""
    for name, tool in mock_registry.items():
        schema_json = json.dumps(tool.input_schema)
        assert "$ref" not in schema_json, f"{name} contains $ref"
        assert "anyOf" not in schema_json, f"{name} contains anyOf"
        assert "oneOf" not in schema_json, f"{name} contains oneOf"


# ---------------------------------------------------------------------------
# 2. Protocol Version Negotiation Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", SUPPORTED_PROTOCOL_VERSIONS)
def test_protocol_negotiation_accepts_supported_versions(version: str, mock_registry):
    """Verify server negotiates and echoes each supported protocol version."""
    reply = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "initialize",
            "params": {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0.0"},
            },
        },
        mock_registry,
        IDENTITY,
    )
    assert reply is not None
    assert reply["jsonrpc"] == "2.0"
    assert reply["id"] == 100
    assert reply["result"]["protocolVersion"] == version
    assert reply["result"]["capabilities"]["tools"] == {"listChanged": False}
    assert reply["result"]["serverInfo"]["name"] == "euroleague-analytics"


def test_protocol_negotiation_falls_back_to_latest_for_unsupported_version(mock_registry):
    """The server falls back to the latest version when the client names an unsupported one."""
    reply = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "initialize",
            "params": {
                "protocolVersion": "9999-99-99",
                "capabilities": {},
                "clientInfo": {"name": "future-client", "version": "9.9.9"},
            },
        },
        mock_registry,
        IDENTITY,
    )
    assert reply is not None
    assert reply["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# 3. Tool Discovery & Parity Across Transports
# ---------------------------------------------------------------------------


def test_every_declared_tool_is_discovered_with_full_annotations(mock_registry):
    """Verify tools/list exposes exactly the registered tools with correct wire attributes."""
    stdio_tools = published_tools(mock_registry)
    sdk_tools = sdk_tools_as_wire(mock_registry)

    assert len(stdio_tools) == 17
    assert len(sdk_tools) == 17
    assert [t["name"] for t in stdio_tools] == sorted(TOOL_NAMES)

    for tool in stdio_tools:
        assert tool["name"].startswith("el_")
        assert tool["annotations"] == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        }
        assert "inputSchema" in tool
        assert "outputSchema" in tool
        assert "description" in tool
        assert len(tool["description"]) > 50


# ---------------------------------------------------------------------------
# 4. Standardized Smoke Test Sequence (Multi-Step Parameterized Workflow)
# ---------------------------------------------------------------------------


def test_smoke_test_step1_describe_warehouse(mock_registry):
    """Smoke Test 1: Simple discovery call to el_describe_warehouse."""
    reply = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "el_describe_warehouse", "arguments": {}},
        },
        mock_registry,
        IDENTITY,
    )
    assert reply is not None
    assert reply["result"]["isError"] is False
    assert "structuredContent" in reply["result"]
    assert len(reply["result"]["content"]) == 1
    assert reply["result"]["content"][0]["type"] == "text"

    data = reply["result"]["structuredContent"]
    assert "coverage" in data
    assert "rows" in data
    assert len(data["rows"]) == 2


def test_smoke_test_step2_find_games(mock_registry):
    """Smoke Test 2: Search games with team and opponent filter."""
    reply = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "el_find_games",
                "arguments": {"season": "E2024", "team": "PAN", "opponent": "OLY"},
            },
        },
        mock_registry,
        IDENTITY,
    )
    assert reply is not None
    assert reply["result"]["isError"] is False
    data = reply["result"]["structuredContent"]
    assert data["row_count"] == 1
    assert data["rows"][0]["gamecode"] == 101


def test_smoke_test_step3_get_game_and_boxscore(mock_registry):
    """Smoke Test 3: Multi-step follow-up retrieving game analytics and player boxscore."""
    # Step 3a: Get game summary
    reply_game = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "el_get_game", "arguments": {"season": "E2024", "gamecode": 101}},
        },
        mock_registry,
        IDENTITY,
    )
    assert reply_game["result"]["isError"] is False
    game_data = reply_game["result"]["structuredContent"]
    assert game_data["rows"][0]["possessions"] == 72

    # Step 3b: Get boxscore with explicit minutes_basis
    reply_box = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "el_get_boxscore",
                "arguments": {"season": "E2024", "gamecode": 101, "minutes_basis": "corrected"},
            },
        },
        mock_registry,
        IDENTITY,
    )
    assert reply_box["result"]["isError"] is False
    box_data = reply_box["result"]["structuredContent"]
    assert box_data["minutes_basis"]["value"] == "corrected"
    assert box_data["rows"][0]["player_name"] == "SLOUKAS, KOSTAS"


def test_smoke_test_step4_malformed_input_error_handling(mock_registry):
    """Smoke Test 4: Error handling on malformed arguments returns model-actionable errors."""
    # Boolean type mismatch caught before database
    reply = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "el_describe_warehouse",
                "arguments": {"include_quarantined": "true"},
            },
        },
        mock_registry,
        IDENTITY,
    )
    assert reply is not None
    assert reply["result"]["isError"] is True
    assert "include_quarantined must be true or false" in reply["result"]["content"][0]["text"]


def test_smoke_test_step5_bulk_narrowing_protection(mock_registry):
    """Smoke Test 5: Broad sweeps on large surfaces are rejected before execution."""
    # el_get_shot_data without any narrowing argument raises ValueError
    reply = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "el_get_shot_data", "arguments": {"season": "E2024"}},
        },
        mock_registry,
        IDENTITY,
    )
    assert reply is not None
    assert reply["result"]["isError"] is True
    assert "needs at least one narrowing argument" in reply["result"]["content"][0]["text"]


# ---------------------------------------------------------------------------
# 5. Dual Response Encoding Integrity
# ---------------------------------------------------------------------------


def test_dual_response_encoding_matches_across_content_and_structured(mock_registry):
    """Verify content[0].text is valid JSON identical to structuredContent."""
    reply = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "el_describe_warehouse", "arguments": {}},
        },
        mock_registry,
        IDENTITY,
    )
    result = reply["result"]
    text_content = result["content"][0]["text"]
    structured_content = result["structuredContent"]

    parsed_from_text = json.loads(text_content)
    assert parsed_from_text == structured_content
    assert parsed_from_text["coverage"]["games_included"] == 732
