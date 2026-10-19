"""运行时状态的真实 HTTP 序列化不公开 owner 的磁盘位置。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_runtime_service, verify_local_token
from app.api.runtime import router
from app.core.path_utils import get_session_path_resolver
from app.core.trace_middleware import TraceMiddleware
from app.services.business.job.service import JobService, JobState
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.infrastructure.runtime_service import RuntimeService
from app.services.infrastructure.trace_event_store import TraceEventStore
from tests.support.background_work import build_background_registry


class UnusedJobExecutor:
    async def run(self, _state: JobState) -> str:
        raise AssertionError("读取运行时状态不得启动 Job")


class UnusedJobEventBus:
    async def publish(self, **_event: object) -> None:
        raise AssertionError("读取运行时状态不得产生 Job 事件")


@pytest.fixture
def runtime_endpoint(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    sessions_root = tmp_path / ".boxteam" / "sessions"
    def reject_job_admission(_session_id: str) -> None:
        raise AssertionError("读取运行时状态不得准入 Session Job")

    jobs = JobService(job_event_bus=UnusedJobEventBus(), job_executor=UnusedJobExecutor(),
        session_lifecycle_guard=reject_job_admission)

    async def pending_checkpoint(_job: JobState) -> None:
        return None

    async def before_provider(_job: JobState) -> bool:
        return False

    service = RuntimeService(
        workspace_id="00000000-0000-4000-8000-000000000001", workspace_root=tmp_path,
        job_service=jobs, background_task_registry=build_background_registry(sessions_root),
        trace_event_store=TraceEventStore(sessions_dir=sessions_root),
        message_stream_store=MessageStreamStore(path_resolver=get_session_path_resolver(sessions_root)),
        pending_tool_checkpoint_id=pending_checkpoint, can_restart_before_provider=before_provider,
    )
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[get_runtime_service] = lambda: service
    application.dependency_overrides[verify_local_token] = lambda: "contract-token"
    with TestClient(application) as client:
        yield client, tmp_path


def test_runtime_http_status_preserves_lifecycle_without_storage_location(runtime_endpoint) -> None:
    client, workspace_root = runtime_endpoint
    for action, expected in ((None, "ready"), ("drain", "draining"), ("drain/cancel", "ready")):
        if action:
            assert client.post(f"/api/v1/runtime/{action}").status_code == 200
        response = client.get("/api/v1/runtime/status", headers={"X-Request-ID": "runtime-location-contract"})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["request_id"] == response.headers["X-Request-ID"] == "runtime-location-contract"
        status = payload["data"]
        assert set(status) == {"pid", "uptime_seconds", "workspace_id", "active_jobs", "lifecycle_state", "accepting_jobs", "blockers", "loaded_agents"}
        assert status["pid"] > 0 and status["uptime_seconds"] >= 0
        assert status["workspace_id"] == "00000000-0000-4000-8000-000000000001"
        assert status["lifecycle_state"] == expected
        assert status["accepting_jobs"] is (expected == "ready")
        assert status["active_jobs"] == 0 and status["blockers"] == []
        assert status["loaded_agents"] == ["planner", "executor", "reviewer", "summarizer"]
        assert str(workspace_root) not in response.text
