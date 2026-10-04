"""The contract every tool must meet, enforced by a loop rather than by review."""

from __future__ import annotations

from typing import Any

import pytest

from euroleague.mcp.identity import IDENTITY
from euroleague.mcp.protocol import handle_message
from euroleague.mcp.tools import TOOL_NAMES, build_registry


def _null_runner(query: Any, args: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("The contract test must not reach the database.")


@pytest.fixture
def registry():
    return build_registry(_null_runner)


def test_seventeen_tools_are_declared():
    assert len(TOOL_NAMES) == 17
    assert len(set(TOOL_NAMES)) == 17


def test_every_declared_name_starts_with_the_project_prefix():
    assert all(name.startswith("el_") for name in TOOL_NAMES)


def test_every_registered_tool_is_declared(registry):
    assert set(registry) <= set(TOOL_NAMES)


def test_all_eleven_declared_tools_are_registered(registry):
    assert set(registry) == set(TOOL_NAMES)


def test_every_tool_is_marked_read_only(registry):
    for tool in registry.values():
        assert tool.annotations["readOnlyHint"] is True


def test_every_tool_declares_all_standard_safety_annotations(registry):
    for tool in registry.values():
        assert tool.annotations == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        }


def test_every_tool_describes_the_existing_response_envelope(registry):
    for tool in registry.values():
        schema = tool.output_schema
        assert schema["type"] == "object"
        assert set(schema["required"]) == {
            "coverage",
            "excluded",
            "rows",
            "row_count",
            "truncated",
            "caveats",
        }
        assert schema["properties"]["rows"]["type"] == "array"
        assert schema["properties"]["row_count"]["type"] == "integer"


def test_every_tool_has_an_object_input_schema(registry):
    for tool in registry.values():
        assert tool.input_schema["type"] == "object"
        assert isinstance(tool.input_schema["properties"], dict)


def test_every_tool_accepts_include_quarantined_defaulting_to_false(registry):
    for tool in registry.values():
        prop = tool.input_schema["properties"]["include_quarantined"]
        assert prop["type"] == "boolean"
        assert prop["default"] is False


def test_every_description_is_written_as_a_prompt_not_a_label(registry):
    for tool in registry.values():
        assert len(tool.description) >= 120, tool.name


def test_every_schema_property_carries_a_description(registry):
    for tool in registry.values():
        for name, prop in tool.input_schema["properties"].items():
            assert prop.get("description"), f"{tool.name}.{name}"


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_every_tool_rejects_string_for_include_quarantined_before_database_use(name: str, registry):
    """Break caught: a non-empty string is coerced to true and reaches the database runner."""
    tool = registry[name]
    with pytest.raises(ValueError, match=r"include_quarantined must be true or false"):
        tool.handler({"include_quarantined": "false"})


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_every_tool_rejects_null_for_include_quarantined_before_database_use(name: str, registry):
    """Break caught: explicit null is coerced to false instead of rejected."""
    tool = registry[name]
    with pytest.raises(ValueError, match=r"include_quarantined must be true or false"):
        tool.handler({"include_quarantined": None})


def test_registry_boolean_validation_rejects_strings_for_other_boolean_properties(registry):
    """Break caught: tool-specific boolean flags like per_game or aggregate accept strings."""
    with pytest.raises(ValueError, match=r"per_game must be true or false"):
        registry["el_get_player_stats"].handler({"per_game": "false"})

    with pytest.raises(ValueError, match=r"aggregate must be true or false"):
        registry["el_get_possessions"].handler({"aggregate": "false"})

    with pytest.raises(ValueError, match=r"only_with_real_coordinates must be true or false"):
        registry["el_get_shot_data"].handler({"only_with_real_coordinates": "false"})


def test_registry_allows_literal_booleans_to_reach_runner():
    """Break caught: literal booleans are blocked by the registry validator."""
    calls = []

    def recording_runner(query: Any, args: dict[str, Any]) -> dict[str, Any]:
        calls.append((query, args))
        return {}

    reg = build_registry(recording_runner)
    for name in TOOL_NAMES:
        tool = reg[name]
        arguments: dict[str, Any] = {"include_quarantined": True}
        if name == "el_get_play_by_play":
            arguments["gamecode"] = 1
        if name == "el_get_shot_data":
            arguments["team"] = "PAN"
        tool.handler(arguments)
        assert calls[-1][1]["include_quarantined"] is True
        arguments["include_quarantined"] = False
        tool.handler(arguments)
        assert calls[-1][1]["include_quarantined"] is False


def test_season_parameter_and_describe_warehouse_clarify_ending_year_convention(registry):
    """Break caught: a model misinterprets E2024 as 2024-25 instead of the season ending in
    spring 2024.
    """
    for name, tool in registry.items():
        if "season" in tool.input_schema["properties"]:
            desc = tool.input_schema["properties"]["season"]["description"]
            assert "spring" in desc.lower() or "ending in" in desc.lower(), f"{name}.season"

    describe_desc = registry["el_describe_warehouse"].description
    assert "spring" in describe_desc.lower() or "ending in" in describe_desc.lower()


def test_paginated_tools_refuse_deep_offsets_before_the_database_runner():
    """Break caught: a caller can walk an unfiltered table with deep offsets."""
    calls = []

    def recording_runner(query: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((query, arguments))
        return {}

    registry = build_registry(recording_runner)
    paginated = [tool for tool in registry.values() if "offset" in tool.input_schema["properties"]]

    for tool in paginated:
        arguments: dict[str, Any] = {"offset": 2001}
        if tool.name == "el_get_play_by_play":
            arguments["gamecode"] = 1
        if tool.name == "el_get_shot_data":
            arguments["team"] = "PAN"
        with pytest.raises(ValueError, match=r"(?i)2,000.*narrow"):
            tool.handler(arguments)

    assert calls == []


def test_unnarrowed_shot_data_is_an_actionable_tool_error_not_a_protocol_error():
    """Break caught: a season-wide shot query looks like an empty or valid result."""
    calls = []

    def recording_runner(query: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((query, arguments))
        return {}

    registry = build_registry(recording_runner)
    reply = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 33,
            "method": "tools/call",
            "params": {"name": "el_get_shot_data", "arguments": {"season": "E2024"}},
        },
        registry,
        IDENTITY,
    )

    assert "error" not in reply
    assert reply["result"]["isError"] is True
    message = reply["result"]["content"][0]["text"]
    assert "gamecode" in message
    assert "team" in message
    assert "player" in message
    assert calls == []


def test_bulk_tool_descriptions_tell_models_to_narrow_before_paging(registry):
    """Break caught: models learn the restriction only after making a refused call."""
    for name in ("el_get_play_by_play", "el_get_shot_data"):
        description = registry[name].description.lower()
        assert "narrow" in description
        assert "offset" in description or "paginate" in description


def test_play_by_play_still_publishes_gamecode_as_required(registry):
    """Break caught: narrowing moves into the handler and the schema stops saying it."""
    assert registry["el_get_play_by_play"].input_schema["required"] == ["season", "gamecode"]
