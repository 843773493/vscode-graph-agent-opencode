"""GET /sessions/{session_id}/child-threads API 适配层单元测试。

沿用 tests/unit/api 既有规范：优先直接调用公开处理函数断言错误映射，
并用进程内 TestClient 验证一次真实 HTTP 封套契约；
不启动真实 Workspace 后端进程，文件系统使用 pytest 临时目录。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.deps import (
    get_rollout_checkpoint_saver,
    get_session_service,
    get_team_service,
    get_thread_residency_tracker,
)
from app.api.sessions import list_session_child_threads, router
from app.core.session_control_store import SessionControlStore
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.session import SessionCreateRequest
from app.schemas.internal_v2.team import TeamListDTO
from app.services.business.session_service import SessionService
from app.services.business.team.service import TeamCoordinationService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.trace_event_store import TraceEventStore
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
def service(workspace, saver: RolloutCheckpointSaver) -> SessionService:
    return SessionService(
        config_service=ConfigService(),
        trace_event_store=TraceEventStore(sessions_dir=workspace.sessions_root),
        workspace_id=WORKSPACE_ID,
        path_resolver=workspace.resolver,
        creation_service=workspace.creation_service,
        initialize_thread_storage=saver.initialize_thread_storage,
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


def _seed_child_thread(
    service: SessionService,
    parent_session_id: str,
    *,
    thread_id: str,
    delegation_id: str,
) -> None:
    """按权威 schema 直落 control store 行（API 适配层测试夹具）。"""
    control = SessionControlStore(
        service.path_resolver.resolve_session_node(parent_session_id)
        / "session-control.sqlite"
    )
    try:
        connection = control.connection
        connection.execute(
            "INSERT INTO thread_catalog (thread_id, kind, created_at) "
            "VALUES (?, 'child', '2026-06-01T12:00:00+00:00')",
            (thread_id,),
        )
        connection.execute(
            "INSERT INTO collaboration_members "
            "(delegation_id, coordinator_session_id, coordinator_thread_id, "
            "child_thread_id, role, subagent_type, title, task_seed, state, "
            "registered_at, updated_at) VALUES "
            "(?, ?, 'thr_coordinator', ?, 'delegated_subagent', ?, ?, ?, "
            "'published', '2026-06-01T12:00:00+00:00', "
            "'2026-06-01T12:00:00+00:00')",
            (
                delegation_id,
                parent_session_id,
                thread_id,
                SUBAGENT_TYPE,
                "分析 child",
                '{"description": "分析"}',
            ),
        )
        connection.commit()
    finally:
        control.close()


async def _seed_parent_with_child_thread(
    service: SessionService,
) -> tuple[str, str]:
    parent_session = await service.create(SessionCreateRequest(title="父会话"))
    thread_id = "thr_" + "a" * 32
    _seed_child_thread(
        service,
        parent_session.session_id,
        thread_id=thread_id,
        delegation_id="del_" + "b" * 32,
    )
    return parent_session.session_id, thread_id


async def test_child_threads_api_returns_thread_payload(
    service: SessionService,
    residency_tracker: ThreadResidencyTracker,
    saver: RolloutCheckpointSaver,
    team_service: AsyncMock,
) -> None:
    parent_session_id, thread_id = await _seed_parent_with_child_thread(service)

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
            "ses_missing",
            _="local",
            request_id="req_child_threads",
            session_service=service,
            residency_tracker=residency_tracker,
            saver=saver,
            team_service=team_service,
        )

    assert raised.value.status_code == 404
    assert "ses_missing" in str(raised.value.detail)


def test_child_threads_api_http_envelope_and_auth(
    service: SessionService,
    residency_tracker: ThreadResidencyTracker,
    saver: RolloutCheckpointSaver,
    team_service: AsyncMock,
) -> None:
    parent_session_id, thread_id = asyncio.run(
        _seed_parent_with_child_thread(service)
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
    assert data["items"][0]["runtime"]["residency"] == "cold"
    assert "session_id" not in data["items"][0]
