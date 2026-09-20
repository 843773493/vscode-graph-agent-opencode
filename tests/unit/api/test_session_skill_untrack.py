"""POST /sessions/{session_id}/threads/{thread_id}/skills/untrack 测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.deps import get_session_skill_tracking_service
from app.api.sessions import router, untrack_session_skill
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.session import (
    SessionCreateRequest,
    SessionSkillUntrackRequest,
)
from app.services.business.session_service import SessionService
from app.services.business.session_skill_tracking_service import (
    SessionSkillTrackingService,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    MAIN_THREAD_ID,
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
    _revision,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage
from app.services.infrastructure.trace_event_store import TraceEventStore

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
SKILL_NAME = "debugging"
SKILL_CONTENT = "v1\n"


@pytest.fixture()
def workspace(tmp_path: Path):
    ws = build_catalog_workspace(tmp_path, workspace_id=WORKSPACE_ID)
    yield ws
    ws.close()


@pytest.fixture()
def sessions_root(workspace) -> Path:
    return workspace.sessions_root


@pytest.fixture()
def session_service(sessions_root: Path, workspace) -> SessionService:
    return SessionService(
        config_service=ConfigService(),
        trace_event_store=TraceEventStore(sessions_dir=sessions_root),
        workspace_id=WORKSPACE_ID,
        path_resolver=workspace.resolver,
        creation_service=workspace.creation_service,
    )


@pytest.fixture()
def saver(sessions_root: Path) -> RolloutCheckpointSaver:
    return RolloutCheckpointSaver(
        sessions_root,
        storage=RolloutStorage(sessions_root),
    )


@pytest.fixture()
def service(
    saver: RolloutCheckpointSaver,
    session_service: SessionService,
) -> SessionSkillTrackingService:
    return SessionSkillTrackingService(
        checkpointer=saver,
        session_service=session_service,
    )


def _descriptor() -> ContextSourceDescriptor:
    # internal_locator 指向不存在的文件：证明 untrack 不读取 source。
    return ContextSourceDescriptor(
        source_id="skill:debugging",
        source_kind="skill",
        name=SKILL_NAME,
        description="调试工作流",
        internal_locator="/.boxteam/skills/debugging/SKILL.md",
    )


def _snapshot() -> SkillCatalogActivationSnapshot:
    return SkillCatalogActivationSnapshot(
        catalog_revision="sha256:test-catalog",
        entries={
            SKILL_NAME: SkillCatalogBinding(
                name=SKILL_NAME,
                resource_id="skill-entry:test:debugging:activation",
                entry_identity="skill-entry:test:debugging",
                display_uri="boxteam://workspace/skill/debugging",
                activation_revision=_revision(SKILL_CONTENT),
                body=SKILL_CONTENT,
            )
        },
    )


async def _seed_session_with_tracked_skill(
    session_service: SessionService,
    saver: RolloutCheckpointSaver,
) -> tuple[str, ContextSourceOwnerKey]:
    session = await session_service.create(
        SessionCreateRequest(title="untrack 测试")
    )
    owner = ContextSourceOwnerKey(
        session_id=session.session_id,
        thread_id=MAIN_THREAD_ID,
    )
    manager = ContextSourceManager(owner=owner, control_state_port=saver)
    manager.register(_descriptor())
    manager.activate_skill_content(SKILL_NAME, SKILL_CONTENT)
    manager.install_skill_activation_snapshot(_snapshot())
    manager.load_skill(SKILL_NAME, mode="tracked")
    batch = manager.prepare_pending()
    assert batch is not None
    manager.commit_model_call_pending(batch)
    return session.session_id, owner


async def test_untrack_success_persists_frozen_state_without_reading_source(
    service: SessionSkillTrackingService,
    saver: RolloutCheckpointSaver,
    session_service: SessionService,
) -> None:
    session_id, owner = await _seed_session_with_tracked_skill(session_service, saver)

    result = await service.untrack(
        session_id=session_id,
        thread_id=MAIN_THREAD_ID,
        name=SKILL_NAME,
    )

    assert result.session_id == session_id
    assert result.thread_id == MAIN_THREAD_ID
    assert result.status == "loaded"
    assert result.tracked is False
    assert result.queued is False
    assert result.error is None
    # 恢复 registration 不携带 descriptor：display_uri 返回 None，不伪造 URI。
    assert result.display_uri is None
    assert result.revision is not None
    (stored,) = saver.load_context_source_control_states(owner)
    assert stored.tracking_status == "untracked"


async def test_untrack_not_tracked_is_deterministic_and_zero_state_change(
    service: SessionSkillTrackingService,
    saver: RolloutCheckpointSaver,
    session_service: SessionService,
) -> None:
    session_id, owner = await _seed_session_with_tracked_skill(session_service, saver)
    first = await service.untrack(
        session_id=session_id, thread_id=MAIN_THREAD_ID, name=SKILL_NAME
    )
    assert first.status == "loaded"
    (tracked_state,) = saver.load_context_source_control_states(owner)
    revision_before = tracked_state.state_revision

    second = await service.untrack(
        session_id=session_id, thread_id=MAIN_THREAD_ID, name=SKILL_NAME
    )

    assert second.status == "not_tracked"
    assert second.tracked is False
    assert second.error is None
    (stored_after,) = saver.load_context_source_control_states(owner)
    assert stored_after.state_revision == revision_before


async def test_untrack_api_handler_maps_unknown_thread_to_404(
    service: SessionSkillTrackingService,
    saver: RolloutCheckpointSaver,
    session_service: SessionService,
) -> None:
    session_id, _owner = await _seed_session_with_tracked_skill(session_service, saver)

    with pytest.raises(HTTPException) as raised:
        await untrack_session_skill(
            session_id,
            "thr_" + "9" * 32,
            SessionSkillUntrackRequest(name=SKILL_NAME),
            _="local",
            request_id="req_untrack",
            skill_tracking_service=service,
        )

    assert raised.value.status_code == 404


def test_untrack_api_http_envelope_auth_and_success(
    saver: RolloutCheckpointSaver,
    session_service: SessionService,
    service: SessionSkillTrackingService,
) -> None:
    session_id, owner = asyncio.run(
        _seed_session_with_tracked_skill(session_service, saver)
    )
    (before,) = saver.load_context_source_control_states(owner)
    url = f"/api/v1/sessions/{session_id}/threads/main/skills/untrack"
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_session_skill_tracking_service] = lambda: service
    try:
        with TestClient(app) as client:
            unauthorized = client.post(url, json={"name": SKILL_NAME})
            assert unauthorized.status_code == 401
            (after_unauthorized,) = saver.load_context_source_control_states(owner)
            assert after_unauthorized.state_revision == before.state_revision

            authorized = client.post(
                url,
                json={"name": SKILL_NAME},
                headers={"X-Local-Token": "local-dev-token"},
            )
    finally:
        app.dependency_overrides.clear()

    assert authorized.status_code == 200
    body = authorized.json()
    assert body["code"] == 0
    assert body["request_id"] == authorized.headers["X-Request-ID"]
    data = body["data"]
    assert data["status"] == "loaded"
    assert data["tracked"] is False
    assert data["mode"] == "untrack"
    assert data["error"] is None
    (after,) = saver.load_context_source_control_states(owner)
    assert after.tracking_status == "untracked"
from tests.unit.core.catalog_workspace_helper import build_catalog_workspace
