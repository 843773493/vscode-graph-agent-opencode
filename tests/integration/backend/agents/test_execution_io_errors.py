"""原生 I/O 失败经真实执行、Job 和冷消息流发布的公开边界。"""

from __future__ import annotations

import asyncio
import json
import logging

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
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    RuntimeCase,
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    cold_message_stream_client as cold_message_stream_client,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runner_case as runner_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runtime_case as runtime_case,  # noqa: PLC0414
)


async def test_native_io_failure_converges_without_publishing_file_locations(
    runtime_case: RuntimeCase,
    cold_message_stream_client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    case = runtime_case
    container = case.runner.container
    location = case.runner.workspace / "private-native-io-location"
    location.mkdir()
    errors: list[OSError] = []

    def fail_model(_payload: dict[str, object]) -> list[dict[str, object]]:
        try:
            location.read_bytes()
        except OSError as error:
            errors.append(error)
            raise
        raise AssertionError("读取目录必须失败")

    case.response_chunks = fail_model
    caplog.set_level(logging.ERROR)
    session = await container.session_service.create(SessionCreateRequest(title="原生 I/O 失败"))
    request = MessageRunRequest(message=MessageCreateRequest(content="验证失败收口"), run=RunOptions())
    accepted = await container.session_orchestrator.create_message(session.session_id, request)
    job = container.job_service._jobs[accepted.job_id]
    await asyncio.wait_for(asyncio.shield(job.task), 20)
    assert job.status == JobStatus.failed, job.error_message
    assert len(errors) == 1 and isinstance(errors[0], IsADirectoryError)
    events = await container.job_event_bus.list_events(job.job_id, limit=1000)
    failures = [event.payload.model_dump(mode="json") for event in events
                if event.type in {EventType.ERROR, EventType.JOB_FAILED}]
    assert failures
    root = f"/api/v1/sessions/{session.session_id}/turns/{accepted.turn_id}/message-stream"
    selector = {"thread_id": accepted.thread_id}
    snapshot = await cold_message_stream_client.get(root + "/snapshot", params=selector)
    assert snapshot.status_code == 200, snapshot.text
    frozen = snapshot.json()["data"]
    response = await cold_message_stream_client.get(root + "/events", params={
        **selector, "turn_stream_id": frozen["turn_stream_id"],
    })
    assert response.status_code == 200, response.text
    records = response.json()["data"]
    model_failure, = [record for record in records if record["type"] == "model.failed"]
    assert frozen["stream_status"] == "failed"
    assert frozen["failure"]["code"] == "execution_error"
    assert model_failure["payload"]["error_code"] == "execution_error"
    expected = f"执行系统 I/O 失败: error=IsADirectoryError, errno={errors[0].errno}"
    assert (await container.job_service.get(job.job_id)).error_message == expected
    assert {failure["error"] for failure in failures} == {expected}
    assert frozen["failure"]["message"] == model_failure["payload"]["message"] == expected
    public = json.dumps({"job_error": job.error_message, "job_events": failures,
                         "snapshot": frozen, "events": records}, ensure_ascii=False)
    assert str(location) not in public
    assert location.name not in public
    assert str(location) in caplog.text
    assert any(record.exc_info and record.exc_info[1] is errors[0] for record in caplog.records)

    storage = case.runner.saver._storage.storage
    with storage._connect(accepted.thread_id, session_id=session.session_id, read_only=True) as connection:
        assert location.name not in "\n".join(connection.iterdump())
        original_items = connection.execute("SELECT * FROM item_catalog ORDER BY item_sequence").fetchall()
    queue = container.checkpointer.execution_queue_record(
        session.session_id, accepted.thread_id, accepted.job_id,
    )
    assert queue.phase == "terminal"
    log_root = container.session_service.path_resolver.resolve_session_node(session.session_id) / "logs/llm_requests"
    logs = [json.loads(path.read_text(encoding="utf-8")) for path in log_root.glob("*.json")]
    failure_log, = [log for log in logs if log["job_id"] == job.job_id]
    assert failure_log["response"]["error"] == expected
    assert location.name not in json.dumps(failure_log, ensure_ascii=False)

    # 同一 thread 经产品入口继续，旧失败及 canonical 前缀仍可冷读且不被改写。
    case.response_chunks = None
    successor = await container.session_orchestrator.create_message(session.session_id, request)
    next_job = container.job_service._jobs[successor.job_id]
    await asyncio.wait_for(asyncio.shield(next_job.task), 20)
    assert next_job.status == JobStatus.completed, next_job.error_message
    reloaded = await cold_message_stream_client.get(root + "/snapshot", params=selector)
    assert reloaded.json()["data"] == frozen
    with storage._connect(accepted.thread_id, session_id=session.session_id, read_only=True) as connection:
        assert connection.execute(
            "SELECT * FROM item_catalog WHERE item_sequence <= ? ORDER BY item_sequence",
            (original_items[-1][0],),
        ).fetchall() == original_items
