from __future__ import annotations

from langchain_core.messages import AIMessage

from app.agents.providers.openai_response_history import (
    _message_with_standard_tool_calls,
)


def _tool_call(call_id: str, name: str = "read_file") -> dict[str, object]:
    return {
        "name": name,
        "args": {"path": "README.md"},
        "id": call_id,
        "type": "tool_call",
    }


def test_standard_tool_history_preserves_canonical_message_identity() -> None:
    message = AIMessage(
        content="已完成",
        tool_calls=[_tool_call("call-a"), _tool_call("call-b", "write_file")],
    )

    projected = _message_with_standard_tool_calls(message)

    assert projected is message
    assert projected.tool_calls is message.tool_calls


def test_standard_calls_still_merge_legacy_content_calls() -> None:
    standard_call = _tool_call("call-a")
    message = AIMessage(
        content=[
            {
                "type": "function_call",
                "call_id": "call-legacy",
                "name": "write_file",
                "arguments": '{"path":"README.md"}',
            }
        ],
        tool_calls=[standard_call],
    )

    projected = _message_with_standard_tool_calls(message)

    assert [call["id"] for call in projected.tool_calls] == ["call-a", "call-legacy"]
    assert [call["id"] for call in message.tool_calls] == ["call-a"]


def test_duplicate_standard_call_ids_still_repair_history() -> None:
    message = AIMessage(
        content="已完成",
        tool_calls=[_tool_call("call-a"), _tool_call("call-a", "write_file")],
    )

    projected = _message_with_standard_tool_calls(message)

    assert projected is not message
    assert [call["id"] for call in projected.tool_calls] == ["call-a"]
    assert [call["name"] for call in message.tool_calls] == ["read_file", "write_file"]
