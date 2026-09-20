"""SessionService.list_child_threads（8.5-B child thread 列表读模型）单测。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.exceptions import NotFoundError
from app.core.identifier import create_prefixed_id
from app.core.session_control_store import (
    SessionControlStore,
    compute_initial_execution_binding_preimage_hash,
    derive_initial_execution_identity,
)
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.trace_event_store import TraceEventStore
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
    """每个测试使用独立临时工作区，不触碰仓库根目录。"""
    return SessionService(
        config_service=ConfigService(),
        trace_event_store=TraceEventStore(sessions_dir=workspace.sessions_root),
        workspace_id=WORKSPACE_ID,
        path_resolver=workspace.resolver,
        creation_service=workspace.creation_service,
    )


def _control_for(service: SessionService, session_id: str) -> SessionControlStore:
    return SessionControlStore(
        service.path_resolver.resolve_session_node(session_id)
        / "session-control.sqlite"
    )


def _insert_child_thread(
    control: SessionControlStore,
    *,
    thread_id: str,
    created_at: str,
    delegation_id: str | None,
    subagent_type: str | None,
    title: str | None,
    state: str,
    admission_state: str | None,
    coordinator_session_id: str | None = None,
) -> None:
    """直接落位 control store 行（读模型测试夹具，经权威 schema）。"""
    connection = control.connection
    connection.execute(
        "INSERT INTO thread_catalog (thread_id, kind, created_at) "
        "VALUES (?, 'child', ?)",
        (thread_id, created_at),
    )
    if delegation_id is not None:
        connection.execute(
            "INSERT INTO collaboration_members "
            "(delegation_id, coordinator_session_id, coordinator_thread_id, "
            "child_thread_id, role, subagent_type, title, task_seed, state, "
            "registered_at, updated_at) VALUES "
            "(?, ?, 'thr_coordinator', ?, 'delegated_subagent', ?, ?, "
            "?, ?, ?, ?)",
            (
                delegation_id,
                coordinator_session_id or "ses_coordinator",
                thread_id,
                subagent_type,
                title,
                '{"description": "做一件事"}',
                state,
                created_at,
                created_at,
            ),
        )
    if admission_state is not None:
        binding_id, job_id = derive_initial_execution_identity(
            delegation_id or thread_id
        )
        session_row = connection.execute(
            "SELECT revision FROM collaboration_ledger WHERE id = 1"
        ).fetchone()
        assert session_row is not None
        preimage_hash = compute_initial_execution_binding_preimage_hash(
            admission_idempotency_key=delegation_id or thread_id,
            session_id="ses_owner",
            thread_id=thread_id,
            creation_idempotency_key=delegation_id or thread_id,
            initial_state="running",
            execution_binding_id=binding_id,
            job_id=job_id,
        )
        connection.execute(
            "INSERT INTO thread_execution_intents "
            "(admission_idempotency_key, session_id, thread_id, "
            "creation_idempotency_key, initial_state, state, "
            "execution_binding_id, job_id, binding_preimage_hash, "
            "claim_owner, claim_generation, last_error, "
            "intent_created_at, intent_updated_at) VALUES "
            "(?, 'ses_owner', ?, ?, 'running', ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
            (
                delegation_id or thread_id,
                thread_id,
                delegation_id or thread_id,
                admission_state,
                binding_id,
                job_id,
                preimage_hash,
                created_at,
                created_at,
            ),
        )
    connection.commit()


async def test_list_child_threads_projects_control_store_state(
    service: SessionService,
) -> None:
    parent = await service.create(SessionCreateRequest(title="父会话"))
    control = _control_for(service, parent.session_id)
    try:
        _insert_child_thread(
            control,
            thread_id="thr_" + "a" * 32,
            created_at="2026-06-01T12:00:00+00:00",
            delegation_id="del_" + "b" * 32,
            subagent_type=SUBAGENT_TYPE,
            title="委派：做一件事",
            state="published",
            admission_state="pending",
            coordinator_session_id=parent.session_id,
        )
    finally:
        control.close()

    result = await service.list_child_threads(parent.session_id)

    assert result.parent_session_id == parent.session_id
    assert result.total == 1
    item = result.items[0]
    assert item.thread_id == "thr_" + "a" * 32
    assert item.created_at.isoformat() == "2026-06-01T12:00:00+00:00"
    assert item.delegation_id == "del_" + "b" * 32
    assert item.subagent_type == SUBAGENT_TYPE
    assert item.title == "委派：做一件事"
    assert item.collaboration_state == "published"
    assert item.admission_state == "pending"
    assert item.status == "pending"
    assert item.role == "delegated_subagent"


async def test_list_child_threads_derives_one_authoritative_status(
    service: SessionService,
) -> None:
    parent = await service.create(SessionCreateRequest(title="状态派生"))
    control = _control_for(service, parent.session_id)
    try:
        _insert_child_thread(
            control,
            thread_id="thr_" + "a" * 32,
            created_at="2026-06-01T12:00:00+00:00",
            delegation_id="del_" + "a" * 32,
            subagent_type=SUBAGENT_TYPE,
            title="运行中",
            state="published",
            admission_state="bound",
            coordinator_session_id=parent.session_id,
        )
        _insert_child_thread(
            control,
            thread_id="thr_" + "b" * 32,
            created_at="2026-06-01T12:01:00+00:00",
            delegation_id="del_" + "b" * 32,
            subagent_type=SUBAGENT_TYPE,
            title="已取消",
            state="cancelled",
            admission_state="pending",
            coordinator_session_id=parent.session_id,
        )
        _insert_child_thread(
            control,
            thread_id="thr_" + "c" * 32,
            created_at="2026-06-01T12:02:00+00:00",
            delegation_id=None,
            subagent_type=None,
            title=None,
            state="registering",
            admission_state=None,
        )
    finally:
        control.close()

    result = await service.list_child_threads(parent.session_id)

    assert {
        item.thread_id: item.status
        for item in result.items
    } == {
        "thr_" + "a" * 32: "running",
        "thr_" + "b" * 32: "failed",
        "thr_" + "c" * 32: "pending",
    }


async def test_list_child_threads_empty_without_control_database(
    service: SessionService,
) -> None:
    parent = await service.create(SessionCreateRequest(title="无子会话"))

    result = await service.list_child_threads(parent.session_id)

    assert result.parent_session_id == parent.session_id
    assert result.items == []
    assert result.total == 0


async def test_list_child_threads_parent_not_found(
    service: SessionService,
) -> None:
    with pytest.raises(NotFoundError, match="ses_missing"):
        await service.list_child_threads("ses_missing")


async def test_list_child_threads_orders_by_created_at_desc(
    service: SessionService,
) -> None:
    parent = await service.create(SessionCreateRequest(title="父会话"))
    control = _control_for(service, parent.session_id)
    try:
        _insert_child_thread(
            control,
            thread_id="thr_" + "a" * 32,
            created_at="2020-01-01T00:00:00+00:00",
            delegation_id=None,
            subagent_type=None,
            title=None,
            state="registering",
            admission_state=None,
        )
        _insert_child_thread(
            control,
            thread_id="thr_" + "c" * 32,
            created_at="2026-06-01T12:00:00+00:00",
            delegation_id=None,
            subagent_type=None,
            title=None,
            state="registering",
            admission_state=None,
        )
    finally:
        control.close()

    result = await service.list_child_threads(parent.session_id)

    assert [item.thread_id for item in result.items] == [
        "thr_" + "c" * 32,
        "thr_" + "a" * 32,
    ]
    # 无 member/intent 的 child row 仍可见，协作/执行字段为 None。
    assert result.items[0].collaboration_state is None
    assert result.items[0].admission_state is None
    assert result.items[0].status == "pending"


async def test_list_child_threads_filters_by_coordinator_session(
    service: SessionService,
) -> None:
    parent = await service.create(SessionCreateRequest(title="父会话"))
    control = _control_for(service, parent.session_id)
    try:
        _insert_child_thread(
            control,
            thread_id="thr_" + "a" * 32,
            created_at="2026-06-01T12:00:00+00:00",
            delegation_id="del_" + "b" * 32,
            subagent_type=SUBAGENT_TYPE,
            title="属主父会话的委派",
            state="published",
            admission_state=None,
            coordinator_session_id=parent.session_id,
        )
        _insert_child_thread(
            control,
            thread_id="thr_" + "e" * 32,
            created_at="2026-06-01T12:01:00+00:00",
            delegation_id="del_" + "d" * 32,
            subagent_type=SUBAGENT_TYPE,
            title="其它 coordinator 的委派",
            state="published",
            admission_state=None,
            coordinator_session_id=create_prefixed_id("ses"),
        )
    finally:
        control.close()

    result = await service.list_child_threads(parent.session_id)

    # member 过滤按 coordinator session：其他 coordinator 的 delegation
    # 不进入本会话侧边栏投影（thread row 本身仍可见）。
    assert result.total == 2
    own = [item for item in result.items if item.delegation_id == "del_" + "b" * 32]
    foreign = [item for item in result.items if item.thread_id == "thr_" + "e" * 32]
    assert own[0].collaboration_state == "published"
    assert foreign[0].collaboration_state is None
