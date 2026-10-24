"""真实内部消息准入、模型执行和 HTTP 中断的持久终结裁决。"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import closing

import httpx
import pytest

from app.core.path_utils import workspace_context
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.core.session_interrupt_state import SessionInterruptState
from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.prompting import PromptSection, internal_message_factory
from app.schemas.internal_v2.common import JobStatus
from app.services.orchestration.execution_step.model_call import StepModelCallAdapter
from tests.integration.backend.sessions.test_runner_cancellation import _session
from tests.integration.backend.sessions.test_thread_runtime_runner import RuntimeCase

pytest_plugins = ["tests.integration.backend.sessions.test_thread_interrupt_identity"]


@pytest.mark.parametrize("winner", ["completion", "interrupt"])
async def test_internal_http_interrupt_and_final_commit_have_one_winner(
    runtime_case: RuntimeCase, interrupt_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch, gateway_skill_publication, winner: str,
) -> None:
    case = runtime_case
    container = case.runner.container
    session_id, thread_id = await _session(case.runner)
    entered, release = threading.Event(), threading.Event()
    converge = StepModelCallAdapter.converge_final_message

    def pause_final(adapter, message):
        if winner == "completion":
            converge(adapter, message)
        entered.set()
        if not release.wait(15):
            raise TimeoutError("终结裁决测试未释放模型提交屏障")
        if winner == "interrupt":
            converge(adapter, message)

    monkeypatch.setattr(StepModelCallAdapter, "converge_final_message", pause_final)
    prepared = internal_message_factory.build(
        kind="generated_session_result", control="处理内部任务结果。",
        sections=(PromptSection("control_context", {"source": "interrupt_product"}),
                  PromptSection("generated_session_result", "产品入口产生的内部输入。")),
        display_content="内部任务结果。",
    )
    gateway = set_current_gateway_id("gateway_0123456789abcdef0123456789abcdef")
    request = set_current_request_id("internal-interrupt-product")
    with workspace_context(container.workspace_id, case.runner.workspace, session_catalog=container.session_catalog):
        try:
            accepted = await container.runtime_source_dispatcher.submit(
                session_id, thread_id, prepared, source_id="interrupt-product-source",
                idempotency_key="interrupt-product-operation",
            )
            job = container.job_service._jobs[accepted.job_id]
            assert await asyncio.to_thread(entered.wait, 10)
            assert accepted.turn_id is None
            admission = container.message_service.execution_admission_for_job(
                session_id, thread_id=thread_id, job_id=job.job_id,
            )
            storage = case.runner.saver._storage.storage
            before = storage.read_items(session_id, thread_id)
            interrupt_task = asyncio.create_task(interrupt_client.post(
                f"/api/v1/sessions/{session_id}/interrupt", params={"thread_id": thread_id},
            ))
            if winner == "interrupt":
                async with asyncio.timeout(8):
                    while case.runner.saver.internal_execution_interrupt(admission) is None:
                        await asyncio.sleep(0.01)
            else:
                response = await asyncio.wait_for(asyncio.shield(interrupt_task), 8)
                assert response.status_code == 200, response.text
                assert response.json()["data"]["status"] == "completed"
                assert case.runner.saver.internal_execution_interrupt(admission) is None
                assert SessionInterruptState.get(session_id, thread_id=thread_id).interrupt_request_id is None
            release.set()
            response = await asyncio.wait_for(interrupt_task, 8)
            assert response.status_code == 200, response.text
            await asyncio.wait_for(asyncio.gather(job.task, return_exceptions=True), 10)
            expected = "completed" if winner == "completion" else "cancelled"
            assert job.status == (JobStatus.completed if winner == "completion" else JobStatus.cancelled)
            assert case.runner.saver.runtime_execution_outcome(session_id, thread_id, admission.execution_id) == expected
            assert len(case.model_requests) == 1
            with storage.read_connection(session_id, thread_id, "") as connection:
                assert connection.execute("SELECT count(*) FROM turn_records").fetchone() == (0,)
                events = connection.execute(
                    "SELECT payload_json FROM control_events WHERE control_kind='interrupt.requested' "
                    "AND entity_type='internal_execution'",
                ).fetchall()
                assert len(events) == (1 if winner == "interrupt" else 0)
                if events:
                    event = json.loads(events[0][0])
                    assert event["request"]["admission"]["execution_id"] == admission.execution_id
                    assert event["request"]["admission"]["job_id"] == job.job_id
                    assert event["request"]["admission"]["thread_id"] == thread_id
                    assert event["request"]["interrupt_request_id"] == response.json()["data"]["interrupt_request_id"]
                    metadata = json.loads(connection.execute(
                        "SELECT metadata_json FROM storage_commits WHERE commit_kind='terminal_convergence' "
                        "AND subject_id=? ORDER BY commit_id DESC LIMIT 1", (admission.execution_id,),
                    ).fetchone()[0])
                    assert metadata == {"after_interrupt_requested": True,
                                        "interrupt_request_id": event["request"]["interrupt_request_id"]}
            with closing(SessionControlStore(container.session_service.path_resolver.resolve_session_node(session_id) / CONTROL_DATABASE_NAME)) as control:
                assert not [lease for lease in control.list_non_terminal_leases()
                            if lease.operation_identity.startswith("internal-interrupt:")]
            if winner == "completion":
                assert storage.read_items(session_id, thread_id) == before
            else:
                reminder = case.runner.saver.internal_execution_interrupt(admission)
                assert reminder is not None
                items = storage.read_items(session_id, thread_id)
                assert len([item for item in items if item.item_id == reminder.reminder_item_id]) == 1
                assert not [item for item in items if item.semantic_kind == "assistant_output" and item.status == "completed"
                            and item.item_id not in {prior.item_id for prior in before}]
            assert SessionInterruptState.get(session_id, thread_id=thread_id).is_default()
        finally:
            release.set()
            reset_current_request_id(request)
            reset_current_gateway_id(gateway)
