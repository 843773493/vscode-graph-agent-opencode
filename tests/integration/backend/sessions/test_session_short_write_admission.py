from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
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
    get_goal_service,
    get_persistent_task_runtime,
    get_request_id,
    get_session_service,
    verify_local_token,
)
from app.api.sessions import router as sessions_router
from app.core.job_event_bus import JobEventBus
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.schemas.internal_v2.goal import SessionGoalDTO
from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationCreateRequest,
    NodeDebugConfigurationUpdateRequest,
)
from app.services.business.context_compaction_service import ContextCompactionService
from app.services.business.job.service import JobService, JobState
from app.services.business.session_goal_service import SessionGoalService
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug.process.launch_orchestrator import (
    NodeDebugLaunchRequest,
)
from app.services.infrastructure.node_debug.service import NodeDebugService
from app.services.infrastructure.node_debug.session.session_admission import (
    NodeDebugSessionAdmission,
)
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from app.services.infrastructure.persistent_tasks.store import PersistentTaskStore
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from tests.harness.python.run_context import TestRunContext
from tests.support.node_debug_dependencies import node_debug_file_paths
from tests.support.workspaces import prepare_test_workspace
from tests.unit.core.catalog_workspace_helper import (
    CatalogWorkspaceContext,
    build_catalog_workspace,
)


class _InstantJobExecutor:
    async def run(self, job: JobState) -> str:
        return "done"


class _RecordingCheckpointStore:
    def __init__(self, workspace: CatalogWorkspaceContext) -> None:
        self.workspace = workspace
        self.checkpoint: ContextCompactionCheckpoint | None = None
        self.load_calls = 0
        self.save_calls = 0

    async def load(self, owner: MutationIntentOwner) -> ContextCompactionCheckpoint:
        assert self.workspace.resolver.main_thread_id(owner.session_id) == owner.thread_id
        self.load_calls += 1
        messages = [HumanMessage(content="旧消息"), HumanMessage(content="新消息")]
        self.checkpoint = ContextCompactionCheckpoint(
            owner=owner,
            raw_messages=list(messages),
            event=None,
            _tuple=cast(CheckpointTuple, object()),
            _checkpoint=cast(Checkpoint, {"channel_versions": {}}),
            _channel_values={"messages": messages},
        )
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


class _StartBarrier:
    def __init__(
        self,
        entered: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self.entered = entered
        self.release = release
        self.calls = 0

    async def launch(self, request: NodeDebugLaunchRequest) -> SimpleNamespace:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return SimpleNamespace(owner=request.owner)


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

    async def request_goal(self, session_id: str, *, objective: SessionGoalDTO | None = None) -> None:
        await self._assert_admission_released(session_id)
        self.calls.append("apply" if objective is not None else "ensure")


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
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Harness]:
    run_context = TestRunContext.from_test_file(Path(__file__))
    prepare_test_workspace(
        workspace_root=run_context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(),
    )
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
    task_state = WorkspaceStateStore(workspace_root=workspace.workspace_root)
    goal_service = SessionGoalService(
        store=PersistentTaskStore(task_state, workspace_id=workspace.workspace_id, paths=workspace.resolver),
        session_service=session_service,
        job_event_bus=JobEventBus(),
    )
    runtime = _RuntimeProbe(session_service)

    async def settle_probe(_runtime, session_id):
        assert _runtime is runtime
        await runtime.settle_active_progress(session_id)

    monkeypatch.setattr("app.api.sessions.settle_goal_progress", settle_probe)
    checkpoint_store = _RecordingCheckpointStore(workspace)
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
    api.dependency_overrides[get_persistent_task_runtime] = lambda: runtime
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
            task_state.close()
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
        json={"agent_id": "coder"},
    )
    assert update.status_code == 200, update.text
    assert update.json()["data"]["current_agent_id"] == "coder"
    assert harness.workspace.node(session_id).name == "原始标题"
    assert (await harness.session_service.get(session_id)).current_agent_id == "coder"

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
    main = harness.workspace.resolver.main_thread_id(session_id)
    assert created.json()["data"]["thread_id"] == updated.json()["data"]["thread_id"] == main
    assert cleared.json()["data"] == {"session_id": session_id, "thread_id": main, "cleared": True}
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
        json={"agent_id": "coder"},
    )
    folder = await harness.client.patch(
        f"/api/v1/sessions/{folder_id}",
        json={"agent_id": "coder"},
    )

    assert missing.status_code == 404, missing.text
    assert folder.status_code == 404, folder.text
    assert harness.workspace.node(folder_id).name == "仅供目录使用"


@pytest.mark.asyncio
async def test_session_update_rejects_title_payload(
    harness: _Harness,
) -> None:
    session_id = await harness.workspace.create_session("保持原名")

    response = await harness.client.patch(
        f"/api/v1/sessions/{session_id}",
        json={"title": "不得经同步接口改名"},
    )

    assert response.status_code == 422, response.text
    assert harness.workspace.node(session_id).name == "保持原名"


@pytest.mark.asyncio
async def test_admitted_session_update_finishes_before_delete_drain(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await harness.workspace.create_session(
        "删除前标题",
        current_provider_id="test-provider",
    )
    initial_updated_at = harness.workspace.node(session_id).updated_at.isoformat()
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
        drain_snapshot = (node.updated_at.isoformat(), node.current_agent_id)
        drain_started.set()
        raise RuntimeError("injected drain failure")

    harness.workspace.delete_service.set_session_drain_callback(fail_drain)
    update_task = asyncio.create_task(
        harness.client.patch(
            f"/api/v1/sessions/{session_id}",
            json={"agent_id": "coder"},
        )
    )
    delete_task: asyncio.Task[object] | None = None
    late_update_task: asyncio.Task[httpx.Response] | None = None
    try:
        await asyncio.wait_for(write_admitted.wait(), timeout=3)
        delete_task = asyncio.create_task(
            harness.job_service.run_session_delete_operation(
                session_id,
                lambda: harness.workspace.resolver.delete_subtree(idempotency_key=f"short-write-delete-{session_id}", root_node_id=session_id),
                deadline=asyncio.get_running_loop().time() + 30,
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
                json={"agent_id": "coder"},
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
        assert update.json()["data"]["current_agent_id"] == "coder"
        late_update = await asyncio.wait_for(late_update_task, timeout=3)
        assert late_update.status_code == 409, late_update.text
        assert "session_deletion_pending" in late_update.json()["detail"]
        with pytest.raises(RuntimeError, match="injected drain failure"):
            await asyncio.wait_for(delete_task, timeout=3)

        assert drain_started.is_set()
        assert drain_snapshot is not None
        assert drain_snapshot[0] != initial_updated_at
        assert drain_snapshot[1] == "coder"
        assert harness.workspace.node(session_id).name == "删除前标题"
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
            lambda: harness.workspace.resolver.delete_subtree(idempotency_key=f"short-write-delete-{session_id}", root_node_id=session_id),
            deadline=asyncio.get_running_loop().time() + 30,
        )
    harness.runtime.probe_settle_admission = False

    update = await harness.client.patch(
        f"/api/v1/sessions/{session_id}",
        json={"agent_id": "coder"},
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


def _node_debug_service(harness: _Harness) -> NodeDebugService:
    return NodeDebugService(
        workspace_root=harness.workspace.workspace_root,
        file_paths=node_debug_file_paths(harness.workspace.workspace_root),
        config_service=ConfigService(workspace_root=harness.workspace.workspace_root),
        session_store=NodeDebugSessionStore(harness.workspace.resolver),
        session_admission=NodeDebugSessionAdmission(
            session_service=harness.session_service,
            path_resolver=harness.workspace.resolver,
        ),
        external_resource_leases=ExternalResourceLeaseLedger(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["get_state", "get_variables"])
@pytest.mark.parametrize("blocked_stage", ["claim", "source"])
async def test_node_debug_state_read_finishes_before_delete_drain(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    reader: str,
    blocked_stage: str,
) -> None:
    session_id = await harness.workspace.create_session(
        "状态读取等待删除 drain", current_provider_id="test-provider"
    )
    thread_id = harness.workspace.resolver.main_thread_id(session_id)
    session_path = harness.workspace.session_dir(session_id)
    node_debug = _node_debug_service(harness)
    harness.workspace.resolver.bind_session_drain_callback(node_debug.drain_session)
    read_entered = asyncio.Event()
    release_read = asyncio.Event()
    drain_attempted = asyncio.Event()

    if blocked_stage == "claim":
        original_reconcile_claim = node_debug._lifecycle.reconcile_persisted_claim
        pause_claim_once = True

        async def pause_claim(owner: tuple[str, str]):
            nonlocal pause_claim_once
            if pause_claim_once:
                pause_claim_once = False
                read_entered.set()
                await release_read.wait()
            return await original_reconcile_claim(owner)

        monkeypatch.setattr(
            node_debug._lifecycle, "reconcile_persisted_claim", pause_claim
        )
    else:
        original_reconcile_sources = node_debug._reconcile_session_sources
        pause_source_once = True

        async def pause_source(
            current_session_id: str,
            current_thread_id: str,
            runtime: object,
        ) -> None:
            nonlocal pause_source_once
            if pause_source_once:
                pause_source_once = False
                read_entered.set()
                await release_read.wait()
            await original_reconcile_sources(
                current_session_id, current_thread_id, runtime
            )

        monkeypatch.setattr(node_debug, "_reconcile_session_sources", pause_source)

    delete_gate = harness.workspace.delete_service._session_gate
    original_exclusive = delete_gate.exclusive

    @asynccontextmanager
    async def observe_drain_gate(current_session_id: str) -> AsyncIterator[None]:
        if current_session_id == session_id:
            drain_attempted.set()
        async with original_exclusive(current_session_id):
            yield

    monkeypatch.setattr(delete_gate, "exclusive", observe_drain_gate)
    read_task = asyncio.create_task(
        node_debug.get_state(session_id, thread_id)
        if reader == "get_state"
        else node_debug.get_variables(session_id=session_id, thread_id=thread_id)
    )
    delete_task = asyncio.create_task(
        harness.workspace.delete_service.delete(
            idempotency_key=f"node-debug-{reader}-{blocked_stage}-read-delete",
            root_node_id=session_id,
        )
    )
    try:
        await asyncio.wait_for(read_entered.wait(), timeout=3)
        await asyncio.wait_for(drain_attempted.wait(), timeout=3)
        assert harness.workspace.node(session_id).state == "deleting"
        assert session_path.is_dir()
        assert not read_task.done()
        assert not delete_task.done()

        release_read.set()
        if reader == "get_state":
            state = await asyncio.wait_for(read_task, timeout=3)
            assert state.status == "idle"
        else:
            with pytest.raises(RuntimeError, match="只有暂停在源码断点"):
                await asyncio.wait_for(read_task, timeout=3)
        await asyncio.wait_for(delete_task, timeout=3)
        assert not session_path.exists()
    finally:
        release_read.set()
        for task in (read_task, delete_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(read_task, delete_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cross_session_configuration_copy_releases_source_gate_after_snapshot(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_session_id = await harness.workspace.create_session(
        "复制来源", current_provider_id="test-provider"
    )
    target_session_id = await harness.workspace.create_session(
        "复制目标", current_provider_id="test-provider"
    )
    source_thread_id = harness.workspace.resolver.main_thread_id(source_session_id)
    target_thread_id = harness.workspace.resolver.main_thread_id(target_session_id)
    (harness.workspace.workspace_root / "main.mjs").write_text(
        "console.log('copy');\n", encoding="utf-8"
    )
    node_debug = _node_debug_service(harness)
    source_state = await node_debug.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=source_session_id,
            thread_id=source_thread_id,
            name="来源原名",
            script_path="main.mjs",
        )
    )
    configuration_id = source_state.active_configuration_id
    assert configuration_id is not None

    original_admission = harness.session_service.session_write_admission
    source_snapshot_ready = asyncio.Event()
    resume_copy = asyncio.Event()
    active_sessions: set[str] = set()
    admitted_sessions: list[str] = []
    max_active_sessions = 0
    paused_source_once = False

    @asynccontextmanager
    async def track_session_gate(current_session_id: str) -> AsyncIterator[None]:
        nonlocal max_active_sessions, paused_source_once
        async with original_admission(current_session_id):
            active_sessions.add(current_session_id)
            admitted_sessions.append(current_session_id)
            max_active_sessions = max(max_active_sessions, len(active_sessions))
            try:
                yield
            finally:
                active_sessions.remove(current_session_id)
        if current_session_id == source_session_id and not paused_source_once:
            paused_source_once = True
            source_snapshot_ready.set()
            await resume_copy.wait()

    monkeypatch.setattr(
        harness.session_service,
        "session_write_admission",
        track_session_gate,
    )
    copy_task = asyncio.create_task(
        node_debug.copy_configuration(
            source_session_id=source_session_id,
            target_session_id=target_session_id,
            configuration_id=configuration_id,
            source_thread_id=source_thread_id,
            target_thread_id=target_thread_id,
        )
    )
    try:
        await asyncio.wait_for(source_snapshot_ready.wait(), timeout=3)
        updated = await node_debug.update_configuration(
            configuration_id,
            NodeDebugConfigurationUpdateRequest(
                session_id=source_session_id,
                thread_id=source_thread_id,
                name="来源新名",
                script_path="main.mjs",
            ),
        )
        assert updated.configurations[0].name == "来源新名"

        resume_copy.set()
        copied = await asyncio.wait_for(copy_task, timeout=3)
        assert copied.name == "来源原名"
        assert max_active_sessions == 1
        assert admitted_sessions == [
            source_session_id,
            source_session_id,
            target_session_id,
        ]
        assert active_sessions == set()

        same_session_copy = await node_debug.copy_configuration(
            source_session_id=source_session_id,
            target_session_id=source_session_id,
            configuration_id=configuration_id,
            source_thread_id=source_thread_id,
            target_thread_id=source_thread_id,
            name="同会话副本",
        )
        assert same_session_copy.name == "同会话副本"
        assert admitted_sessions == [
            source_session_id,
            source_session_id,
            target_session_id,
            source_session_id,
        ]
        assert max_active_sessions == 1
    finally:
        resume_copy.set()
        if not copy_task.done():
            copy_task.cancel()
        await asyncio.gather(copy_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_admitted_node_debug_start_finishes_before_delete_drain(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await harness.workspace.create_session(
        "等待调试启动完成", current_provider_id="test-provider"
    )
    thread_id = harness.workspace.resolver.main_thread_id(session_id)
    node_debug = _node_debug_service(harness)
    launch_entered = asyncio.Event()
    release_start = asyncio.Event()
    drain_attempted = asyncio.Event()
    launch = _StartBarrier(launch_entered, release_start)
    monkeypatch.setattr(node_debug, "_create_launch_orchestrator", lambda: launch)
    harness.workspace.resolver.bind_session_drain_callback(node_debug.drain_session)

    delete_gate = harness.workspace.delete_service._session_gate
    original_exclusive = delete_gate.exclusive

    @asynccontextmanager
    async def observe_drain_gate(current_session_id: str) -> AsyncIterator[None]:
        if current_session_id == session_id:
            drain_attempted.set()
        async with original_exclusive(current_session_id):
            yield

    monkeypatch.setattr(delete_gate, "exclusive", observe_drain_gate)
    start_task = asyncio.create_task(
        node_debug.start(
            session_id=session_id,
            thread_id=thread_id,
            path="main.mjs",
            args=[],
            breakpoints=[],
        )
    )
    delete_task: asyncio.Task[object] | None = None
    try:
        await asyncio.wait_for(launch_entered.wait(), timeout=3)
        delete_task = asyncio.create_task(
            harness.workspace.delete_service.delete(
                idempotency_key="node-debug-admitted-start-delete",
                root_node_id=session_id,
            )
        )
        await asyncio.wait_for(drain_attempted.wait(), timeout=3)

        assert harness.workspace.node(session_id).state == "deleting"
        assert not start_task.done()
        assert not delete_task.done()

        release_start.set()
        await asyncio.wait_for(start_task, timeout=3)
        await asyncio.wait_for(delete_task, timeout=3)
        assert launch.calls == 1
    finally:
        release_start.set()
        pending = [start_task]
        if delete_task is not None:
            pending.append(delete_task)
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_node_debug_start_after_drain_cannot_spawn_before_session_rename(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await harness.workspace.create_session(
        "排空后的调试启动", current_provider_id="test-provider"
    )
    thread_id = harness.workspace.resolver.main_thread_id(session_id)
    node_debug = _node_debug_service(harness)
    drain_finished = asyncio.Event()
    release_rename = asyncio.Event()
    admission_attempted = asyncio.Event()
    launch_entered = asyncio.Event()
    launch = _StartBarrier(launch_entered, asyncio.Event())
    monkeypatch.setattr(node_debug, "_create_launch_orchestrator", lambda: launch)

    async def pause_after_owner_drain(current_session_id: str) -> None:
        await node_debug.drain_session(current_session_id)
        drain_finished.set()
        await release_rename.wait()

    harness.workspace.resolver.bind_session_drain_callback(pause_after_owner_drain)
    original_admission = harness.session_service.session_write_admission

    @asynccontextmanager
    async def observe_start_admission(
        current_session_id: str,
    ) -> AsyncIterator[None]:
        if current_session_id == session_id:
            admission_attempted.set()
        async with original_admission(current_session_id):
            yield

    monkeypatch.setattr(
        harness.session_service,
        "session_write_admission",
        observe_start_admission,
    )
    delete_task = asyncio.create_task(
        harness.workspace.delete_service.delete(
            idempotency_key="node-debug-late-start-delete",
            root_node_id=session_id,
        )
    )
    start_task: asyncio.Task[object] | None = None
    try:
        await asyncio.wait_for(drain_finished.wait(), timeout=3)
        assert harness.workspace.node(session_id).state == "deleting"
        assert harness.workspace.session_dir(session_id).is_dir()

        start_task = asyncio.create_task(
            node_debug.start(
                session_id=session_id,
                thread_id=thread_id,
                path="main.mjs",
                args=[],
                breakpoints=[],
            )
        )
        await asyncio.wait_for(admission_attempted.wait(), timeout=3)
        assert not start_task.done()
        assert launch.calls == 0

        release_rename.set()
        await asyncio.wait_for(delete_task, timeout=3)
        with pytest.raises((FileNotFoundError, SessionDeletionPendingError)) as error:
            await asyncio.wait_for(start_task, timeout=3)
        assert "不存在或已删除" in str(
            error.value
        ) or "session_deletion_pending" in str(error.value)
        assert launch.calls == 0
    finally:
        release_rename.set()
        pending = [delete_task]
        if start_task is not None:
            pending.append(start_task)
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
