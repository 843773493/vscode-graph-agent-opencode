"""真实工具失败经过canonical、检查点及模型请求时不复制原生诊断。"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from functools import wraps

import pytest
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.serde.types import ERROR

from app.agents import agent_factory
from tests.integration.backend.sessions.test_persistent_tasks import _target
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
    _tool_reply,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 使用当前文件的隔离工作区。
)


@pytest.fixture
def fail_after_read(monkeypatch: pytest.MonkeyPatch) -> Callable[[BaseException], list[ToolMessage]]:
    def install(raw_error: BaseException) -> list[ToolMessage]:
        create_agent = agent_factory.create_model_call_agent
        observations: list[ToolMessage] = []

        def instrument_tools(*args, **kwargs):
            registered = [
                *(kwargs["tools"] or ()),
                *(tool for middleware in kwargs["middleware"] for tool in getattr(middleware, "tools", ())),
            ]
            tool, = [tool for tool in registered if tool.name == "read_file"]
            body = tool.coroutine
            assert body is not None

            @wraps(body)
            async def fail_after_result(*body_args, **body_kwargs):
                result = await body(*body_args, **body_kwargs)
                assert isinstance(result, ToolMessage) and result.status == "success", result
                assert isinstance(result.content, str)
                assert "真实文件工具仍执行一次读取。" in result.content
                observations.append(result)
                raise raw_error

            tool.coroutine = fail_after_result
            return create_agent(*args, **kwargs)

        monkeypatch.setattr(agent_factory, "create_model_call_agent", instrument_tools)
        return observations

    return install


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_tool_timeout_keeps_native_diagnostics_out_of_canonical_and_next_request(
    source_case: RuntimeSourceCase, fail_after_read: Callable[[BaseException], list[ToolMessage]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    case = source_case
    session_id, thread_id, _target_uri = await _target(case)
    (case.workspace / "timeout-input.txt").write_text("真实文件工具仍执行一次读取。", encoding="utf-8")
    locations = ("/private/tool-timeout.sqlite", r"C:\private\tool-timeout.sqlite", r"\\target\private\tool-timeout.sqlite")
    raw_error = TimeoutError(110, "真实回执交付超时: " + " | ".join(locations), locations[0])
    observations = fail_after_read(raw_error)
    calls = await case.plan(session_id, [
        _tool_reply("read_file", {"path": "timeout-input.txt"}, "timeout-read"),
        {"content": "工具结果未知，已保留恢复提示。"},
    ])
    with caplog.at_level(logging.ERROR):
        accepted = await _send(case, session_id, "读取测试材料并处理超时。")
        job_id = accepted["job_id"]
        assert isinstance(job_id, str)
        await _job(case, job_id)
    assert len(observations) == 1 and case.call_count(session_id, thread_id) == 2
    messages = case.request(calls[1])["messages"]
    assert isinstance(messages, list)
    result, = [message for message in messages if message["role"] == "tool"]
    assert result["tool_call_id"] == "timeout-read"
    payload = json.loads(result["content"])
    assert payload["status"] == "error" and payload["code"] == "tool_execution_timeout"
    assert payload["retryable"] is True and payload["recovery"] == "check_tool_state_before_retry"
    assert "stage=tool-timeout" in payload["error"] and "error=TimeoutError" in payload["error"]
    assert "errno=110" in payload["error"]
    items = case.container.checkpointer._storage.storage.read_items(session_id, thread_id)
    carriers = []
    for item in items:
        if item.semantic_kind == "tool_result":
            assert isinstance(item.payload, Mapping)
            assert item.payload["name"] == "read_file" and item.payload["content"] == result["content"]
            carriers.append(item.payload)
    results = [payload for payload in carriers if "tool_attempt_id" in payload]
    assert len(results) == 1 and results[0]["tool_call_id"] == calls[0] + ":tool-call:timeout-read"
    serialized = json.dumps([case.requests(), carriers], ensure_ascii=False)
    assert "tool-timeout.sqlite" not in serialized
    checkpoints = list(case.container.checkpointer.list(None))
    assert checkpoints and "tool-timeout.sqlite" not in repr(checkpoints)
    for checkpoint in checkpoints:
        config = checkpoint.config.get("configurable")
        assert config is not None and config["thread_id"] == thread_id
    assert any(record.name == "app.core.failures" and record.exc_info is not None
               and record.exc_info[1] is raw_error for record in caplog.records)
    assert str(raw_error) in caplog.text
    case.write_actual("tool-timeout", {"result": payload, "canonical": results[0], "calls": calls})


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_unhandled_tool_failure_keeps_native_diagnostics_out_of_durable_error_channels(
    source_case: RuntimeSourceCase, fail_after_read: Callable[[BaseException], list[ToolMessage]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    case = source_case
    session_id, thread_id, _target_uri = await _target(case)
    (case.workspace / "failure-input.txt").write_text("真实文件工具仍执行一次读取。", encoding="utf-8")
    locations = ("/private/tool-execution.sqlite", r"C:\private\tool-execution.sqlite", r"\\target\private\tool-execution.sqlite")
    raw_error = OSError(5, "真实回执交付失败: " + " | ".join(locations), locations[0])
    observations = fail_after_read(raw_error)
    calls = await case.plan(session_id, [_tool_reply("read_file", {"path": "failure-input.txt"}, "failed-read")])
    with caplog.at_level(logging.ERROR):
        accepted = await _send(case, session_id, "读取测试材料并暴露执行失败。")
        job_id = accepted["job_id"]
        assert isinstance(job_id, str)
        job = await _job(case, job_id, expected="failed")
    assert len(observations) == 1 and case.call_count(session_id, thread_id) == 1
    items = case.container.checkpointer._storage.storage.read_items(session_id, thread_id)
    result, = [item for item in items if item.semantic_kind == "tool_result"]
    assert isinstance(result.payload, Mapping)
    assert result.payload["tool_call_id"] == calls[0] + ":tool-call:failed-read"
    assert result.payload["name"] == "read_file"
    assert result.payload["tool_outcome"] == "unknown" and result.payload["execution_confirmed"] is False
    content = result.payload["content"]
    assert isinstance(content, str) and "stage=tool-execution" in content and "error=OSError" in content and "errno=5" in content
    checkpoints = list(case.container.checkpointer.list(None))
    errors = [value for checkpoint in checkpoints for _, channel, value in checkpoint.pending_writes or () if channel == ERROR]
    assert errors and all(isinstance(value, str) and "stage=checkpoint-task" in value and "error=OSError" in value and "errno=5" in value for value in errors)
    assert "tool-execution.sqlite" not in json.dumps([job, case.requests(), result.payload], ensure_ascii=False)
    assert "tool-execution.sqlite" not in repr(checkpoints)
    assert any(record.name == "app.core.failures" and record.exc_info is not None
               and record.exc_info[1] is raw_error for record in caplog.records)
    assert str(raw_error) in caplog.text
    case.write_actual("tool-execution", {"job": job, "canonical": result.payload, "checkpoint_errors": errors, "calls": calls})
