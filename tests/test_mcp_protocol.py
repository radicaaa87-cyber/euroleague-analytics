"""The JSON-RPC layer: framing, negotiation, dispatch, and stdout purity."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

from euroleague.mcp.identity import IDENTITY
from euroleague.mcp.protocol import (
    LATEST_PROTOCOL_VERSION,
    Tool,
    handle_message,
    serve,
)


def _echo_tool() -> dict[str, Tool]:
    def handler(arguments: dict) -> dict:
        return {"echoed": arguments.get("value")}

    return {
        "el_echo": Tool(
            name="el_echo",
            description="Echo a value back. Test double only.",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
            handler=handler,
        )
    }


def test_initialize_echoes_a_supported_client_version():
    reply = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        },
        _echo_tool(),
        IDENTITY,
    )
    assert reply["result"]["protocolVersion"] == "2024-11-05"
    assert reply["result"]["capabilities"]["tools"] == {"listChanged": False}
    assert reply["result"]["serverInfo"]["name"] == "euroleague-analytics"


def test_initialize_falls_back_to_our_latest_for_an_unknown_version():
    reply = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "1.0.0", "capabilities": {}},
        },
        _echo_tool(),
        IDENTITY,
    )
    assert reply["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION


def test_notifications_get_no_reply():
    assert (
        handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, {}, IDENTITY)
        is None
    )


def test_tools_list_returns_the_registry():
    reply = handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, _echo_tool(), IDENTITY
    )
    tools = reply["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["el_echo"]
    assert tools[0]["annotations"]["readOnlyHint"] is True
    assert tools[0]["annotations"]["destructiveHint"] is False
    assert tools[0]["annotations"]["openWorldHint"] is False


def test_tools_call_wraps_the_handler_result_as_text_and_structured_content():
    reply = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "el_echo", "arguments": {"value": "hello"}},
        },
        _echo_tool(),
        IDENTITY,
    )
    result = reply["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {"echoed": "hello"}
    assert json.loads(result["content"][0]["text"]) == {"echoed": "hello"}


def test_unknown_method_is_a_protocol_error():
    reply = handle_message({"jsonrpc": "2.0", "id": 4, "method": "nope"}, {}, IDENTITY)
    assert reply["error"]["code"] == -32601


def test_unknown_tool_is_a_protocol_error_naming_the_available_tools():
    reply = handle_message(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "el_missing"}},
        _echo_tool(),
        IDENTITY,
    )
    assert reply["error"]["code"] == -32602
    assert "el_echo" in reply["error"]["message"]


def test_a_missing_required_argument_is_an_invalid_params_error():
    reply = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "el_echo", "arguments": {}},
        },
        _echo_tool(),
        IDENTITY,
    )
    assert reply["error"]["code"] == -32602
    assert "value" in reply["error"]["message"]


def test_a_handler_failure_is_a_tool_error_not_a_protocol_error():
    def explode(arguments: dict) -> dict:
        raise ValueError("no season E2099 in the warehouse")

    tools = {
        "el_boom": Tool(
            name="el_boom",
            description="Always fails. Test double only.",
            input_schema={"type": "object", "properties": {}},
            handler=explode,
        )
    }
    reply = handle_message(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "el_boom"}},
        tools,
        IDENTITY,
    )
    assert "error" not in reply
    assert reply["result"]["isError"] is True
    assert "E2099" in reply["result"]["content"][0]["text"]


def test_malformed_json_produces_a_parse_error_and_the_loop_continues():
    stdin = io.StringIO('not json\n{"jsonrpc":"2.0","id":9,"method":"tools/list"}\n')
    stdout = io.StringIO()
    serve(stdin, stdout, _echo_tool(), IDENTITY)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert replies[0]["error"]["code"] == -32700
    assert replies[1]["id"] == 9


def test_serve_writes_only_json_lines_to_stdout():
    stdin = io.StringIO(
        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        '{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
    )
    stdout = io.StringIO()
    serve(stdin, stdout, _echo_tool(), IDENTITY)
    for line in stdout.getvalue().splitlines():
        if line.strip():
            json.loads(line)


def test_non_object_params_produces_a_reply_not_a_crash():
    reply = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": ["not", "a", "dict"],
        },
        _echo_tool(),
        IDENTITY,
    )
    assert reply is not None
    assert "error" in reply
    assert reply["error"]["code"] == -32602


def test_non_object_arguments_returns_invalid_params_not_internal_error():
    reply = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "el_echo", "arguments": 7},
        },
        _echo_tool(),
        IDENTITY,
    )
    assert reply is not None
    assert "error" in reply
    assert reply["error"]["code"] == -32602
    assert "value" in reply["error"]["message"]


def test_exception_outside_tool_handler_triggers_serve_catch_all():
    class BadTool:
        def to_wire(self) -> dict:
            raise RuntimeError("Exception during tool serialization")

    tools = {"el_bad": BadTool()}
    stdin = io.StringIO(
        '{"jsonrpc":"2.0","id":12,"method":"tools/list"}\n'
        '{"jsonrpc":"2.0","id":13,"method":"ping"}\n'
    )
    stdout = io.StringIO()
    serve(stdin, stdout, tools, IDENTITY)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(replies) == 2
    assert replies[0]["id"] == 12
    assert replies[0]["error"]["code"] == -32603
    assert "internal error" in replies[0]["error"]["message"].lower()
    assert replies[1]["id"] == 13
    assert "result" in replies[1]


def test_notification_that_raises_produces_no_reply():
    class BadTool:
        def to_wire(self) -> dict:
            raise RuntimeError("Exception during tool serialization")

    tools = {"el_bad": BadTool()}
    stdin = io.StringIO('{"jsonrpc":"2.0","method":"tools/list"}\n')
    stdout = io.StringIO()
    serve(stdin, stdout, tools, IDENTITY)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(replies) == 0


def _load_entry_point():
    path = Path(__file__).resolve().parent.parent / "scripts" / "mcp_server.py"
    spec = importlib.util.spec_from_file_location("mcp_server_entry", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["mcp_server_entry"] = module
    spec.loader.exec_module(module)
    return module


def test_the_entry_point_registers_all_seventeen_tools_without_connecting():
    module = _load_entry_point()
    registry = module.build_tool_registry(lambda: None)
    assert len(registry) == 17
    assert all(name.startswith("el_") for name in registry)
