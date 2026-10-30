"""控制状态拒绝经真实消息执行、Job 和冷消息流发布的边界。"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app.core.job_event_bus import EventType
from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.message import (
    MessageCreateRequest,
    MessageRunRequest,
    RunOptions,
)
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.job.service import JobService
from tests.integration.backend.sessions.test_thread_runtime_runner import RuntimeCase
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    cold_message_stream_client as cold_message_stream_client,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runner_case as runner_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runtime_case as runtime_case,  # noqa: PLC0414
)


@pytest.mark.parametrize("fault", ["shape", "session", "command"])
async def test_control_state_rejection_keeps_locations_out_of_execution_records(
    runtime_case: RuntimeCase,
    cold_message_stream_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fault: str,
) -> None:
    case = runtime_case
    container = case.runner.container
    jobs = container.job_service
    assert isinstance(jobs, JobService)
    runner = container.agent_execution_service._step_runner
    resolve = runner.ports.control_inbox_state_path
    damaged: list[tuple[Path, bytes]] = []

    def corrupt_state(session_id: str, turn_stream_id: str) -> Path:
        # 只在真实路径解析完成后制造损坏，不替测试生成 Session、准入或历史。
        path = resolve(session_id, turn_stream_id)
        assert path.is_relative_to(container.session_service.path_resolver.resolve_session_node(session_id))
        private = str(path)
        payload = [] if fault == "shape" else (
            {"session_id": private, "commands": []} if fault == "session" else
            {"session_id": session_id, "commands": [{private: "invalid-field"}]}
        )
        body = json.dumps(payload).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        damaged.append((path, body))
        return path

    caplog.set_level(logging.ERROR)
    session = await container.session_service.create(SessionCreateRequest(title="控制状态拒绝"))
    request = MessageRunRequest(message=MessageCreateRequest(content="验证控制状态错误"), run=RunOptions())
    with monkeypatch.context() as failure:
        failure.setattr(runner, "ports", replace(runner.ports, control_inbox_state_path=corrupt_state))
        accepted = await container.session_orchestrator.create_message(session.session_id, request)
        job = jobs._jobs[accepted.job_id]
        assert job.task is not None
        await asyncio.wait_for(asyncio.shield(job.task), 20)
    assert job.status == JobStatus.failed, job.error_message
    assert not case.model_requests
    (path, body), = damaged
    assert path.read_bytes() == body
    assert str(path) in caplog.text
    admission = container.checkpointer.execution_admission_for_job(
        session.session_id, thread_id=accepted.thread_id, job_id=accepted.job_id,
    )
    assert admission.turn_id == accepted.turn_id
    assert container.checkpointer.execution_queue_record(
        session.session_id, accepted.thread_id, accepted.job_id,
    ).phase == "terminal"
    assert container.agent_execution_service.execution_scope_registry.get_inbox(
        path.stem,
    ) is None

    root = f"/api/v1/sessions/{session.session_id}/turns/{accepted.turn_id}/message-stream"
    selector = {"thread_id": accepted.thread_id}
    response = await cold_message_stream_client.get(root + "/snapshot", params=selector)
    assert response.status_code == 200, response.text
    frozen = response.json()["data"]
    response = await cold_message_stream_client.get(root + "/events", params={
        **selector, "turn_stream_id": frozen["turn_stream_id"],
    })
    assert response.status_code == 200, response.text
    records = response.json()["data"]
    failures = [event.payload.model_dump(mode="json") for event in
                await container.job_event_bus.list_events(job.job_id, limit=1000)
                if event.type in {EventType.ERROR, EventType.JOB_FAILED}]
    assert failures
    assert frozen["stream_status"] == "failed"
    assert frozen["failure"]["code"] == "execution_error"
    expected = (await container.job_service.get(job.job_id)).error_message
    assert expected and "artifact=turn_control_state" in expected
    assert str(path) not in expected
    assert {failure["error"] for failure in failures} == {expected}
    assert frozen["failure"]["message"] == expected
    public = json.dumps({"job_error": job.error_message, "events": failures,
                         "snapshot": frozen, "records": records}, ensure_ascii=False)
    assert str(path) not in public
    storage = case.runner.saver._storage.storage
    with storage._connect(accepted.thread_id, session_id=session.session_id, read_only=True) as connection:
        assert str(path) not in "\n".join(connection.iterdump())
        original_rows = connection.execute("SELECT * FROM item_catalog ORDER BY item_sequence").fetchall()

    # 原损坏状态仍可诊断；下一消息仍从同一产品入口执行，不改写已失败的历史。
    successor = await container.session_orchestrator.create_message(session.session_id, request)
    next_job = jobs._jobs[successor.job_id]
    assert next_job.task is not None
    await asyncio.wait_for(asyncio.shield(next_job.task), 20)
    assert next_job.status == JobStatus.completed, next_job.error_message
    assert len(case.model_requests) == 1
    assert path.read_bytes() == body
    response = await cold_message_stream_client.get(root + "/snapshot", params=selector)
    assert response.json()["data"] == frozen
    with storage._connect(accepted.thread_id, session_id=session.session_id, read_only=True) as connection:
        assert connection.execute(
            "SELECT * FROM item_catalog WHERE item_sequence <= ? ORDER BY item_sequence",
            (original_rows[-1][0],),
        ).fetchall() == original_rows
