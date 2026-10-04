from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.agents.context_checkpoint_store import ContextCompactionCheckpoint
from app.agents.context_compaction_adapter import ContextCompactionCheck
from app.api.deps import (
    get_context_compaction_service,
    get_job_service,
    get_request_id,
    get_session_service,
    verify_local_token,
)
from app.api.sessions import router as sessions_router
from app.core.job_event_bus import JobEventBus
from app.schemas.internal_v2.session import SessionDTO
from app.services.business.context_compaction_service import ContextCompactionService
from app.services.business.job.service import JobService, JobState
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.trace_event_store import TraceEventStore
from tests.harness.python.run_context import TestRunContext
from tests.unit.core.catalog_workspace_helper import (
    CatalogWorkspaceContext,
    build_catalog_workspace,
)


class _ImmediateJobExecutor:
    async def run(self, job: JobState) -> str:
        del job
        return "done"


class _RecordingCheckpointStore:
    def __init__(self) -> None:
        self.load_calls = 0
        self.save_calls = 0

    async def load(self, session_id: str) -> ContextCompactionCheckpoint | None:
        del session_id
        self.load_calls += 1
        return None

    async def save_compaction_request(
        self, *, checkpoint: ContextCompactionCheckpoint
    ) -> None:
        del checkpoint
        self.save_calls += 1
        raise AssertionError("空 checkpoint 不应创建压缩请求")


class _CompactorProbe:
    def __init__(self) -> None:
        self.check_calls = 0

    async def check(
        self,
        *,
        agent_id: str,
        raw_messages: list[object],
        event: object,
    ) -> ContextCompactionCheck:
        del agent_id, raw_messages, event
        self.check_calls += 1
        raise AssertionError("空 checkpoint 不应触发摘要判断")


@dataclass(slots=True)
class _Harness:
    workspace: CatalogWorkspaceContext
    client: httpx.AsyncClient
    session_service: SessionService
    job_service: JobService
    checkpoint_store: _RecordingCheckpointStore
    compactor: _CompactorProbe


@pytest.fixture
async def harness() -> AsyncIterator[_Harness]:
    run_context = TestRunContext.from_test_file(Path(__file__))
    if run_context.workspace_root.exists():
        shutil.rmtree(run_context.workspace_root)
    workspace = build_catalog_workspace(run_context.output_root)
    session_service = SessionService(
        config_service=ConfigService(),
        trace_event_store=TraceEventStore(sessions_dir=workspace.sessions_root),
        workspace_id=workspace.workspace_id,
        path_resolver=workspace.resolver,
        creation_service=workspace.creation_service,
    )
    job_service = JobService(
        job_event_bus=JobEventBus(),
        job_executor=_ImmediateJobExecutor(),
        session_lifecycle_guard=session_service.assert_session_active,
    )
    checkpoint_store = _RecordingCheckpointStore()
    compactor = _CompactorProbe()
    compaction_service = ContextCompactionService(
        checkpoint_store=checkpoint_store,
        job_service=job_service,
        session_service=session_service,
        summarization_compactor=compactor,
    )

    api = FastAPI()
    api.include_router(sessions_router, prefix="/api/v1")
    api.dependency_overrides[verify_local_token] = lambda: "test-local-token"
    api.dependency_overrides[get_request_id] = lambda: "test-request-id"
    api.dependency_overrides[get_session_service] = lambda: session_service
    api.dependency_overrides[get_job_service] = lambda: job_service
    api.dependency_overrides[get_context_compaction_service] = lambda: (
        compaction_service
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api, raise_app_exceptions=False),
        base_url="http://session-idle-delete-race.test",
        timeout=10,
    ) as client:
        try:
            yield _Harness(
                workspace=workspace,
                client=client,
                session_service=session_service,
                job_service=job_service,
                checkpoint_store=checkpoint_store,
                compactor=compactor,
            )
        finally:
            workspace.close()


@pytest.mark.asyncio
async def test_active_compaction_and_missing_session_keep_their_api_contracts(
    harness: _Harness,
) -> None:
    session_id = await harness.workspace.create_session(
        "可压缩会话",
        current_provider_id="test-provider",
    )

    compact = await harness.client.post(f"/api/v1/sessions/{session_id}/compact")
    missing = await harness.client.post(
        "/api/v1/sessions/ses_00000000000070008000000000000000/compact"
    )

    assert compact.status_code == 200, compact.text
    assert compact.json()["data"]["status"] == "skipped"
    assert missing.status_code == 404, missing.text
    assert harness.checkpoint_store.load_calls == 1
    assert harness.checkpoint_store.save_calls == 0
    assert harness.compactor.check_calls == 0


@pytest.mark.asyncio
async def test_compaction_during_real_delete_returns_conflict_before_checkpoint_reads(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await harness.workspace.create_session(
        "删除排空中的会话",
        current_provider_id="test-provider",
    )
    drain_started = asyncio.Event()
    continue_drain = asyncio.Event()
    session_get_calls = 0
    original_get = harness.session_service.get

    async def record_session_get(current_session_id: str) -> SessionDTO:
        nonlocal session_get_calls
        session_get_calls += 1
        return await original_get(current_session_id)

    async def hold_delete_drain(current_session_id: str) -> None:
        assert current_session_id == session_id
        assert harness.workspace.node(session_id).state == "deleting"
        drain_started.set()
        await continue_drain.wait()

    monkeypatch.setattr(harness.session_service, "get", record_session_get)
    harness.workspace.delete_service.set_session_drain_callback(hold_delete_drain)
    delete_task = asyncio.create_task(
        harness.client.delete(f"/api/v1/sessions/{session_id}")
    )
    try:
        await asyncio.wait_for(drain_started.wait(), timeout=3)
        assert session_id in harness.job_service._deleting_sessions

        compact = await harness.client.post(
            f"/api/v1/sessions/{session_id}/compact"
        )

        assert compact.status_code == 409, compact.text
        assert "session_deletion_pending" in compact.json()["detail"]
        assert session_get_calls == 0
        assert harness.checkpoint_store.load_calls == 0
        assert harness.checkpoint_store.save_calls == 0
        assert harness.compactor.check_calls == 0
    finally:
        continue_drain.set()
        await asyncio.gather(delete_task, return_exceptions=True)

    delete = delete_task.result()
    assert delete.status_code == 200, delete.text
    with pytest.raises(KeyError):
        harness.workspace.node(session_id)
