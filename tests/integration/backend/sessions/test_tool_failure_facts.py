"""真实工具失败经过canonical、检查点及模型请求时不复制原生诊断。"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from functools import wraps

import httpx
import pytest
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.serde.types import ERROR

from app.agents import agent_factory
from app.agents.tools import web as web_tools
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes
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


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_canonical_rejection_keeps_dynamic_fields_out_of_public_execution_records(
    source_case: RuntimeSourceCase, fail_after_read: Callable[[BaseException], list[ToolMessage]],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    case = source_case
    session_id, thread_id, _ = await _target(case)
    (case.workspace / "canonical-input.txt").write_text("真实文件工具仍执行一次读取。", encoding="utf-8")
    location = str(case.workspace / "private-canonical-field.sqlite")
    with pytest.raises(ItemSchemaError) as rejected:
        canonical_json_bytes({location: float("nan")})
    error = rejected.value
    original_builder = agent_factory.create_model_call_agent
    observations = fail_after_read(error)
    call, = await case.plan(session_id, [_tool_reply("read_file", {"path": "canonical-input.txt"}, "canonical-read")])
    with caplog.at_level(logging.ERROR):
        accepted = await _send(case, session_id, "读取材料并明确报告内部结构拒绝。")
        job_id = accepted["job_id"]
        assert isinstance(job_id, str)
        failed = await _job(case, job_id, expected="failed")
    assert len(observations) == 1 and case.call_count(session_id, thread_id) == 1
    assert failed["error_message"] == "执行数据校验失败: error=ItemSchemaError"
    items = case.container.checkpointer._storage.storage.read_items(session_id, thread_id)
    result, = [item for item in items if item.semantic_kind == "tool_result"]
    assert isinstance(result.payload, Mapping)
    assert result.payload["name"] == "read_file" and result.payload["tool_call_id"] == call + ":tool-call:canonical-read"
    assert result.payload["tool_outcome"] == "unknown" and result.payload["execution_confirmed"] is False
    root = f"/api/v1/sessions/{session_id}/turns/{accepted['turn_id']}/message-stream"
    snapshot = await case.client.get(root + "/snapshot", params={"thread_id": thread_id})
    logs = await case.client.get(f"/api/v1/sessions/{session_id}/llm-request-logs")
    traces = await case.client.get(f"/api/v1/sessions/{session_id}/traces", params={"thread_id": thread_id})
    for response in (snapshot, logs, traces):
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
    assert snapshot.json()["data"]["stream_status"] == "failed"
    public = json.dumps([failed, result.payload, snapshot.json(), logs.json(), traces.json()], ensure_ascii=False)
    assert "private-canonical-field.sqlite" not in public
    checkpoints = list(case.container.checkpointer.list(None))
    assert "private-canonical-field.sqlite" not in repr(checkpoints)
    storage = case.container.checkpointer._storage.storage
    with storage._connect(thread_id, session_id=session_id, read_only=True) as connection:
        assert "private-canonical-field.sqlite" not in "\n".join(connection.iterdump())
    assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)
    assert str(error) in caplog.text
    original_items = case.sql(session_id, thread_id, "SELECT item_id,content_hash FROM item_catalog ORDER BY item_sequence")

    monkeypatch.setattr(agent_factory, "create_model_call_agent", original_builder)
    await case.plan(session_id, [{"content": "同一会话继续处理已有材料。"}])
    resumed = await _send(case, session_id, "故障解除，继续整理。")
    resumed_job = resumed["job_id"]
    assert isinstance(resumed_job, str)
    completed = await _job(case, resumed_job)
    assert completed["session_id"] == session_id and completed["thread_id"] == thread_id
    assert case.call_count(session_id, thread_id) == 2
    assert "private-canonical-field.sqlite" not in json.dumps(case.requests(), ensure_ascii=False)
    original = await case.client.get(root + "/snapshot", params={"thread_id": thread_id})
    assert original.status_code == 200 and original.json()["data"] == snapshot.json()["data"]
    assert case.sql(session_id, thread_id, "SELECT item_id,content_hash FROM item_catalog ORDER BY item_sequence")[:len(original_items)] == original_items
    case.write_actual("canonical-validation-failure", {"job": failed, "tool_result": result.payload, "stream": snapshot.json()})


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
@pytest.mark.parametrize("tool_name", ["web_search", "fetch_webpage"])
async def test_web_dependency_failure_keeps_native_diagnostics_out_of_execution_records(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, tool_name: str,
) -> None:
    case = source_case
    session_id, thread_id, _ = await _target(case)
    location = case.workspace / "private-web-dependency-location"
    location.mkdir()
    try:
        location.read_bytes()
    except OSError as error:
        native = error
    else:
        raise AssertionError("读取目录必须产生原生错误")
    url = "https://web-failure.invalid/document"
    dependency_error = httpx.ConnectError(str(native), request=httpx.Request("GET", url)) if tool_name == "fetch_webpage" else native
    failing = True
    observed: list[str] = []

    def search(_self: object, query: str) -> list[dict[str, str]]:
        observed.append(query)
        if failing:
            raise dependency_error
        return [{"title": "恢复后的搜索结果", "link": url, "snippet": "外部依赖恢复"}]

    handle = httpx.AsyncHTTPTransport.handle_async_request

    async def fetch(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        if str(request.url) == url:
            observed.append(str(request.url))
            if failing:
                raise dependency_error from native
            return httpx.Response(200, request=request, headers={"content-type": "text/plain"}, text="外部依赖恢复")
        return await handle(transport, request)

    monkeypatch.setattr(web_tools.DuckDuckGoSearchResults, "invoke", search)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fetch)
    arguments = {"query": "Python documentation"} if tool_name == "web_search" else {"urls": [url]}
    reply = _tool_reply("invoke_extension_tool", {"tool_name": tool_name, "arguments": arguments}, "web-dependency")
    calls = await case.plan(session_id, [reply])
    with caplog.at_level(logging.ERROR):
        accepted = await _send(case, session_id, "验证网页工具依赖失败，保留明确的恢复依据。")
        job_id = accepted["job_id"]
        assert isinstance(job_id, str)
        job = await _job(case, job_id, expected="failed")
    assert observed == ["Python documentation" if tool_name == "web_search" else url]
    assert case.call_count(session_id, thread_id) == 1
    items = case.container.checkpointer._storage.storage.read_items(session_id, thread_id)
    result, = [item for item in items if item.semantic_kind == "tool_result"]
    assert isinstance(result.payload, Mapping)
    assert result.payload["tool_call_id"] == calls[0] + ":tool-call:web-dependency"
    assert result.payload["name"] == "invoke_extension_tool"
    assert result.payload["tool_outcome"] == "unknown" and result.payload["execution_confirmed"] is False
    root = f"/api/v1/sessions/{session_id}/turns/{accepted['turn_id']}/message-stream"
    snapshot = await case.client.get(root + "/snapshot", params={"thread_id": thread_id})
    assert snapshot.status_code == 200, snapshot.text
    frozen_stream = snapshot.json()["data"]
    assert frozen_stream["stream_status"] == "failed"
    logs = await case.client.get(f"/api/v1/sessions/{session_id}/llm-request-logs")
    traces = await case.client.get(f"/api/v1/sessions/{session_id}/traces", params={"thread_id": thread_id})
    for response in (snapshot, logs, traces):
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
    public = json.dumps([job, result.payload, frozen_stream, logs.json(), traces.json(), case.requests()], ensure_ascii=False)
    assert location.name not in public and str(location) not in public
    checkpoints = list(case.container.checkpointer.list(None))
    assert location.name not in repr(checkpoints)
    storage = case.container.checkpointer._storage.storage
    with storage._connect(thread_id, session_id=session_id, read_only=True) as connection:
        assert location.name not in "\n".join(connection.iterdump())
    assert any(record.name == "app.core.failures" and record.exc_info and record.exc_info[1] is dependency_error for record in caplog.records)
    assert str(location) in caplog.text
    original_items = case.sql(session_id, thread_id, "SELECT item_id,content_hash FROM item_catalog ORDER BY item_sequence")

    # 只恢复外部依赖；原会话再次调用同一工具，旧失败和canonical身份保持。
    failing = False
    await case.plan(session_id, [reply, {"content": "工具依赖已恢复。"}])
    successor = await _send(case, session_id, "使用恢复后的网页工具。")
    successor_job = successor["job_id"]
    assert isinstance(successor_job, str)
    await _job(case, successor_job)
    assert len(observed) == 2 and case.call_count(session_id, thread_id) == 3
    original = await case.client.get(root + "/snapshot", params={"thread_id": thread_id})
    assert original.status_code == 200 and original.json()["data"] == frozen_stream
    assert case.sql(session_id, thread_id, "SELECT item_id,content_hash FROM item_catalog ORDER BY item_sequence")[:len(original_items)] == original_items
    case.write_actual(tool_name + "-dependency-failure", {"job": job, "tool_result": result.payload, "message_stream": frozen_stream})
