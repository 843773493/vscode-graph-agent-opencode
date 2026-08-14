from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.agents import custom_tool_confirmation_middleware as confirmation_module
from app.agents.custom_tool_confirmation_middleware import (
    CustomToolConfirmationMiddleware,
)


def _state(*tool_calls: dict[str, object]) -> dict[str, object]:
    return {
        "messages": [
            AIMessage(
                content="",
                tool_calls=list(tool_calls),
            )
        ]
    }


def _custom_call(
    *,
    call_id: str,
    target_name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    return {
        "name": "invoke_custom_tool",
        "args": {"tool_name": target_name, "arguments": arguments},
        "id": call_id,
        "type": "tool_call",
    }


def test_custom_tool_confirmation_matches_internal_target_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def approve(request: dict[str, object]) -> dict[str, object]:
        captured.update(request)
        return {"decisions": [{"type": "approve"}]}

    monkeypatch.setattr(confirmation_module, "interrupt", approve)
    middleware = CustomToolConfirmationMiddleware(
        frozenset({"evaluate_expression"})
    )
    state = _state(
        _custom_call(
            call_id="call-evaluate",
            target_name="evaluate_expression",
            arguments={"expression": "input + 1"},
        ),
        _custom_call(
            call_id="call-list",
            target_name="list_breakpoints",
            arguments={},
        ),
    )

    result = middleware.after_model(state, MagicMock())

    assert captured["action_requests"] == [
        {
            "name": "evaluate_expression",
            "args": {"expression": "input + 1"},
            "description": (
                "模型请求执行需要人工确认的扩展工具。\n\n"
                "Tool: evaluate_expression\nArgs: {'expression': 'input + 1'}"
            ),
        }
    ]
    assert result is not None
    messages = result["messages"]
    assert isinstance(messages, list)
    assert len(messages[0].tool_calls) == 2


def test_custom_tool_confirmation_rejection_skips_target_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        confirmation_module,
        "interrupt",
        lambda _request: {
            "decisions": [{"type": "reject", "message": "不要执行这个表达式"}]
        },
    )
    middleware = CustomToolConfirmationMiddleware(
        frozenset({"evaluate_expression"})
    )

    result = middleware.after_model(
        _state(
            _custom_call(
                call_id="call-rejected",
                target_name="evaluate_expression",
                arguments={"expression": "dangerous()"},
            )
        ),
        MagicMock(),
    )

    assert result is not None
    messages = result["messages"]
    assert isinstance(messages, list)
    assert isinstance(messages[-1], ToolMessage)
    assert messages[-1].status == "error"
    assert messages[-1].content == "不要执行这个表达式"


def test_custom_tool_confirmation_edit_keeps_fixed_model_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        confirmation_module,
        "interrupt",
        lambda _request: {
            "decisions": [
                {
                    "type": "edit",
                    "edited_action": {
                        "name": "evaluate_expression",
                        "args": {"expression": "safeValue"},
                    },
                }
            ]
        },
    )
    middleware = CustomToolConfirmationMiddleware(
        frozenset({"evaluate_expression"})
    )

    result = middleware.after_model(
        _state(
            _custom_call(
                call_id="call-edited",
                target_name="evaluate_expression",
                arguments={"expression": "dangerous()"},
            )
        ),
        MagicMock(),
    )

    assert result is not None
    messages = result["messages"]
    assert isinstance(messages, list)
    assert messages[0].tool_calls == [
        {
            "name": "invoke_custom_tool",
            "args": {
                "tool_name": "evaluate_expression",
                "arguments": {"expression": "safeValue"},
            },
            "id": "call-edited",
            "type": "tool_call",
        }
    ]
