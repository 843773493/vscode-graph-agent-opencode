from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolCall, ToolMessage
from langgraph.types import Command

from app.agents import tool_confirmation_middleware as confirmation_module
from app.agents.tool_confirmation_middleware import (
    ToolApprovalState,
    ToolConfirmationMiddleware,
)
from app.core.model_delta_context import (
    reset_current_model_delta_sink,
    set_current_model_delta_sink,
)


def _state(*tool_calls: ToolCall) -> ToolApprovalState:
    return {
        "messages": [
            AIMessage(
                content="",
                id="approval-assistant",
                tool_calls=list(tool_calls),
            )
        ]
    }


def _custom_call(
    *,
    call_id: str,
    target_name: str,
    arguments: dict[str, object],
) -> ToolCall:
    return {
        "name": "invoke_extension_tool",
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
    middleware = ToolConfirmationMiddleware(
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
                "模型请求执行需要人工确认的工具。\n\n"
                "Tool: evaluate_expression\nArgs: {'expression': 'input + 1'}"
            ),
        }
    ]
    assert result is not None
    assert result == {"tool_approval_edits": {}}
    assert isinstance(state["messages"][0], AIMessage)
    assert len(state["messages"][0].tool_calls) == 2


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
    middleware = ToolConfirmationMiddleware(
        frozenset({"evaluate_expression"})
    )

    state = _state(
            _custom_call(
                call_id="call-rejected",
                target_name="evaluate_expression",
                arguments={"expression": "dangerous()"},
            )
        )
    original = state["messages"][0].model_dump()
    result = middleware.after_model(state, MagicMock())

    assert result is not None
    messages = result["messages"]
    assert isinstance(messages, list)
    assert isinstance(messages[-1], ToolMessage)
    assert messages[-1].status == "error"
    assert messages[-1].content == "不要执行这个表达式"
    assert state["messages"][0].model_dump() == original


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
    middleware = ToolConfirmationMiddleware(
        frozenset({"evaluate_expression"})
    )

    state = _state(
            _custom_call(
                call_id="call-edited",
                target_name="evaluate_expression",
                arguments={"expression": "dangerous()"},
            )
        )
    original = state["messages"][0].model_dump()
    result = middleware.after_model(state, MagicMock())

    assert result is not None
    assert "messages" not in result
    assert state["messages"][0].model_dump() == original
    assert result["tool_approval_edits"] == {
        "call-edited": {
            "name": "invoke_extension_tool",
            "args": {
                "tool_name": "evaluate_expression",
                "arguments": {"expression": "safeValue"},
            },
            "id": "call-edited",
            "type": "tool_call",
        }
    }


@pytest.mark.asyncio
async def test_async_rejection_commits_result_with_original_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = MagicMock(complete_tool_approval=AsyncMock())
    token = set_current_model_delta_sink(sink)
    monkeypatch.setattr(
        confirmation_module,
        "interrupt",
        lambda _request: {"decisions": [{"type": "reject", "message": "用户拒绝"}]},
    )
    state = _state(_custom_call(call_id="call-approval", target_name="evaluate_expression", arguments={"expression": "safe"}))
    state["messages"][0].response_metadata["model_call_id"] = "model-call-approval"
    try:
        result = await ToolConfirmationMiddleware(
            frozenset({"evaluate_expression"})
        ).aafter_model(
            state,
            MagicMock(),
        )
    finally:
        reset_current_model_delta_sink(token)

    assert result is not None
    sink.complete_tool_approval.assert_awaited_once()
    recorded = sink.complete_tool_approval.await_args
    assert recorded.kwargs == {"model_call_id": "model-call-approval"}
    assert recorded.args[0].tool_call_id == "call-approval"
    assert recorded.args[0].content == "用户拒绝" and recorded.args[0].status == "error"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("selected,result_kind", [("selected_tool", kind) for kind in ("text", "command", "commands")] + [("unknown_tool", "text")])
async def test_direct_edit_executes_selected_tool_without_rewriting_emitting_call(
    monkeypatch: pytest.MonkeyPatch, asynchronous: bool, selected: str, result_kind: str,
) -> None:
    from langchain_core.tools import tool
    from langgraph.graph import END, START, StateGraph

    from app.agents.graph_tool_adapter import ContentPreservingToolNode
    from app.agents.tool_confirmation_middleware import ToolApprovalState
    from app.agents.tool_invocation_context import (
        ToolInvocationContext,
        ToolInvocationContextMiddleware,
    )

    context = ToolInvocationContext()
    received = []

    @tool
    def original_tool(value: str) -> str:
        """不应执行的原始目标。"""
        raise AssertionError("编辑后不得执行原工具")

    @tool
    def selected_tool(value: str) -> str | Command | list[Command | ToolMessage]:
        """用户明确选择的目标。"""
        received.append((value, context.require_model_call_id(), context.require_tool_call_id()))
        message = ToolMessage(content=value, tool_call_id=context.require_tool_call_id(), name="selected_tool")
        if result_kind == "command":
            return Command(update={"messages": [message]})
        if result_kind == "commands":
            return [Command(update={"tool_approval_edits": {}}), message]
        return value

    monkeypatch.setattr(confirmation_module, "interrupt", lambda _request: {
        "decisions": [{"type": "edit", "edited_action": {"name": selected, "args": {"value": "用户编辑"}}}]
    })
    state = _state({"name": "original_tool", "args": {"value": "模型原值"}, "id": "original-call", "type": "tool_call"})
    assistant = state["messages"][0]
    assistant.response_metadata["model_call_id"] = "original-model"
    original = assistant.model_dump()
    middleware = ToolConfirmationMiddleware(frozenset(), interrupt_on={"original_tool": True})
    result = middleware.after_model(state, MagicMock())
    invocation = ToolInvocationContextMiddleware(context)
    node = ContentPreservingToolNode([original_tool, selected_tool],
                                    wrap_tool_call=invocation.wrap_tool_call,
                                    awrap_tool_call=invocation.awrap_tool_call)
    builder = StateGraph(ToolApprovalState)
    builder.add_node("tools", node)
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile()
    execution_state = cast(ToolApprovalState, {**state, **result})
    sink = MagicMock(prepare_tool_execution=AsyncMock(), complete_tool_approval=AsyncMock())
    token = set_current_model_delta_sink(sink)
    try:
        output = await graph.ainvoke(execution_state) if asynchronous else graph.invoke(execution_state)
    finally:
        reset_current_model_delta_sink(token)
    assert output["messages"][-1].tool_call_id == "original-call"
    assert output["messages"][-1].name == "original_tool"
    if selected == "selected_tool":
        assert received == [("用户编辑", "original-model", "original-call")]
        assert output["messages"][-1].content == "用户编辑"
        sink.complete_tool_approval.assert_not_called()
    else:
        assert received == []
        assert output["messages"][-1].status == "error"
        assert "unknown_tool is not a valid tool" in output["messages"][-1].content
        if asynchronous:
            sink.complete_tool_approval.assert_awaited_once_with(output["messages"][-1], model_call_id="original-model")
    assert assistant.model_dump() == original
    # 下一次相同 provider ID 没有审批时，不得复用上批编辑。
    assert middleware.after_model(_state({"name": "selected_tool", "args": {}, "id": "original-call", "type": "tool_call"}), MagicMock()) == {"tool_approval_edits": {}}


def test_direct_and_extension_requests_preserve_order_and_allowed_decisions(monkeypatch):
    captured = {}

    def decide(request):
        captured.update(request)
        return {"decisions": [{"type": "approve"}, {"type": "respond", "message": "用户结果"}]}

    monkeypatch.setattr(confirmation_module, "interrupt", decide)
    middleware = ToolConfirmationMiddleware(frozenset({"extension"}), interrupt_on={
        "direct": {"allowed_decisions": ["approve", "reject"], "description": "直接工具确认"},
    })
    state = _state({"name": "direct", "args": {}, "id": "direct-id", "type": "tool_call"},
                   _custom_call(call_id="extension-id", target_name="extension", arguments={"x": 1}))
    result = middleware.after_model(state, MagicMock())
    assert [action["name"] for action in captured["action_requests"]] == ["direct", "extension"]
    assert captured["action_requests"][0]["description"] == "直接工具确认"
    assert captured["review_configs"][0]["allowed_decisions"] == ["approve", "reject"]
    messages = cast(list[ToolMessage], result["messages"])
    assert messages[0].tool_call_id == "extension-id"
    assert messages[0].content == "用户结果"
