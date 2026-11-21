"""真实消息准入、工具执行与 HTTP 中断的精确 Thread 合同。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from boxteam_plugin_sdk.http import get_request_id
from fastapi import FastAPI

from app.api import messages as messages_api
from app.api import sessions as sessions_api
from app.api.deps import (
    get_session_orchestrator,
    get_session_stop_service,
    verify_local_token,
)
from app.core.execution_admission_context import require_current_execution_admission
from app.core.path_utils import workspace_context
from app.core.session_interrupt_state import SessionInterruptState
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.core.trace_middleware import TraceMiddleware
from app.domain.background_work import BackgroundTaskDescriptor
from app.schemas.internal_v2.common import JobStatus
from app.services.orchestration.message_stream.runtime import MessageStreamRuntime
from tests.integration.backend.sessions.test_runner_cancellation import _session
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    RuntimeCase,
    _child,
)
from tests.support.api_helpers import with_message_run_operation_id

pytest_plugins = ["tests.integration.backend.sessions.test_thread_runtime_runner"]


@pytest.mark.parametrize("pending_messages", ["keep", "discard"])
async def test_stop_settles_all_threads_preserves_history_and_allows_explicit_restart(
    runtime_case: RuntimeCase, interrupt_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch, pending_messages: str,
    gateway_skill_publication,
) -> None:
    case = runtime_case
    container = case.runner.container
    session_id, main = await _session(case.runner)
    child = await _child(case, session_id, creation_key="stop-child")
    other_session, other_thread = await _session(case.runner)
    release = asyncio.Event()
    entered = {(session_id, main): asyncio.Event(), (session_id, child): asyncio.Event(),
               (other_session, other_thread): asyncio.Event()}
    start_model = MessageStreamRuntime.start_model

    async def hold_model(runtime, *args, **kwargs):
        await start_model(runtime, *args, **kwargs)
        entered[(runtime.writer.session_id, runtime.writer.thread_id)].set()
        await release.wait()

    monkeypatch.setattr(MessageStreamRuntime, "start_model", hold_model)
    original_policy = container.session_stop_service._policy
    monkeypatch.setattr(container.session_stop_service, "_policy", lambda profile:
                        original_policy(profile).model_copy(update={"pending_messages": pending_messages}))
    settling, finish_settlement = asyncio.Event(), asyncio.Event()
    settle_jobs = container.job_service.settle_jobs

    async def hold_settlement(job_ids):
        settling.set()
        await finish_settlement.wait()
        await settle_jobs(job_ids)

    monkeypatch.setattr(container.job_service, "settle_jobs", hold_settlement)

    async def send(owner, thread, text):
        response = await interrupt_client.post(
            f"/api/v1/sessions/{owner}/threads/{thread}/messages",
            json=with_message_run_operation_id({"message": {"content": text}, "run": {}}),
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]

    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        try:
            active = []
            for (owner, thread), ready in entered.items():
                active.append(await send(owner, thread, "等待停止"))
                await asyncio.wait_for(ready.wait(), 8)
            queued = [await send(session_id, thread, "尚未执行的排队输入") for thread in (main, child)]
            descriptor = BackgroundTaskDescriptor(kind="emit_system_time_messages", version=1, payload={
                "interval_seconds": 60, "message_count": 2, "source_id": "stop-background",
                "started_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            })
            background = await container.background_task_registry.spawn(
                session_id=session_id, thread_id=child, agent_id="default",
                task_id="stop-background", descriptor=descriptor,
            )
            stopping = asyncio.create_task(interrupt_client.post(f"/api/v1/sessions/{session_id}/stop"))
            try:
                await asyncio.wait_for(settling.wait(), 8)
                for owner in (container.terminal_resource_owner, container.browser_resource_owner):
                    with pytest.raises(SessionDeletionPendingError):
                        async with owner.admit_thread(session_id, main):
                            pytest.fail("停止期间不得准入新资源操作")
                with pytest.raises(SessionDeletionPendingError):
                    async with container.node_debug_service._session_admission.admit(session_id, main):
                        pytest.fail("停止期间不得准入新调试操作")
                with pytest.raises(SessionDeletionPendingError):
                    await container.background_task_registry.spawn(
                        session_id=session_id, thread_id=main, agent_id="default",
                        task_id="during-stop", descriptor=descriptor,
                    )
                rejected = await interrupt_client.post(
                    f"/api/v1/sessions/{session_id}/threads/{main}/messages",
                    json=with_message_run_operation_id({"message": {"content": "停止期间的新输入"}, "run": {}}),
                )
                assert rejected.status_code == 409, rejected.text
            finally:
                finish_settlement.set()
                response = await stopping
            assert response.status_code == 200, response.text
            assert response.json()["data"]["status"] == "stopped"
            assert response.json()["request_id"] == response.headers["x-request-id"]
            assert container.background_task_registry.get_handle(session_id, background.task_id).status == "cancelled"
            for item in active[:2]:
                job = container.job_service._jobs[item["job_id"]]
                assert job.status == JobStatus.cancelled and job.task.done()
            other_job = container.job_service._jobs[active[2]["job_id"]]
            assert other_job.status == JobStatus.running and not other_job.task.done()
            for thread, item in zip((main, child), queued, strict=True):
                job = container.job_service._jobs[item["job_id"]]
                assert job.status == (JobStatus.queued if pending_messages == "keep" else JobStatus.cancelled)
                record = case.runner.saver.execution_queue_record(session_id, thread, item["job_id"])
                assert record.phase == ("queued" if pending_messages == "keep" else "terminal")
                if pending_messages == "discard":
                    assert record.outcome == "cancelled"
                assert await container.session_service.resolve_thread(session_id, thread) == thread
            await asyncio.sleep(0)
            assert all(container.job_service._jobs[item["job_id"]].status != JobStatus.running for item in queued)
            release.set()
            restarted = await send(session_id, main, "停止后的显式新操作")
            assert restarted["job_id"] not in {item["job_id"] for item in active + queued}
        finally:
            release.set()


@pytest.fixture
async def interrupt_client(runtime_case: RuntimeCase):
    container = runtime_case.runner.container
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(messages_api.router, prefix="/api/v1")
    application.include_router(sessions_api.router, prefix="/api/v1")
    application.dependency_overrides[get_session_orchestrator] = lambda: container.session_orchestrator
    application.dependency_overrides[get_session_stop_service] = lambda: container.session_stop_service
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
                response = await client.post(base + f"/threads/{thread}/messages", json=with_message_run_operation_id({"message": {"content": "读取文件并等待中断。"}, "run": {}}))
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
