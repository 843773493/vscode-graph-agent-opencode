"""真实消息准入、工具执行与 HTTP 中断的精确 Thread 合同。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from app.api import messages as messages_api
from app.api import sessions as sessions_api
from app.api.deps import (
    get_request_id,
    get_session_interrupt_service,
    get_session_orchestrator,
    verify_local_token,
)
from app.core.execution_admission_context import require_current_execution_admission
from app.core.path_utils import workspace_context
from app.core.session_interrupt_state import SessionInterruptState
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.common import JobStatus
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime
from tests.integration.backend.sessions.test_runner_cancellation import _session
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    RuntimeCase,
    _child,
)

pytest_plugins = ["tests.integration.backend.sessions.test_thread_runtime_runner"]


@pytest.fixture
async def interrupt_client(runtime_case: RuntimeCase):
    container = runtime_case.runner.container
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(messages_api.router, prefix="/api/v1")
    application.include_router(sessions_api.router, prefix="/api/v1")
    application.dependency_overrides[get_session_orchestrator] = lambda: container.session_orchestrator
    application.dependency_overrides[get_session_interrupt_service] = lambda: container.session_interrupt_service
    application.dependency_overrides[verify_local_token] = lambda: "local-test"
    application.dependency_overrides[get_request_id] = lambda: "request-thread-interrupt"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
        headers={"X-BoxTeam-Gateway-Id": "gateway_0123456789abcdef0123456789abcdef", "X-Request-ID": "request-thread-interrupt"},
    ) as client:
        yield client


async def test_main_interrupt_never_selects_child_and_explicit_child_is_isolated(
    runtime_case: RuntimeCase, interrupt_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch,
    gateway_skill_publication,
) -> None:
    case = runtime_case
    container = case.runner.container
    container.job_service._job_timeout_seconds = 60
    session_id, main = await _session(case.runner)
    child = await _child(case, session_id, creation_key="interrupt-child")
    (case.runner.workspace / "interrupt-probe.txt").write_text("中断前的真实工具结果。\n")
    entered = {thread: asyncio.Event() for thread in (main, child)}
    release = {thread: asyncio.Event() for thread in (main, child)}
    calls: dict[str, int] = {}
    start_tool = MessageStreamRuntime.start_tool

    def model_reply(_payload):
        admission = require_current_execution_admission()
        count = calls.get(admission.execution_id, 0) + 1
        calls[admission.execution_id] = count
        if count == 1:
            return [
                {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{
                    "index": 0, "id": f"interrupt-read-{admission.thread_id}", "type": "function",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "interrupt-probe.txt"})},
                }]}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
        return [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "执行完成。"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]

    async def pause_started(runtime, **kwargs):
        result = await start_tool(runtime, **kwargs)
        thread = runtime.writer.thread_id
        entered[thread].set()
        await release[thread].wait()
        return result

    case.response_chunks = model_reply
    monkeypatch.setattr(MessageStreamRuntime, "start_tool", pause_started)
    client = interrupt_client
    base = f"/api/v1/sessions/{session_id}"
    accepted = {}
    committed_results = {}
    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        try:
            async def start(thread):
                response = await client.post(base + f"/threads/{thread}/messages", json={"message": {"content": "读取文件并等待中断。"}, "run": {}})
                assert response.status_code == 200, response.text
                accepted[thread] = response.json()["data"]
                await asyncio.wait_for(entered[thread].wait(), 8)
                async with asyncio.timeout(3):
                    while not SessionInterruptState.get(session_id, thread_id=thread).active_tool_names:
                        await asyncio.sleep(0.01)
                # 真实工具已写入 canonical，而事件消费者仍停在 tool.started。
                # 中断必须找回真实结果，不能再提交同 attempt 的未知正文。
                async with asyncio.timeout(8):
                    while True:
                        results = [item for item in case.runner.saver._storage.storage.read_items(session_id, thread)
                                   if item.semantic_kind == "tool_result"]
                        if results:
                            break
                        await asyncio.sleep(0.02)
                assert len(results) == 1
                assert results[0].payload["tool_outcome"] == "success"
                assert "中断前的真实工具结果" in results[0].payload["content"]
                committed_results[thread] = results[0].to_dict()

            await start(child)
            child_state = SessionInterruptState.get(session_id, thread_id=child)
            child_tools = dict(child_state.active_tools_by_run_id)
            child_job = container.job_service._jobs[accepted[child]["job_id"]]
            child_status = child_job.status
            assert child_status == JobStatus.running
            no_main = await client.post(base + "/interrupt")
            assert no_main.status_code == 404, no_main.text
            assert SessionInterruptState.get(session_id, thread_id=child).interrupt_request_id is None
            assert child_job.status == child_status

            await start(main)
            interrupted = await client.post(base + "/interrupt")
            assert interrupted.status_code == 200, interrupted.text
            result = interrupted.json()["data"]
            assert (result["session_id"], result["thread_id"], result["job_id"]) == (session_id, main, accepted[main]["job_id"])
            assert result["phase"] == "tool" and result["tool_name"] == "read_file"
            main_job = container.job_service._jobs[accepted[main]["job_id"]]
            await asyncio.wait_for(asyncio.gather(main_job.task, return_exceptions=True), 8)
            assert main_job.status == JobStatus.cancelled
            child_state = SessionInterruptState.get(session_id, thread_id=child)
            assert child_state.active_tools_by_run_id == child_tools
            assert child_state.interrupt_request_id is None
            assert child_state.cancellation_reason is None
            assert not SessionInterruptState.get(session_id, thread_id=main).active_tool_names

            interrupted_child = await client.post(base + "/interrupt", params={"thread_id": child})
            assert interrupted_child.status_code == 200, interrupted_child.text
            result_child = interrupted_child.json()["data"]
            assert (result_child["thread_id"], result_child["job_id"]) == (child, accepted[child]["job_id"])
            assert result_child["interrupt_request_id"] != result["interrupt_request_id"]
            child_job = container.job_service._jobs[accepted[child]["job_id"]]
            await asyncio.wait_for(asyncio.gather(child_job.task, return_exceptions=True), 8)
            assert child_job.status == JobStatus.cancelled
            for thread in (main, child):
                assert SessionInterruptState.get(session_id, thread_id=thread).is_default()
                stream_ids = await container.message_stream_store.existing_stream_ids(session_id=session_id, thread_id=thread, turn_ids=[accepted[thread]["turn_id"]])
                stream_id = stream_ids[accepted[thread]["turn_id"]]
                snapshot = await container.message_stream_store.get_state(stream_id)
                assert snapshot["stream_status"] == "interrupted"
                assert len(snapshot["tool_executions"]) == 1
                assert snapshot["tool_executions"][0]["outcome"] == "success"
                results = [item for item in case.runner.saver._storage.storage.read_items(session_id, thread)
                           if item.semantic_kind == "tool_result"]
                original = committed_results[thread]
                assert [item.to_dict() for item in results if item.item_id == original["item_id"]] == [original]
                # request carrier 和真实 attempt 共用已知正文，收尾只补齐 attempt 身份。
                attempts = [item for item in results if "tool_attempt_id" in item.payload]
                assert len(attempts) == 1
                assert all(item.payload["tool_outcome"] == "success" for item in results)
                assert all(item.payload["content"] == original["payload"]["content"] for item in results)
        finally:
            for signal in release.values():
                signal.set()
