from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.checkpoint.base import Checkpoint, CheckpointTuple

from app.agents.context_checkpoint_store import ContextCompactionCheckpoint
from app.agents.context_compaction_adapter import ContextCompactionCheck
from app.api.deps import (
    get_context_compaction_service,
    get_goal_runtime_service,
    get_goal_service,
    get_request_id,
    get_session_service,
    verify_local_token,
)
from app.api.sessions import router as sessions_router
from app.core.job_event_bus import JobEventBus
from app.schemas.internal_v2.goal import SessionGoalDTO
from app.services.business.context_compaction_service import ContextCompactionService
from app.services.business.job.service import JobService, JobState
from app.services.business.session_goal_service import SessionGoalService
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.session_goal_store import SessionGoalStore
from app.services.infrastructure.trace_event_store import TraceEventStore
from tests.harness.python.run_context import TestRunContext
from tests.unit.core.catalog_workspace_helper import (
    CatalogWorkspaceContext,
    build_catalog_workspace,
)


class _InstantJobExecutor:
    async def run(self, job: JobState) -> str:
        return "done"


class _RecordingCheckpointStore:
    def __init__(self) -> None:
        messages = [HumanMessage(content="旧消息"), HumanMessage(content="新消息")]
        self.checkpoint = ContextCompactionCheckpoint(
            raw_messages=list(messages),
            event=None,
            _tuple=cast(CheckpointTuple, object()),
            _checkpoint=cast(Checkpoint, {"channel_versions": {}}),
            _channel_values={"messages": messages},
        )
        self.load_calls = 0
        self.save_calls = 0

    async def load(self, session_id: str) -> ContextCompactionCheckpoint:
        del session_id
        self.load_calls += 1
        return self.checkpoint

    async def save_compaction_request(
        self, *, checkpoint: ContextCompactionCheckpoint
    ) -> None:
        assert checkpoint is self.checkpoint
        self.save_calls += 1


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
        del agent_id, event
        self.check_calls += 1
        messages = [cast(AnyMessage, message) for message in raw_messages]
        return ContextCompactionCheck(
            messages=messages,
            effective_messages=messages,
            cutoff=1,
        )


class _RuntimeProbe:
    def __init__(self, session_service: SessionService) -> None:
        self._session_service = session_service
        self.probe_settle_admission = True
        self.calls: list[str] = []

    async def _assert_admission_released(self, session_id: str) -> None:
        async def acquire() -> None:
            async with self._session_service.session_write_admission(session_id):
                pass

        await asyncio.wait_for(acquire(), timeout=2)

    async def settle_active_progress(self, session_id: str) -> None:
        if self.probe_settle_admission:
            await self._assert_admission_released(session_id)
        self.calls.append("settle")

    async def apply_objective_update(self, goal: SessionGoalDTO) -> None:
        await self._assert_admission_released(goal.session_id)
        self.calls.append("apply")

    async def ensure_active_goal_running(self, session_id: str) -> None:
        await self._assert_admission_released(session_id)
        self.calls.append("ensure")


@dataclass(slots=True)
class _Harness:
    workspace: CatalogWorkspaceContext
    client: httpx.AsyncClient
    session_service: SessionService
    job_service: JobService
    goal_service: SessionGoalService
    runtime: _RuntimeProbe
    compaction_service: ContextCompactionService
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
        job_executor=_InstantJobExecutor(),
        session_lifecycle_guard=session_service.assert_session_active,
    )
    goal_service = SessionGoalService(
        store=SessionGoalStore(workspace.resolver),
        session_service=session_service,
        job_event_bus=JobEventBus(),
    )
    runtime = _RuntimeProbe(session_service)
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
    api.dependency_overrides[get_goal_service] = lambda: goal_service
    api.dependency_overrides[get_goal_runtime_service] = lambda: runtime
    api.dependency_overrides[get_context_compaction_service] = lambda: (
        compaction_service
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://session-short-write.test",
        timeout=10,
    ) as client:
        try:
            yield _Harness(
                workspace=workspace,
                client=client,
                session_service=session_service,
                job_service=job_service,
                goal_service=goal_service,
                runtime=runtime,
                compaction_service=compaction_service,
                checkpoint_store=checkpoint_store,
                compactor=compactor,
            )
        finally:
            workspace.close()


@pytest.mark.asyncio
async def test_active_short_write_endpoints_keep_their_contracts(
    harness: _Harness,
) -> None:
    session_id = await harness.workspace.create_session(
        "原始标题",
        current_provider_id="test-provider",
    )

    update = await harness.client.patch(
        f"/api/v1/sessions/{session_id}",
        json={"title": "更新标题"},
    )
    assert update.status_code == 200, update.text
    assert update.json()["data"]["title"] == "更新标题"
    assert harness.workspace.node(session_id).name == "更新标题"
    assert (await harness.session_service.get(session_id)).title == "更新标题"

    compact = await harness.client.post(f"/api/v1/sessions/{session_id}/compact")
    assert compact.status_code == 200, compact.text
    assert compact.json()["data"]["status"] == "scheduled"
    assert harness.checkpoint_store.load_calls == 1
    assert harness.checkpoint_store.save_calls == 1
    assert harness.compactor.check_calls == 1

    created = await harness.client.put(
        f"/api/v1/sessions/{session_id}/goal",
        json={"objective": "完成第一目标"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["data"]["objective"] == "完成第一目标"

    updated = await harness.client.put(
        f"/api/v1/sessions/{session_id}/goal",
        json={"objective": "完成更新后的目标"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["objective"] == "完成更新后的目标"

    cleared = await harness.client.delete(f"/api/v1/sessions/{session_id}/goal")
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"] == {"session_id": session_id, "cleared": True}
    assert await harness.goal_service.get(session_id) is None
    assert harness.runtime.calls == ["ensure", "apply", "ensure", "settle"]


@pytest.mark.asyncio
async def test_session_update_keeps_not_found_for_missing_and_folder_nodes(
    harness: _Harness,
) -> None:
    folder_id = harness.workspace.create_folder("仅供目录使用")
    missing_session_id = "ses_00000000000070008000000000000000"

    missing = await harness.client.patch(
        f"/api/v1/sessions/{missing_session_id}",
        json={"title": "不存在"},
    )
    folder = await harness.client.patch(
        f"/api/v1/sessions/{folder_id}",
        json={"title": "不得改名"},
    )

    assert missing.status_code == 404, missing.text
    assert folder.status_code == 404, folder.text
    assert harness.workspace.node(folder_id).name == "仅供目录使用"


@pytest.mark.asyncio
async def test_admitted_session_update_finishes_before_delete_drain(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await harness.workspace.create_session(
        "删除前标题",
        current_provider_id="test-provider",
    )
    initial_updated_at = harness.workspace.manifest(session_id)["updated_at"]
    write_admitted = asyncio.Event()
    continue_write = asyncio.Event()
    drain_started = asyncio.Event()
    drain_snapshot: tuple[str, str] | None = None
    original_get = harness.session_service.get
    topology_gate = harness.session_service._navigation_topology_gate
    original_shared = topology_gate.shared
    shared_acquisitions = 0
    shared_acquired = asyncio.Event()

    @asynccontextmanager
    async def count_topology_shared():
        nonlocal shared_acquisitions
        async with original_shared():
            shared_acquisitions += 1
            shared_acquired.set()
            yield

    async def pause_after_admission(current_session_id: str):
        result = await original_get(current_session_id)
        if current_session_id == session_id and not write_admitted.is_set():
            write_admitted.set()
            await continue_write.wait()
        return result

    monkeypatch.setattr(harness.session_service, "get", pause_after_admission)
    monkeypatch.setattr(topology_gate, "shared", count_topology_shared)

    async def fail_drain(current_session_id: str) -> None:
        nonlocal drain_snapshot
        assert current_session_id == session_id
        node = harness.workspace.node(session_id)
        assert node.state == "deleting"
        manifest = harness.workspace.manifest(session_id)
        drain_snapshot = (manifest["updated_at"], node.name)
        drain_started.set()
        raise RuntimeError("injected drain failure")

    harness.workspace.delete_service.set_session_drain_callback(fail_drain)
    update_task = asyncio.create_task(
        harness.client.patch(
            f"/api/v1/sessions/{session_id}",
            json={"title": "已准入的新标题"},
        )
    )
    delete_task: asyncio.Task[object] | None = None
    late_update_task: asyncio.Task[httpx.Response] | None = None
    try:
        await asyncio.wait_for(write_admitted.wait(), timeout=3)
        delete_task = asyncio.create_task(
            harness.job_service.run_session_delete_operation(
                session_id,
                lambda: harness.session_service.delete(session_id),
            )
        )
        for _ in range(300):
            if harness.workspace.node(session_id).state == "deleting":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("删除流程未将 catalog 节点推进到 deleting")

        assert not drain_started.is_set()
        assert not update_task.done()
        late_update_task = asyncio.create_task(
            harness.client.patch(
                f"/api/v1/sessions/{session_id}",
                json={"title": "删除开始后的迟到标题"},
            )
        )
        for _ in range(300):
            if shared_acquisitions >= 2:
                break
            shared_acquired.clear()
            await asyncio.wait_for(shared_acquired.wait(), timeout=3)
        else:
            pytest.fail("迟到更新未进入 topology shared 准入")
        assert not late_update_task.done()
        continue_write.set()

        update = await asyncio.wait_for(update_task, timeout=3)
        assert update.status_code == 200, update.text
        assert update.json()["data"]["title"] == "已准入的新标题"
        late_update = await asyncio.wait_for(late_update_task, timeout=3)
        assert late_update.status_code == 409, late_update.text
        assert "session_deletion_pending" in late_update.json()["detail"]
        with pytest.raises(RuntimeError, match="injected drain failure"):
            await asyncio.wait_for(delete_task, timeout=3)

        assert drain_started.is_set()
        assert drain_snapshot is not None
        assert drain_snapshot[0] != initial_updated_at
        assert drain_snapshot[1] == "已准入的新标题"
        assert harness.workspace.node(session_id).state == "deleting"
        assert harness.workspace.session_dir(session_id).is_dir()
    finally:
        continue_write.set()
        pending = [
            task
            for task in (update_task, late_update_task, delete_task)
            if task is not None
        ]
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_deleting_session_rejects_all_new_short_write_endpoints(
    harness: _Harness,
) -> None:
    session_id = await harness.workspace.create_session(
        "保持原状",
        current_provider_id="test-provider",
    )

    async def fail_drain(current_session_id: str) -> None:
        assert current_session_id == session_id
        assert harness.workspace.node(session_id).state == "deleting"
        raise RuntimeError("injected drain failure")

    harness.workspace.delete_service.set_session_drain_callback(fail_drain)
    with pytest.raises(RuntimeError, match="injected drain failure"):
        await harness.job_service.run_session_delete_operation(
            session_id,
            lambda: harness.session_service.delete(session_id),
        )
    harness.runtime.probe_settle_admission = False

    update = await harness.client.patch(
        f"/api/v1/sessions/{session_id}",
        json={"title": "不得写入"},
    )
    compact = await harness.client.post(f"/api/v1/sessions/{session_id}/compact")
    goal_set = await harness.client.put(
        f"/api/v1/sessions/{session_id}/goal",
        json={"objective": "不得创建"},
    )
    goal_clear = await harness.client.delete(f"/api/v1/sessions/{session_id}/goal")

    responses = [update, compact, goal_set, goal_clear]
    assert [response.status_code for response in responses] == [409, 409, 409, 409]
    assert all(
        "session_deletion_pending" in response.json()["detail"]
        for response in responses
    )
    assert harness.workspace.node(session_id).name == "保持原状"
    assert (await harness.session_service.get(session_id)).title == "保持原状"
    assert await harness.goal_service.get(session_id) is None
    assert harness.checkpoint_store.load_calls == 0
    assert harness.checkpoint_store.save_calls == 0
    assert harness.compactor.check_calls == 0
    assert harness.workspace.node(session_id).state == "deleting"
    assert harness.workspace.session_dir(session_id).is_dir()
