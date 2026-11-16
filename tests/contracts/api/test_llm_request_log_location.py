"""真实日志 HTTP 响应只公开记录内容，不把 owner 的磁盘位置交给客户端。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import (
    get_llm_request_log_reader,
    get_session_service,
    verify_local_token,
)
from app.api.sessions import router
from app.core.trace_middleware import TraceMiddleware
from app.services.infrastructure.llm_request_log_reader import LLMRequestLogReader


@pytest.fixture
def log_endpoint(tmp_path: Path, session_bundle_factory) -> Iterator[tuple[TestClient, str, Path]]:
    session_id = "ses_019c257bff2c7c6989d1c38a6ae7fbf2"
    sessions_root = tmp_path / "sessions"
    session_root = session_bundle_factory(sessions_root, session_id)
    log_root = session_root / "logs" / "llm_requests"
    log_root.mkdir(parents=True)
    (log_root / "1700000000000000000.json").write_text(json.dumps({
        "session_id": session_id, "job_id": "log-contract-job", "timestamp": 1700000000123,
        "request": {"messages": [{"role": "user", "content": "审计请求"}]},
        "response": {"result": [{"role": "assistant", "content": "审计结果"}]},
        "upstream": {"attempts": []},
    }), encoding="utf-8")
    service = LLMRequestLogReader(sessions_dir=sessions_root)
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(router, prefix="/api/v1")
    class SessionAdmission:
        @asynccontextmanager
        async def session_read_admission(self, session_id: str):
            yield

    application.dependency_overrides[get_session_service] = SessionAdmission
    application.dependency_overrides[get_llm_request_log_reader] = lambda: service
    application.dependency_overrides[verify_local_token] = lambda: "contract-token"
    with TestClient(application) as client:
        yield client, session_id, session_root


def test_log_http_response_preserves_content_without_physical_location(log_endpoint) -> None:
    client, session_id, session_root = log_endpoint
    response = client.get(
        f"/api/v1/sessions/{session_id}/llm-request-logs", headers={"X-Request-ID": "log-location-contract"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["request_id"] == response.headers["X-Request-ID"] == "log-location-contract"
    summary, = payload["data"]
    assert summary == {"session_id": session_id, "file_name": "1700000000000000000.json",
                       "recorded_at": 1700000000000,
                       "size_bytes": (session_root / "logs/llm_requests/1700000000000000000.json").stat().st_size}
    response = client.get(f"/api/v1/sessions/{session_id}/llm-request-logs/{summary['file_name']}")
    assert response.status_code == 200, response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    record = response.json()["data"]
    assert record == {
        "session_id": session_id, "job_id": "log-contract-job", "timestamp": 1700000000123,
        "file_name": "1700000000000000000.json",
        "request": {"messages": [{"role": "user", "content": "审计请求"}]},
        "response": {"result": [{"role": "assistant", "content": "审计结果"}]},
        "upstream": {"attempts": []},
    }
    assert str(session_root) not in response.text
    missing = client.get(f"/api/v1/sessions/{session_id}/llm-request-logs/1700000000000000001.json")
    assert missing.status_code == 404, missing.text
    rejected = client.get(f"/api/v1/sessions/{session_id}/llm-request-logs/not-a-log.json")
    assert rejected.status_code == 422, rejected.text
