"""The HTTP transport must publish exactly what the stdio transport publishes.

The risk this guards is drift: two transports slowly answering differently while
both look healthy. A response-only comparison is not enough. The `readOnlyHint`
annotation is a default on the `Tool` dataclass in `protocol.py`, and the SDK
path never touches that dataclass, so the annotation can be dropped in transit
with nothing failing anywhere.
"""

from __future__ import annotations

from typing import Any

import pytest

from euroleague.mcp.http_app import (
    published_tools,
    sdk_tools,
    sdk_tools_as_wire,
    tool_fingerprint,
)
from euroleague.mcp.tools import TOOL_NAMES, build_registry

EXPECTED_TOOL_LIST_FINGERPRINT = "64b6e9ff7e993f9e7b9258251b189dfe2007e903b85f9b8d92d0415850f12b3b"


def _registry() -> dict:
    """The real ten tools, bound to a runner that is never called."""

    def runner(query: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("no test in this module should reach the database")

    return build_registry(runner)


def test_all_eleven_tools_are_published() -> None:
    names = [tool["name"] for tool in published_tools(_registry())]
    assert sorted(names) == sorted(TOOL_NAMES)


def test_every_published_tool_is_marked_read_only() -> None:
    """CLAUDE.md requires readOnlyHint on read-only tools."""
    for tool in published_tools(_registry()):
        assert tool["annotations"]["readOnlyHint"] is True, tool["name"]


def test_every_published_tool_has_explicit_standard_safety_annotations() -> None:
    for tool in published_tools(_registry()):
        assert tool["annotations"] == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        }, tool["name"]


def test_every_published_tool_has_an_output_schema() -> None:
    for tool in published_tools(_registry()):
        assert tool["outputSchema"]["type"] == "object", tool["name"]


def test_every_sdk_tool_is_marked_read_only() -> None:
    """The annotation must survive conversion into the SDK's own objects."""
    for tool in sdk_tools(_registry()):
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is False, tool.name
        assert tool.annotations.open_world_hint is False, tool.name
        assert tool.output_schema is not None, tool.name


def test_the_sdk_wire_shape_equals_the_stdio_wire_shape() -> None:
    """The load-bearing assertion: both transports publish the same document."""
    registry = _registry()
    assert sdk_tools_as_wire(registry) == published_tools(registry)


def test_the_two_transports_have_the_same_fingerprint() -> None:
    registry = _registry()
    assert tool_fingerprint(sdk_tools_as_wire(registry)) == tool_fingerprint(
        published_tools(registry)
    )


def test_fingerprint_is_stable_across_calls() -> None:
    assert tool_fingerprint(published_tools(_registry())) == tool_fingerprint(
        published_tools(_registry())
    )


def test_tool_list_fingerprint_matches_the_versioned_registry_contract() -> None:
    """Break caught: a tool description or schema changes without its fingerprint update."""
    assert tool_fingerprint(published_tools(_registry())) == EXPECTED_TOOL_LIST_FINGERPRINT


def test_fingerprint_changes_when_an_annotation_is_lost() -> None:
    """The test that would catch a silently dropped readOnlyHint."""
    good = published_tools(_registry())
    damaged = [dict(tool) for tool in good]
    damaged[0] = {**damaged[0], "annotations": {}}
    assert tool_fingerprint(good) != tool_fingerprint(damaged)


def test_fingerprint_changes_when_a_schema_changes() -> None:
    """A silently altered input schema must also fail, not only a lost annotation."""
    good = published_tools(_registry())
    damaged = [dict(tool) for tool in good]
    damaged[0] = {**damaged[0], "inputSchema": {"type": "object", "properties": {}}}
    assert tool_fingerprint(good) != tool_fingerprint(damaged)


def test_input_schemas_are_carried_across_verbatim() -> None:
    """Schema derivation from the handler signature would silently produce nonsense."""
    registry = _registry()
    by_name = {tool.name: tool for tool in sdk_tools(registry)}
    for name, tool in registry.items():
        assert by_name[name].input_schema == tool.input_schema


def test_output_schemas_are_carried_across_verbatim() -> None:
    registry = _registry()
    by_name = {tool.name: tool for tool in sdk_tools(registry)}
    for name, tool in registry.items():
        assert by_name[name].output_schema == tool.output_schema


def test_the_portable_registry_contains_no_openai_specific_metadata() -> None:
    for tool in published_tools(_registry()):
        assert "_meta" not in tool
        assert not any(key.startswith("openai/") for key in tool)


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_each_tool_keeps_its_description(name: str) -> None:
    """Descriptions are read by the model at call time; a truncated one changes behaviour."""
    registry = _registry()
    by_name = {tool.name: tool for tool in sdk_tools(registry)}
    assert by_name[name].description == registry[name].description
