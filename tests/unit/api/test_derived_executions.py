"""派生 HTTP 边界使用有限输入、精确归属和有界状态回执。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_derived_execution_service, verify_local_token
from app.api.derived_executions import router
from app.core.identifier import create_prefixed_id
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.derived_execution import (
    DerivedExecutionDTO,
    DerivedItemReferenceDTO,
)


@pytest.fixture
def derived_api():
    session_id, thread_id = create_prefixed_id("ses"), create_prefixed_id("thr")
    calls: list[tuple] = []
    snapshot = DerivedExecutionDTO(task_id="derived-task", session_id=session_id, thread_id=thread_id,
        status="pending", created_at=datetime.now(UTC), source_view_id="view-frozen",
        source_last_ref=DerivedItemReferenceDTO(session_id=session_id, thread_id=thread_id,
            item_id="item-frozen", content_hash="frozen-hash"))

    async def submit(payload):
        calls.append(("submit", payload))
        return snapshot

    async def get(session, thread, task, *, wait_seconds):
        calls.append(("get", session, thread, task, wait_seconds))
        return snapshot

    async def cancel(session, thread, task):
        calls.append(("cancel", session, thread, task))
        return snapshot

    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_derived_execution_service] = lambda: SimpleNamespace(submit=submit, get=get, cancel=cancel)
    app.dependency_overrides[verify_local_token] = lambda: "local"
    return TestClient(app, headers={"X-Request-ID": "request-derived-test"}), calls, session_id, thread_id


def test_submit_preserves_scope_and_returns_frozen_source(derived_api):
    client, calls, session_id, thread_id = derived_api
    response = client.post("/api/v1/derived-executions", json={
        "session_id": session_id, "thread_id": thread_id, "idempotency_key": "once",
        "instruction": "分析已提交资料", "timeout_seconds": 20, "max_model_calls": 1,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == "request-derived-test"
    assert response.headers["X-Request-ID"] == body["request_id"]
    assert body["data"]["source_view_id"] == "view-frozen"
    assert body["data"]["source_last_ref"]["item_id"] == "item-frozen"
    assert calls[0][1].allowed_tool_ids == []
    assert not {"descriptor", "checkpoint", "instruction"} & body["data"].keys()


@pytest.mark.parametrize("body", [b"{" + b"x" * (512 * 1024), b'"' + b"x" * (512 * 1024) + b'"'])
def test_request_limit_precedes_json_parsing(derived_api, body):
    client, calls, *_ = derived_api
    response = client.post("/api/v1/derived-executions", content=body, headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert calls == []


@pytest.mark.parametrize("wait", ["-1", "31", "inf", "nan"])
def test_wait_must_be_finite_and_bounded(derived_api, wait):
    client, calls, session_id, thread_id = derived_api
    response = client.get("/api/v1/derived-executions/derived-task", params={
        "session_id": session_id, "thread_id": thread_id, "wait_seconds": wait,
    })
    assert response.status_code == 422
    assert calls == []


def test_observation_and_cancel_require_explicit_thread(derived_api):
    client, calls, session_id, thread_id = derived_api
    assert client.get("/api/v1/derived-executions/derived-task", params={"session_id": session_id}).status_code == 422
    assert client.post("/api/v1/derived-executions/derived-task/cancel", json={"session_id": session_id}).status_code == 422
    assert calls == []
    assert client.get("/api/v1/derived-executions/derived-task", params={
        "session_id": session_id, "thread_id": thread_id, "wait_seconds": 0.25,
    }).status_code == 200
    assert calls[-1] == ("get", session_id, thread_id, "derived-task", 0.25)
    assert client.post("/api/v1/derived-executions/derived-task/cancel", json={
        "session_id": session_id, "thread_id": thread_id,
    }).status_code == 200
    assert calls[-1] == ("cancel", session_id, thread_id, "derived-task")
