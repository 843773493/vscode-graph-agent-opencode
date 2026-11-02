"""内部校验与原生 I/O 失败经执行、Job 和冷消息流发布的公开边界。"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from langchain_core.messages import AIMessageChunk
from pydantic import TypeAdapter, ValidationError

from app import main
from app.api.deps import get_message_stream_store
from app.core.job_event_bus import EventType
from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.message import (
    MessageCreateRequest,
    MessageRunRequest,
    RunOptions,
)
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 复用现有 model_stream 及完整应用生命周期
)
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
from tests.support.api_helpers import create_session
from tests.support.sealed_request_audit import audit_sealed_requests


@pytest.mark.parametrize("text_only", [False, True], ids=["native", "text_only"])
async def test_native_io_failure_converges_with_known_file_diagnostics(
    runtime_case: RuntimeCase,
    cold_message_stream_client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
    text_only: bool,
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
            if text_only:
                wrapped = OSError(str(error))
                errors.append(wrapped)
                raise wrapped from error
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
    assert len(errors) == 1
    error, = errors
    if text_only:
        assert type(error) is OSError
        assert error.errno is error.filename is error.filename2 is None
        assert isinstance(error.__cause__, IsADirectoryError)
        assert error.__cause__.filename == str(location)
    else:
        assert isinstance(error, IsADirectoryError)
        assert error.filename == str(location)
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
    expected = f"执行系统 I/O 失败: error={type(error).__name__}, errno={error.errno}"
    if not text_only:
        expected += f", reason={error.strerror}, filename={str(location)!r}"
    assert (await container.job_service.get(job.job_id)).error_message == expected
    assert {failure["error"] for failure in failures} == {expected}
    assert frozen["failure"]["message"] == model_failure["payload"]["message"] == expected
    public = json.dumps({"job_error": job.error_message, "job_events": failures,
                         "snapshot": frozen, "events": records}, ensure_ascii=False)
    assert (location.name in public) is (not text_only)
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
    assert (location.name in json.dumps(failure_log, ensure_ascii=False)) is (not text_only)

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


async def test_internal_validation_failure_keeps_job_and_cold_history_free_of_rejected_locations(
    source_case: RuntimeSourceCase, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    case = source_case
    locations = ("/private/validation-secret.sqlite", r"C:\private\validation-secret.sqlite",
                 r"\\target\private\validation-secret.sqlite")
    with pytest.raises(ValidationError) as caught:
        TypeAdapter(dict[str, int]).validate_python(dict.fromkeys(locations, "invalid-internal-value"))
    error = caught.value
    rejected = error.errors(include_url=False)
    original_accept = MessageStreamRuntime.accept_message_chunk
    failed_calls: list[str] = []

    async def accept_then_fail(
        runtime: MessageStreamRuntime, chunk: AIMessageChunk, *, model_call_id: str | None = None,
    ) -> None:
        await original_accept(runtime, chunk, model_call_id=model_call_id)
        # 真实模型增量先沿原链发布；只在该消费边界注入内部结构校验故障。
        if runtime.current_model_call_id is not None and chunk.content and not failed_calls:
            failed_calls.append(runtime.current_model_call_id)
            raise error

    session_id = await create_session(case.client, "内部校验失败的公开事实")
    call, = await case.plan(session_id, [{"content": "模型返回正常正文，由内部消费边界触发故障。"}])
    with monkeypatch.context() as patch:
        patch.setattr(MessageStreamRuntime, "accept_message_chunk", accept_then_fail)
        with caplog.at_level(logging.ERROR):
            accepted = await _send(case, session_id, "验证内部结构校验失败")
            job_id = accepted["job_id"]
            assert isinstance(job_id, str)
            job = await _job(case, job_id, expected="failed")
    assert failed_calls == [call]
    thread_id = accepted["thread_id"]
    assert isinstance(thread_id, str)
    expected = "执行数据校验失败: error=ValidationError"
    assert job["error_message"] == expected
    assert error.errors(include_url=False) == rejected
    assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)
    assert all(location in caplog.text for location in locations)
    events = await case.container.job_event_bus.list_events(job_id, limit=1000)
    failures = [event.payload.model_dump(mode="json") for event in events
                if event.type in {EventType.ERROR, EventType.JOB_FAILED}]
    assert failures and {failure["error"] for failure in failures} == {expected}

    resolver = case.container.session_service.path_resolver
    cold = MessageStreamStore(path_resolver=resolver)
    root = f"/api/v1/sessions/{session_id}/turns/{accepted['turn_id']}/message-stream"
    with monkeypatch.context() as patch:
        patch.setitem(main.app.dependency_overrides, get_message_stream_store, lambda: cold)
        snapshot = await case.client.get(root + "/snapshot", params={"thread_id": thread_id})
        response = await case.client.get(root + "/events", params={"thread_id": thread_id})
    assert snapshot.status_code == response.status_code == 200
    assert snapshot.headers["X-Request-ID"] == snapshot.json()["request_id"]
    frozen = snapshot.json()["data"]
    records = response.json()["data"]
    model_failure, = [record for record in records if record["type"] == "model.failed"]
    assert model_failure["model_call_id"] == call and model_failure["thread_id"] == thread_id
    assert frozen["stream_status"] == "failed"
    assert frozen["failure"]["message"] == model_failure["payload"]["message"] == expected
    assert frozen["failure"]["code"] == model_failure["payload"]["error_code"] == "execution_error"
    assert "validation-secret.sqlite" not in json.dumps([job, failures, frozen, records], ensure_ascii=False)
    storage = case.container.checkpointer._storage.storage
    with storage._connect(thread_id, session_id=session_id, read_only=True) as connection:
        assert "validation-secret.sqlite" not in "\n".join(connection.iterdump())
        original_items = connection.execute("SELECT * FROM item_catalog ORDER BY item_sequence").fetchall()
    assert original_items
    log_root = resolver.resolve_session_node(session_id) / "logs/llm_requests"
    log_paths = list(log_root.glob("*.json"))
    assert log_paths and all("validation-secret.sqlite" not in path.read_text(encoding="utf-8") for path in log_paths)
    capture = json.loads((case.model.artifacts / (call + ".json")).read_text(encoding="utf-8"))
    first_audit = audit_sealed_requests(storage.root(session_id, thread_id=thread_id) / "index.sqlite",
                                       log_paths, {call: capture})

    successor_call, = await case.plan(session_id, [{"content": "原线程恢复后继续执行。"}])
    successor = await _send(case, session_id, "从原线程继续")
    assert successor["thread_id"] == thread_id
    successor_id = successor["job_id"]
    assert isinstance(successor_id, str)
    await _job(case, successor_id)
    restored = await case.client.get(root + "/snapshot", params={"thread_id": thread_id})
    assert restored.status_code == 200 and restored.json()["data"] == frozen
    with storage._connect(thread_id, session_id=session_id, read_only=True) as connection:
        assert connection.execute("SELECT * FROM item_catalog WHERE item_sequence<=? ORDER BY item_sequence",
                                  (original_items[-1][0],)).fetchall() == original_items
    captures = {key: json.loads((case.model.artifacts / (key + ".json")).read_text(encoding="utf-8"))
                for key in (call, successor_call)}
    final_audit = audit_sealed_requests(storage.root(session_id, thread_id=thread_id) / "index.sqlite",
                                       log_root.glob("*.json"), captures)
    assert len(case.requests()) == 2
    case.write_actual("validation-failure", {"job": job, "snapshot": frozen, "events": records,
                                            "sealed_before": first_audit, "sealed_after": final_audit})
