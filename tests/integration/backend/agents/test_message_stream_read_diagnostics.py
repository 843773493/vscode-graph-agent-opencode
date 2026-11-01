"""产品历史的三个读取入口在诊断日志失败时仍拒绝原损坏并可恢复。"""

from __future__ import annotations

import asyncio
import json
import logging
from traceback import format_exception

import httpx
import pytest

from app import main
from app.api.deps import get_message_stream_store
from app.services.business.job.service import JobService
from app.services.infrastructure.message_stream_store import MessageStreamStore
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 原产品应用与唯一 model_stream 装具
)
from tests.support.api_helpers import create_session
from tests.support.sealed_request_audit import audit_sealed_requests


@pytest.mark.parametrize("sink", ["not_directory", "missing"])
async def test_product_read_rejection_keeps_primary_error_when_diagnostics_fail(
    source_case: RuntimeSourceCase, sink: str,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    case = source_case
    session = await create_session(case.client, "消息流读取及诊断双错")
    await case.plan(session, [{"content": "原执行生成的消息历史。"}])
    accepted = await _send(case, session, "生成一轮完整历史。")
    thread, turn, job_id = accepted["thread_id"], accepted["turn_id"], accepted["job_id"]
    assert isinstance(thread, str) and isinstance(turn, str) and isinstance(job_id, str)
    await _job(case, job_id)
    assert isinstance(case.container.job_service, JobService)
    await case.container.job_service.drain_finalizations()
    streams = case.container.message_stream_store
    if streams._snapshot_tasks:
        await asyncio.gather(*streams._snapshot_tasks.values())
    base = f"/api/v1/sessions/{session}/turns/{turn}/message-stream"
    response = await case.client.get(base + "/events")
    assert response.status_code == 200, response.text
    expected = response.json()["data"]
    assert expected[-1]["type"] == "stream.completed"
    stream_id = expected[0]["turn_stream_id"]
    resolver = case.container.session_service.path_resolver
    canonical = resolver.resolve_thread_node(session, thread) / "rollout/rollout.jsonl"
    canonical_bytes = canonical.read_bytes()
    paths = {
        "events": streams._stream_path(session, stream_id),
        "state": streams._state_path(session, stream_id),
        "index": streams._index_path(session),
        "applied_event_ids": streams._applied_event_ids_path(session, stream_id),
    }
    originals = {name: path.read_bytes() if path.exists() else None for name, path in paths.items()}
    blocker = case.workspace / "diagnostic-sink-secret"
    if sink == "not_directory":
        blocker.write_text("普通文件不能作为日志目录")
    destination = blocker / "service.log"
    sink_errors: list[OSError] = []
    emit = caplog.handler.emit

    def capture(record: logging.LogRecord) -> None:
        emit(record)
        if record.name == "app.services.infrastructure.message_stream_store":
            try:
                destination.write_text(record.getMessage())
            except OSError as error:
                sink_errors.append(error)
                raise

    results = []
    cold: MessageStreamStore

    def cold_reader() -> MessageStreamStore:
        return cold

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app, raise_app_exceptions=False),
        base_url=case.client.base_url, headers=case.client.headers,
    ) as client:
        for artifact, target in paths.items():
            marker = str(case.workspace / f"rejected-{artifact}-secret")
            original = originals[artifact]
            if artifact == "events":
                assert original is not None
                lines = original.splitlines(keepends=True)
                raw = json.loads(lines[0])
                raw["event"]["session_id"] = marker
                damaged = (json.dumps(raw) + "\n").encode() + b"".join(lines[1:])
            elif artifact == "state":
                assert original is not None
                raw = json.loads(original)
                raw["thread_id"] = marker
                damaged = json.dumps(raw).encode()
            elif artifact == "index":
                damaged = json.dumps({"format_version": marker}).encode()
            else:
                damaged = json.dumps({marker: marker}).encode()
            cold = MessageStreamStore(path_resolver=resolver)
            try:
                target.write_bytes(damaged)
                # 原执行已经结束。仅将读取依赖指向同一 owner 的真实冷 reader，不创建第二 writer。
                with monkeypatch.context() as patch, caplog.at_level(logging.ERROR):
                    patch.setitem(main.app.dependency_overrides, get_message_stream_store, cold_reader)
                    patch.setattr(caplog.handler, "emit", capture)
                    for suffix in ("/events", "/snapshot", ""):
                        start = len(caplog.records)
                        failures_before = len(sink_errors)
                        failed = await client.get(base + suffix)
                        assert failed.status_code == 404, failed.text
                        assert failed.headers["X-Request-ID"] == failed.json()["request_id"]
                        assert f"session_id={session}" in failed.text and f"artifact={artifact}" in failed.text
                        assert "diagnostic_sink_error=" + type(sink_errors[-1]).__name__ in failed.text
                        assert marker not in failed.text and str(case.workspace) not in failed.text
                        assert len(sink_errors) == failures_before + 1
                        records = [record for record in caplog.records[start:]
                                   if record.name == "app.services.infrastructure.message_stream_store"]
                        record, = records
                        assert record.exc_info is not None and record.exc_info[1] is not None
                        diagnostic = "".join(format_exception(record.exc_info[1]))
                        assert marker in diagnostic and str(sink_errors[-1]) in diagnostic
                        assert stream_id not in cold._states and stream_id not in cold._event_ids_loaded
                        assert target.read_bytes() == damaged and canonical.read_bytes() == canonical_bytes
                        results.append({"artifact": artifact, "suffix": suffix, "response": failed.json(),
                                        "diagnostic": diagnostic})
            finally:
                if original is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(original)
            with monkeypatch.context() as patch:
                patch.setitem(main.app.dependency_overrides, get_message_stream_store, cold_reader)
                reopened = await client.get(base + "/events")
            assert reopened.status_code == 200, reopened.text
            assert reopened.json()["data"] == expected
            assert canonical.read_bytes() == canonical_bytes
    assert len(results) == 12 and len(sink_errors) == 12
    await case.plan(session, [{"content": "原线程继续完成。"}])
    resumed = await _send(case, session, "诊断恢复后继续。")
    resumed_job = resumed["job_id"]
    assert isinstance(resumed_job, str)
    assert resumed["thread_id"] == thread and resumed["turn_id"] != turn
    await _job(case, resumed_job)
    await case.container.job_service.drain_finalizations()
    reopened = await case.client.get(base + "/events")
    assert reopened.status_code == 200 and reopened.json()["data"] == expected
    assert canonical.read_bytes().startswith(canonical_bytes)
    captures = {call: json.loads((case.model.artifacts / (call + ".json")).read_text())
                for call in case.planned_calls}
    proof = audit_sealed_requests(
        canonical.with_name("index.sqlite"), (resolver.resolve_session_node(session) / "logs/llm_requests").glob("*.json"), captures,
    )
    audited = proof["requests"]
    assert isinstance(audited, list) and len(audited) == 2
    case.write_actual("message-stream-read-diagnostics-" + sink, {
        "accepted": accepted, "expected": expected, "results": results, "resumed": resumed,
        "sealed_requests": proof,
    })
