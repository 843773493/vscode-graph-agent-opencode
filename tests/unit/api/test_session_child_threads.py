"""GET /sessions/{session_id}/child-threads API 适配层单元测试。

沿用 tests/unit/api 既有规范：优先直接调用公开处理函数断言错误映射，
并用进程内 TestClient 验证一次真实 HTTP 封套契约；
不启动真实 Workspace 后端进程，文件系统使用 pytest 临时目录。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from shutil import rmtree
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.agents.graph_binding import (
    DEEP_AGENT_CAPABILITY_PROFILE,
    DEEP_AGENT_GRAPH_BINDING,
    compute_capability_profile_hash,
)
from app.api.deps import (
    get_rollout_checkpoint_saver,
    get_session_service,
    get_team_service,
    get_thread_residency_tracker,
)
from app.api.sessions import list_session_child_threads, router
from app.core.trace_middleware import TraceMiddleware
from app.domain.itemized.execution_admission import InternalExecutionIntent
from app.schemas.internal_v2.session import SessionCreateRequest
from app.schemas.internal_v2.team import TeamListDTO
from app.services.business.session_service import SessionService
from app.services.business.team.service import TeamCoordinationService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.mapping.itemized.thread_seed import frozen_thread_seed
from app.services.orchestration.owner_thread_creation_factory import (
    OwnerThreadCreationFactory,
)
from app.services.orchestration.thread_residency import ThreadResidencyTracker
from tests.unit.core.catalog_workspace_helper import build_catalog_workspace

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
SUBAGENT_TYPE = "general-purpose"


@pytest.fixture()
def workspace(tmp_path: Path):
    context = build_catalog_workspace(tmp_path, workspace_id=WORKSPACE_ID)
    try:
        yield context
    finally:
        context.close()


@pytest.fixture()
def service(workspace) -> SessionService:
    return SessionService(
        config_service=ConfigService(),
        trace_event_store=TraceEventStore(sessions_dir=workspace.sessions_root),
        workspace_id=WORKSPACE_ID,
        path_resolver=workspace.resolver,
        creation_service=workspace.creation_service,
    )


@pytest.fixture
def residency_tracker() -> ThreadResidencyTracker:
    return ThreadResidencyTracker()


@pytest.fixture
def saver(workspace) -> RolloutCheckpointSaver:
    return RolloutCheckpointSaver(workspace.sessions_root)


@pytest.fixture
def team_service() -> AsyncMock:
    service = AsyncMock(spec=TeamCoordinationService)
    service.list_teams.return_value = TeamListDTO(items=[])
    return service


@pytest.fixture
def thread_creation_factory(workspace) -> OwnerThreadCreationFactory:
    factory = OwnerThreadCreationFactory(
        sessions_root=workspace.sessions_root,
        workspace_id=WORKSPACE_ID,
        path_resolver=workspace.resolver,
    )
    try:
        yield factory
    finally:
        for service in factory._services.values():
            service.control_store.close()


async def _seed_child_thread(
    service: SessionService,
    factory: OwnerThreadCreationFactory,
    saver: RolloutCheckpointSaver,
    parent_session_id: str,
    parent_thread_id: str,
    *,
    key: str,
    delegation_id: str,
    admit_execution: bool = True,
) -> tuple[str, Path]:
    """经真实 child owner 发布 locator，再提交未 bound 的内部 admission。"""
    capability_profile = dict(DEEP_AGENT_CAPABILITY_PROFILE)
    capability_profile["thread_role"] = "child"
    graph_binding = {
        "graph_id": DEEP_AGENT_GRAPH_BINDING.graph_id,
        "graph_revision": DEEP_AGENT_GRAPH_BINDING.graph_revision,
        "graph_schema_hash": DEEP_AGENT_GRAPH_BINDING.graph_schema_hash,
        "capability_profile_hash": compute_capability_profile_hash(
            capability_profile=capability_profile
        ),
    }
    task_reference = {
        "parent_session_id": parent_session_id,
        "parent_thread_id": parent_thread_id,
        "delegation_id": delegation_id,
    }
    owner = factory.for_owner_session(parent_session_id)
    created = await owner.create(
        idempotency_key=key,
        session_id=parent_session_id,
        thread_id=None,
        delegation_id=delegation_id,
        initial_state="running",
        session_metadata={
            "graph_binding": graph_binding,
            "capability_profile": capability_profile,
            "task_seed": {
                "description": "分析",
                "gateway_id": None,
                "request_id": None,
            },
            "task_reference": task_reference,
        },
        artifact_manifest={},
        collaboration_member={
            "role": "delegated_subagent",
            "subagent_type": SUBAGENT_TYPE,
            "title": "分析 child",
        },
    )
    record = owner.control_store.get_thread_creation_record(key)
    frozen = frozen_thread_seed(record)
    intent = owner.control_store.get_initial_execution_intent(key)
    owner.control_store.validate_initial_execution_readiness(intent)
    assert created.execution_binding_id == intent.execution_binding_id
    assert created.frozen_job_id == intent.job_id
    binding = InternalExecutionIntent(
        message_id=f"seed-{intent.execution_binding_id}",
        execution_id=intent.execution_binding_id,
        admission_idempotency_key=intent.admission_idempotency_key,
        input_reference_json=json.dumps(
            frozen.reference,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    if admit_execution:
        saver.admit_internal_execution(
            parent_session_id,
            thread_id=created.child_thread_id,
            job_id=created.frozen_job_id,
            intent=binding,
            message=None,
        )
    assert owner.control_store.get_initial_execution_intent(key).state == "pending"
    final_dir = service.path_resolver.resolve_thread_node(
        parent_session_id, created.child_thread_id
    )
    assert final_dir.is_dir()
    return created.child_thread_id, final_dir


async def _seed_parent_with_child_thread(
    service: SessionService,
    factory: OwnerThreadCreationFactory,
    saver: RolloutCheckpointSaver,
    *,
    admit_execution: bool = True,
) -> tuple[str, str, Path]:
    parent_session = await service.create(SessionCreateRequest(title="父会话"))
    parent_thread_id = await service.resolve_main_thread(parent_session.session_id)
    thread_id, final_dir = await _seed_child_thread(
        service,
        factory,
        saver,
        parent_session.session_id,
        parent_thread_id,
        key="child-api-published",
        delegation_id="del_" + "b" * 32,
        admit_execution=admit_execution,
    )
    return parent_session.session_id, thread_id, final_dir


async def test_child_threads_api_returns_thread_payload(
    service: SessionService,
    thread_creation_factory: OwnerThreadCreationFactory,
    residency_tracker: ThreadResidencyTracker,
    saver: RolloutCheckpointSaver,
    team_service: AsyncMock,
) -> None:
    parent_session_id, thread_id, _final_dir = await _seed_parent_with_child_thread(
        service, thread_creation_factory, saver
    )

    response = await list_session_child_threads(
        parent_session_id,
        _="local",
        request_id="req_child_threads",
        session_service=service,
        residency_tracker=residency_tracker,
        saver=saver,
        team_service=team_service,
    )

    assert response.data is not None
    assert response.request_id == "req_child_threads"
    data = response.data
    assert data.parent_session_id == parent_session_id
    assert data.total == 1
    assert data.teams == []
    team_service.list_teams.assert_awaited_once_with(
        requester_session_id=parent_session_id,
        requester_thread_id=await service.resolve_main_thread(parent_session_id),
    )
    assert len(data.items) == 1
    item = data.items[0]
    assert item.thread_id == thread_id
    assert item.title == "分析 child"
    assert item.subagent_type == SUBAGENT_TYPE
    assert item.collaboration_state == "published"
    assert item.status == "pending"
    assert item.report_execution_id is not None
    assert item.report_status is None
    assert item.created_at is not None
    assert item.runtime is not None
    assert item.runtime.residency == "cold"
    assert item.runtime.blockers == []


async def test_child_threads_api_maps_missing_parent_to_404(
    service: SessionService,
    residency_tracker: ThreadResidencyTracker,
    saver: RolloutCheckpointSaver,
    team_service: AsyncMock,
) -> None:
    with pytest.raises(HTTPException) as raised:
        await list_session_child_threads(
            "ses_019000000000700080000000000000ff",
            _="local",
            request_id="req_child_threads",
            session_service=service,
            residency_tracker=residency_tracker,
            saver=saver,
            team_service=team_service,
        )

    assert raised.value.status_code == 404
    assert "ses_019000000000700080000000000000ff" in str(raised.value.detail)


def test_child_threads_api_http_envelope_and_auth(
    service: SessionService,
    thread_creation_factory: OwnerThreadCreationFactory,
    residency_tracker: ThreadResidencyTracker,
    saver: RolloutCheckpointSaver,
    team_service: AsyncMock,
) -> None:
    parent_session_id, thread_id, _final_dir = asyncio.run(
        _seed_parent_with_child_thread(service, thread_creation_factory, saver)
    )
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_session_service] = lambda: service
    app.dependency_overrides[get_thread_residency_tracker] = lambda: residency_tracker
    app.dependency_overrides[get_rollout_checkpoint_saver] = lambda: saver
    app.dependency_overrides[get_team_service] = lambda: team_service
    try:
        with TestClient(app) as client:
            unauthorized = client.get(
                f"/api/v1/sessions/{parent_session_id}/child-threads"
            )
            assert unauthorized.status_code == 401

            authorized = client.get(
                f"/api/v1/sessions/{parent_session_id}/child-threads",
                headers={"X-Local-Token": "local-dev-token"},
            )
    finally:
        app.dependency_overrides.clear()

    assert authorized.status_code == 200
    assert authorized.headers["X-Request-ID"]
    body = authorized.json()
    assert body["code"] == 0
    assert body["request_id"] == authorized.headers["X-Request-ID"]
    data = body["data"]
    assert data["parent_session_id"] == parent_session_id
    assert data["total"] == 1
    assert data["teams"] == []
    assert data["items"][0]["thread_id"] == thread_id
    assert data["items"][0]["collaboration_state"] == "published"
    assert data["items"][0]["status"] == "pending"
    assert data["items"][0]["report_execution_id"]
    assert data["items"][0]["report_status"] is None
    assert data["items"][0]["runtime"]["residency"] == "cold"
    assert "session_id" not in data["items"][0]


def test_child_thread_missing_directory_http_keeps_identity_without_native_path(
    service: SessionService,
    thread_creation_factory: OwnerThreadCreationFactory,
    residency_tracker: ThreadResidencyTracker,
    saver: RolloutCheckpointSaver,
    team_service: AsyncMock,
) -> None:
    session_id, thread_id, directory = asyncio.run(
        _seed_parent_with_child_thread(service, thread_creation_factory, saver)
    )
    rmtree(directory)
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_session_service] = lambda: service
    app.dependency_overrides[get_thread_residency_tracker] = lambda: residency_tracker
    app.dependency_overrides[get_rollout_checkpoint_saver] = lambda: saver
    app.dependency_overrides[get_team_service] = lambda: team_service
    with TestClient(app) as client:
        response = client.get(f"/api/v1/sessions/{session_id}/child-threads", headers={
            "X-Local-Token": "local-dev-token", "X-Request-ID": "missing-child-native-path"})
    assert response.status_code == 409
    assert "冻结 locator 缺失或不是普通目录" in response.json()["detail"]
    assert session_id in response.text and thread_id in response.text
    assert str(directory) not in response.text
    assert str(service._path_resolver.sessions_root) not in response.text
    assert response.headers["X-Request-ID"] == "missing-child-native-path"
    assert not directory.exists()


@pytest.mark.asyncio
async def test_child_threads_api_missing_published_locator_fails_closed(
    service: SessionService,
    thread_creation_factory: OwnerThreadCreationFactory,
    residency_tracker: ThreadResidencyTracker,
    saver: RolloutCheckpointSaver,
    team_service: AsyncMock,
) -> None:
    parent_session_id, _thread_id, final_dir = await _seed_parent_with_child_thread(
        service, thread_creation_factory, saver
    )
    rmtree(final_dir)

    with pytest.raises(HTTPException) as raised:
        await list_session_child_threads(
            parent_session_id,
            _="local",
            request_id="req_child_threads_missing_locator",
            session_service=service,
            residency_tracker=residency_tracker,
            saver=saver,
            team_service=team_service,
        )

    assert raised.value.status_code == 409
    assert "冻结 locator 缺失或不是普通目录" in str(raised.value.detail)
    assert parent_session_id in str(raised.value.detail)
    assert _thread_id in str(raised.value.detail)
    assert str(final_dir) not in str(raised.value.detail)
    assert str(service._path_resolver.sessions_root) not in str(raised.value.detail)


@pytest.mark.asyncio
async def test_child_threads_pending_read_does_not_materialize_rollout(
    service: SessionService,
    thread_creation_factory: OwnerThreadCreationFactory,
    residency_tracker: ThreadResidencyTracker,
    saver: RolloutCheckpointSaver,
    team_service: AsyncMock,
) -> None:
    parent_id, thread_id, final_dir = await _seed_parent_with_child_thread(
        service, thread_creation_factory, saver, admit_execution=False
    )
    assert final_dir.is_dir()
    rollout_root = saver._storage.storage.root(parent_id, thread_id=thread_id)
    assert not rollout_root.exists()

    response = await list_session_child_threads(
        parent_id, _="local", request_id="req_cold_child_no_rollout",
        session_service=service, residency_tracker=residency_tracker,
        saver=saver, team_service=team_service,
    )
    assert len(response.data.items) == 1
    item = response.data.items[0]
    assert item.thread_id == thread_id
    assert item.status == "pending"
    assert item.report_execution_id is not None
    assert item.report_status is None
    assert item.runtime.residency == "cold"
    assert not rollout_root.exists()


@pytest.mark.asyncio
async def test_child_thread_report_read_blocks_real_delete_until_response_is_complete(
    workspace,
    service: SessionService,
    thread_creation_factory: OwnerThreadCreationFactory,
    residency_tracker: ThreadResidencyTracker,
    saver: RolloutCheckpointSaver,
    team_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_id, thread_id, final_dir = await _seed_parent_with_child_thread(
        service, thread_creation_factory, saver
    )
    control = thread_creation_factory.for_owner_session(parent_id).control_store
    intent = control.claim_initial_execution_intent(
        "child-api-published", claim_owner="report-race", claim_generation=1,
    )
    control.mark_initial_execution_bound(
        intent.admission_idempotency_key,
        execution_binding_id=intent.execution_binding_id, job_id=intent.job_id,
        claim_owner="report-race", claim_generation=1,
    )
    query_paused, continue_query = asyncio.Event(), asyncio.Event()
    delete_attempted, delete_admitted = asyncio.Event(), asyncio.Event()
    original_exclusive = workspace.delete_service._gate.exclusive

    @asynccontextmanager
    async def observe_delete_admission():
        delete_attempted.set()
        async with original_exclusive():
            delete_admitted.set()
            yield

    async def pause_teams(**kwargs):
        query_paused.set()
        await continue_query.wait()
        return TeamListDTO(items=[])

    original_outcome = saver.runtime_execution_outcome
    report_reads = []
    def read_report(session_id, current_thread_id, execution_id):
        assert not delete_admitted.is_set()
        assert final_dir.is_dir()
        report_reads.append((session_id, current_thread_id, execution_id))
        return original_outcome(session_id, current_thread_id, execution_id)

    monkeypatch.setattr(workspace.delete_service._gate, "exclusive", observe_delete_admission)
    monkeypatch.setattr(saver, "runtime_execution_outcome", read_report)
    team_service.list_teams.side_effect = pause_teams
    query = asyncio.create_task(list_session_child_threads(
        parent_id, _="local", request_id="req_child_delete_race",
        session_service=service, residency_tracker=residency_tracker,
        saver=saver, team_service=team_service,
    ))
    delete = None
    try:
        await asyncio.wait_for(query_paused.wait(), timeout=3)
        delete = asyncio.create_task(workspace.delete_service.delete(
            idempotency_key="child-report-delete-race", root_node_id=parent_id,
        ))
        await asyncio.wait_for(delete_attempted.wait(), timeout=3)
        assert not delete_admitted.is_set()
        assert workspace.node(parent_id).state == "active"
        continue_query.set()
        response = await asyncio.wait_for(query, timeout=3)
        assert response.data.items[0].admission_state == "bound"
        assert report_reads == [(parent_id, thread_id, intent.execution_binding_id)]
        await asyncio.wait_for(delete, timeout=3)
        assert delete_admitted.is_set()
        assert not final_dir.exists()
        with pytest.raises(KeyError):
            workspace.node(parent_id)
    finally:
        continue_query.set()
        for task in (query, delete):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (query, delete) if task is not None), return_exceptions=True)
