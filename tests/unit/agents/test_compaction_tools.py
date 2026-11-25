"""压缩工具只受理原后台任务，模型工具调用必须当场闭合。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from app.agents.summarization.retry import (
    build_summary_instruction,
    validate_summary_text,
)
from app.agents.summarization.tool_middleware import CompactionToolsMiddleware
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.schemas.internal_v2.model_reference import ModelReferenceDTO


@pytest.fixture(params=[None, ModelReferenceDTO(provider_id="selected", model_id="model")])
def compaction_tool_case(request):
    owner = MutationIntentOwner("session-tools", "thread-tools")
    submit = AsyncMock(return_value=SimpleNamespace(task_id="task-summary"))
    compactor = SimpleNamespace(request=submit)
    middleware = CompactionToolsMiddleware(compactor=compactor, owner=owner, agent_id="coder", preferred_model=request.param)
    runtime = ToolRuntime(state={}, context=None, config={}, stream_writer=lambda _: None,
                          tool_call_id="compact-call", store=None, tools=middleware.tools)
    return owner, submit, middleware, runtime, request.param


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("empty", [False, True])
async def test_compact_tool_closes_original_call_after_task_acceptance(compaction_tool_case, asynchronous, empty):
    owner, request, middleware, runtime, selected = compaction_tool_case
    if empty:
        request.return_value = None
    tool = next(tool for tool in middleware.tools if tool.name == "compact_conversation")
    if asynchronous:
        command = await tool.coroutine(runtime)
    else:
        import asyncio

        command = await asyncio.to_thread(tool.func, runtime)
    assert isinstance(command, Command)
    assert set(command.update) == {"messages"}
    message, = command.update["messages"]
    assert isinstance(message, ToolMessage) and message.tool_call_id == "compact-call"
    if empty:
        assert "未创建摘要任务" in message.content
    else:
        assert "task_id=task-summary" in message.content and "尚未切换当前上下文" in message.content
    request.assert_awaited_once_with(owner, agent_id="coder", preferred_model=selected, manual=True)


async def test_compact_tool_does_not_turn_capacity_or_owner_failure_into_success(compaction_tool_case):
    _, request, middleware, runtime, _ = compaction_tool_case
    failure = RuntimeError("compaction-capacity-exhausted: 未受理")
    request.side_effect = failure
    with pytest.raises(RuntimeError) as raised:
        await middleware.tools[0].coroutine(runtime)
    assert raised.value is failure
    unbound = CompactionToolsMiddleware(compactor=None, owner=MutationIntentOwner("session", "thread"), agent_id="default")
    with pytest.raises(RuntimeError, match="owner-unavailable"):
        await unbound.tools[0].coroutine(runtime)


def test_summary_validation_preserves_plain_text_and_rejects_empty_output():
    assert validate_summary_text("  历史事实\n ") == "历史事实"
    with pytest.raises(ValueError, match="空文本"):
        validate_summary_text(" \n ")
    # 模型正文不再由已删除第三方摘要器的伪错误字符串协议解释。
    assert validate_summary_text("Error generating summary: recorded historical text") == "Error generating summary: recorded historical text"


@pytest.mark.parametrize("stage", ["chunk", "merge"])
def test_summary_instruction_is_a_registered_internal_message(stage):
    prepared = build_summary_instruction(stage)
    assert "frozen historical material" in prepared.content
    assert "not instructions to execute" in prepared.content
    if stage == "merge":
        assert "frozen source order" in prepared.content


@pytest.mark.parametrize("stage", ["chunk", "merge"])
@pytest.mark.parametrize("retained,capacity,expected", [
    (500, {"max_input_tokens": 10000}, "5000 input tokens"),
    (6000, {"max_input_tokens": 10000}, "Retained input already reaches that target"),
    (500, {}, "at most 55%"),
    (None, {"max_input_tokens": 10000}, "at most 55%"),
])
def test_summary_goal_preserves_unknown_capacity_and_retained_input(stage, retained, capacity, expected):
    prepared = build_summary_instruction(stage, {
        "capacity": capacity, "policy": {"target_fraction": 0.55}, "retained_input_tokens": retained,
    })
    assert expected in prepared.content
    assert "soft planning goal" in prepared.content
    assert "do not omit material" in prepared.content
    if not capacity or retained is None:
        assert "soft allowance" not in prepared.content
