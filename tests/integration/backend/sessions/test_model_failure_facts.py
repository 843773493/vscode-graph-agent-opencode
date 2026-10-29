"""真实模型拒绝经事件、canonical 和普通查询传播时保留失败事实。"""

from __future__ import annotations

import json
import logging
from typing import cast

import commentjson
import pytest

from app.testing.model_stream import StreamFrame, build_cassette, done_frame
from tests.integration.backend.sessions.test_persistent_tasks import _target
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    source_case as source_case,  # noqa: PLC0414 - 沿当前测试文件隔离工作区。
)


@pytest.mark.parametrize("source_case", [{"managed_gateway": True}], indirect=True)
async def test_provider_rejection_keeps_native_diagnostics_out_of_history_and_traces(
    source_case: RuntimeSourceCase, caplog: pytest.LogCaptureFixture,
) -> None:
    case = source_case
    session_id, thread_id, _ = await _target(case)
    config = commentjson.loads((case.workspace / ".boxteam/workspace.jsonc").read_text(encoding="utf-8"))
    endpoint = next(provider["endpoint"] for provider in config["llm"]["providers"] if provider["id"] == "primary")
    locations = ("/private/provider-request.sqlite", r"C:\private\provider-request.sqlite", r"\\target\private\provider-request.sqlite")
    cassette = build_cassette(
        asset_id="provider-rejection", model="e2e-stub-model", url=endpoint + "/chat/completions",
        frames=[StreamFrame(kind="data", encoding="json", payload={
            "error": {"type": "permission_error", "message": "原始 Provider 拒绝: " + " | ".join(locations)},
        }), done_frame()],
    )
    interaction, = cast(list[dict[str, object]], cassette.raw["interactions"])
    call, = case.control.plan_model_calls(session_id, thread_id, [interaction])
    case.planned_calls[call] = {"session_id": session_id, "thread_id": thread_id, "model_call_id": call}
    with caplog.at_level(logging.ERROR):
        accepted = await _send(case, session_id, "明确报告模型服务拒绝。")
        job_id = accepted["job_id"]
        assert isinstance(job_id, str)
        job = await _job(case, job_id, expected="failed")
    assert case.call_count(session_id, thread_id) == 1
    events = case.container.trace_event_store.read_events(session_id)
    failure, = [event.payload.model_dump(mode="json") for event in events if event.type == "model_failed"]
    assert failure["provider_id"] == "primary" and failure["model"] == "e2e-stub-model"
    assert "stage=model-provider" in failure["error"] and failure["error_type"] in failure["error"]
    items = case.container.checkpointer._storage.storage.read_items(session_id, thread_id)
    calls = case.sql(session_id, thread_id, "SELECT model_call_id,outcome FROM model_calls")
    assert calls == [(call, "failed")]
    stream_response = await case.client.get(
        f"/api/v1/sessions/{session_id}/turns/{accepted['turn_id']}/message-stream/events",
        params={"thread_id": thread_id},
    )
    assert stream_response.status_code == 200, stream_response.text
    assert stream_response.headers["X-Request-ID"] == stream_response.json()["request_id"]
    stream_error, = [event for event in stream_response.json()["data"] if event["type"] == "model.failed"]
    assert stream_error["thread_id"] == thread_id and stream_error["model_call_id"] == call
    assert stream_error["payload"] == {
        "model_call_id": call, "attempt": 1, "outcome": "upstream_error",
        "error_code": failure["error_type"], "message": failure["error"], "retryable": True,
    }
    response = await case.client.get(f"/api/v1/sessions/{session_id}/traces", params={"thread_id": thread_id})
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    logs_response = await case.client.get(f"/api/v1/sessions/{session_id}/llm-request-logs")
    assert logs_response.status_code == 200, logs_response.text
    assert logs_response.headers["X-Request-ID"] == logs_response.json()["request_id"]
    log, = logs_response.json()["data"]
    assert log["job_id"] == job_id
    assert "stage=execution-provider" in log["response"]["error"]
    attempt, = log["upstream"]["attempts"]
    assert attempt["error"] == log["response"]["error"] and attempt["response"] is None
    checkpoints = list(case.container.checkpointer.list(None))
    serialized = json.dumps([job, failure, calls, response.json(), stream_response.json(), logs_response.json(),
                             [item.payload for item in items]], ensure_ascii=False)
    assert "provider-request.sqlite" not in serialized and "provider-request.sqlite" not in repr(checkpoints)
    diagnostic, = [record for record in caplog.records if record.name == "app.core.failures"
                   and "stage=model-provider" in record.getMessage()]
    assert diagnostic.exc_info is not None
    raw_error = diagnostic.exc_info[1]
    assert raw_error is not None and type(raw_error).__name__ == failure["error_type"]
    assert str(raw_error) in caplog.text and all(location in str(raw_error) for location in locations)
    assert len(case.requests()) == 1

    # 修复后从原产品入口继续执行，旧失败记录及其归属不应被改写。
    retry, = await case.plan(session_id, [{"content": "原会话恢复后继续执行。"}])
    resumed = await _send(case, session_id, "服务恢复后继续。")
    resumed_job_id = resumed["job_id"]
    assert isinstance(resumed_job_id, str)
    await _job(case, resumed_job_id)
    assert case.sql(session_id, thread_id, "SELECT model_call_id,outcome FROM model_calls ORDER BY created_at") == [
        *calls, (retry, "completed"),
    ]
    original = await case.client.get(
        f"/api/v1/sessions/{session_id}/turns/{accepted['turn_id']}/message-stream/events",
        params={"thread_id": thread_id},
    )
    assert original.status_code == 200 and original.json()["data"] == stream_response.json()["data"]
    assert len(case.requests()) == 2
    case.write_actual("provider-rejection", {"job": job, "failure": failure, "model_calls": calls, "llm_logs": logs_response.json()})
