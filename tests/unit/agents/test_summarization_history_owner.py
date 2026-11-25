"""历史原文工具只读取自身 Thread 的 canonical owner，不公开归档路径。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain.tools import ToolRuntime
from langchain_core.tools import ToolException

from app.agents.summarization.tool_middleware import CompactionToolsMiddleware
from app.domain.itemized.mutation_intents import MutationIntentOwner


@pytest.fixture
def history_tool_case():
    owner = MutationIntentOwner("history-session", "history-thread")
    read = Mock(return_value="41: 历史原文")
    middleware = CompactionToolsMiddleware(compactor=SimpleNamespace(read_history=read), owner=owner, agent_id="default")
    tool = next(tool for tool in middleware.tools if tool.name == "read_compaction_history")
    runtime = ToolRuntime(state={}, context=None, config={}, stream_writer=lambda _: None,
                          tool_call_id="history-read", store=None, tools=middleware.tools)
    return owner, read, tool, runtime


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_history_tool_uses_exact_bound_owner_and_requested_range(history_tool_case, asynchronous):
    owner, read, tool, runtime = history_tool_case
    result = await tool.coroutine(runtime, 41, 7) if asynchronous else tool.func(runtime, 41, 7)
    assert result == "41: 历史原文"
    read.assert_called_once_with(owner, line_offset=41, limit=7)
    properties = tool.tool_call_schema.model_json_schema()["properties"]
    assert set(properties) == {"line_offset", "limit"}
    assert properties["line_offset"]["minimum"] == 1
    assert properties["limit"]["minimum"] == 1 and properties["limit"]["maximum"] == 2000
    assert properties["limit"]["default"] == 200


async def test_history_owner_failure_retains_tool_error_and_cause(history_tool_case):
    _, read, tool, runtime = history_tool_case
    failure = ValueError("compaction-history-unavailable: 当前 Thread 没有已采用候选")
    read.side_effect = failure
    with pytest.raises(ToolException) as raised:
        await tool.coroutine(runtime)
    assert raised.value.__cause__ is failure
    assert "当前 Thread" in str(raised.value)
    read.side_effect = OSError("canonical reader failed")
    with pytest.raises(OSError, match="canonical reader failed"):
        await tool.coroutine(runtime)
