"""SessionService.list_child_threads（8.5-B child thread 列表读模型）单测。"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.agents.graph_binding import (
    DEEP_AGENT_CAPABILITY_PROFILE,
    DEEP_AGENT_GRAPH_BINDING,
    compute_capability_profile_hash,
)
from app.core.exceptions import NotFoundError
from app.core.identifier import create_prefixed_id
from app.core.session_control_store import (
    CollaborationMemberInput,
    SessionControlStore,
)
from app.core.thread_creation import (
    canonical_json_text,
    compute_artifact_manifest_hash,
    compute_thread_creation_preimage_hash,
)
from app.schemas.internal_v2.session import SessionCreateRequest
from app.schemas.internal_v2.team import TeamBoardDTO
from app.services.business.session_service import SessionService
from app.services.business.team.board_manager import TeamBoardManager
from app.services.business.team.rules import require_active_member
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.orchestration.owner_thread_creation_factory import (
    OwnerThreadCreationFactory,
)
from tests.support.canonical_id_at import thread_id_for_name_at
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


def _create_child_thread(
    control: SessionControlStore,
    *,
    owner_session_id: str,
    coordinator_thread_id: str,
    key: str,
    created_at: datetime,
    delegation_id: str | None,
    title: str | None,
    coordinator_session_id: str | None = None,
    team_id: str | None = None,
    publish: bool = True,
    admission_state: str = "pending",
) -> str:
    """通过 owner 创建接口建立 child、member 与 execution intent。"""
    thread_id = thread_id_for_name_at(key, created_at)
    task_seed: dict[str, object] = {"description": title or "普通任务"}
    if team_id is not None:
        task_seed.update(
            trusted_context={
                "team_id": team_id,
                "coordinator_thread_id": coordinator_thread_id,
                "work_mode": "write",
            },
            untrusted_instructions="只完成团队任务。",
        )
    task_reference = (
        {
            "parent_session_id": owner_session_id,
            "parent_thread_id": coordinator_thread_id,
            "delegation_id": delegation_id,
        }
        if delegation_id is not None
        else None
    )
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
    session_metadata = {
        "graph_binding": graph_binding,
        "capability_profile": capability_profile,
        "task_seed": task_seed,
        "task_reference": task_reference,
    }
    artifact_manifest: dict[str, str] = {}
    record = control.create_or_get_thread_creation_record(
        idempotency_key=key,
        initial_state="running",
        preimage_hash=compute_thread_creation_preimage_hash(
            workspace_id=WORKSPACE_ID,
            session_id=owner_session_id,
            thread_id=thread_id,
            delegation_id=delegation_id,
            initial_state="running",
            session_metadata=session_metadata,
            artifact_manifest=artifact_manifest,
        ),
        graph_binding=canonical_json_text(graph_binding),
        capability_profile=canonical_json_text(capability_profile),
        created_at=created_at,
        thread_id=thread_id,
        delegation_id=delegation_id,
        task_seed=canonical_json_text(task_seed),
        task_reference=(
            canonical_json_text(task_reference) if task_reference is not None else None
        ),
        owner_session_id=owner_session_id,
        collaboration_member=(
            CollaborationMemberInput(
                coordinator_session_id=(coordinator_session_id or owner_session_id),
                coordinator_thread_id=coordinator_thread_id,
                role="delegated_subagent",
                subagent_type=SUBAGENT_TYPE,
                title=title or "普通任务",
            )
            if delegation_id is not None
            else None
        ),
    )
    if not publish:
        control.abort_thread_creation_record(key, "夹具在发布前中止")
        return record.child_thread_id

    manifest_text = canonical_json_text(artifact_manifest)
    control.freeze_thread_creation_artifact_manifest(
        key,
        artifact_manifest=manifest_text,
        artifact_manifest_hash=compute_artifact_manifest_hash(artifact_manifest),
    )
    control.publish_thread_creation_record(key, session_id=owner_session_id)
    if admission_state == "bound":
        claimed = control.claim_initial_execution_intent(
            key,
            claim_owner="unit-test",
            claim_generation=1,
        )
        control.mark_initial_execution_bound(
            key,
            execution_binding_id=claimed.execution_binding_id,
            job_id=claimed.job_id,
            claim_owner="unit-test",
            claim_generation=1,
        )
    else:
        assert admission_state == "pending"
    return record.child_thread_id


def _owner_thread_factory(service: SessionService) -> OwnerThreadCreationFactory:
    return OwnerThreadCreationFactory(
        sessions_root=service.path_resolver.sessions_root,
        workspace_id=service.workspace_id,
        path_resolver=service.path_resolver,
    )


def _close_owner_thread_factory(factory: OwnerThreadCreationFactory) -> None:
    for owner in factory._services.values():
        owner.control_store.close()


async def _create_real_delegated_child(
    owner,
    *,
    session_id: str,
    coordinator_thread_id: str,
    idempotency_key: str,
    delegation_id: str,
    team_id: str,
):
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
    session_metadata = {
        "graph_binding": graph_binding,
        "capability_profile": capability_profile,
        "task_seed": {
            "description": "分析团队任务",
            "gateway_id": None,
            "request_id": None,
            "trusted_context": {
                "team_id": team_id,
                "coordinator_thread_id": coordinator_thread_id,
                "work_mode": "write",
            },
            "untrusted_instructions": "只完成团队任务。",
        },
        "task_reference": {
            "parent_session_id": session_id,
            "parent_thread_id": coordinator_thread_id,
            "delegation_id": delegation_id,
        },
    }
    return await owner.create(
        idempotency_key=idempotency_key,
        session_id=session_id,
        thread_id=None,
        delegation_id=delegation_id,
        initial_state="running",
        session_metadata=session_metadata,
        artifact_manifest={},
        collaboration_member={
            "role": "reviewer",
            "subagent_type": SUBAGENT_TYPE,
            "title": "团队审查 child",
        },
    )


class _TeamBoardStore:
    def __init__(self, board: TeamBoardDTO) -> None:
        self.board = board

    def get(self, session_id: str, team_id: str) -> TeamBoardDTO:
        assert (session_id, team_id) == (self.board.session_id, self.board.team_id)
        return self.board

    def list(self, session_id: str) -> list[TeamBoardDTO]:
        assert session_id == self.board.session_id
        return [self.board]

    def recent_events(self, session_id: str, team_id: str, *, limit: int = 20):
        assert (session_id, team_id) == (self.board.session_id, self.board.team_id)
        return []


def _team_board(session_id: str, coordinator_thread_id: str) -> TeamBoardDTO:
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    return TeamBoardDTO(
        team_id="team_consumer_projection",
        name="Consumer projection",
        session_id=session_id,
        coordinator_thread_id=coordinator_thread_id,
        version=1,
        created_at=now,
        updated_at=now,
    )


async def _start_real_child_creation_before_publish(
    owner,
    *,
    session_id: str,
    coordinator_thread_id: str,
    idempotency_key: str,
    delegation_id: str,
    team_id: str,
):
    publish_entered = asyncio.Event()
    resume_publish = asyncio.Event()
    original_publish = owner._publish_and_finalize

    async def wait_before_publish(*args, **kwargs):
        publish_entered.set()
        await resume_publish.wait()
        return await original_publish(*args, **kwargs)

    owner._publish_and_finalize = wait_before_publish
    task = asyncio.create_task(
        _create_real_delegated_child(
            owner,
            session_id=session_id,
            coordinator_thread_id=coordinator_thread_id,
            idempotency_key=idempotency_key,
            delegation_id=delegation_id,
            team_id=team_id,
        )
    )
    await asyncio.wait_for(publish_entered.wait(), timeout=10)
    record = owner.control_store.get_thread_creation_record(idempotency_key)
    return task, resume_publish, record


async def test_list_child_threads_is_a_guarded_sqlite_snapshot_during_real_publish(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = await service.create(SessionCreateRequest(title="读取与发布交错"))
    parent_thread_id = await service.resolve_main_thread(parent.session_id)
    factory = _owner_thread_factory(service)
    owner = factory.for_owner_session(parent.session_id)
    delegation_id = "del_" + "e" * 32
    create_task, resume_publish, record = await _start_real_child_creation_before_publish(
        owner,
        session_id=parent.session_id,
        coordinator_thread_id=parent_thread_id,
        idempotency_key="consumer-read-publication-race",
        delegation_id=delegation_id,
        team_id="team_read_race",
    )
    assert record.state == "preparing"
    assert owner.control_store.get_collaboration_member(delegation_id).state == "registering"

    row_loaded = threading.Event()
    resume_reader = threading.Event()
    snapshot_flags: list[bool] = []
    reader_results = []
    reader_errors: list[Exception] = []
    original_projection = SessionControlStore._collaboration_member_from_row

    def pause_after_member_row(control, row):
        if (
            threading.current_thread().name == "child-list-reader"
            and control.database_path == owner.control_store.database_path
            and row["delegation_id"] == delegation_id
        ):
            snapshot_flags.append(control.connection.in_transaction)
            row_loaded.set()
            if not resume_reader.wait(timeout=10):
                raise TimeoutError("测试读取屏障未释放")
        return original_projection(control, row)

    monkeypatch.setattr(
        SessionControlStore,
        "_collaboration_member_from_row",
        pause_after_member_row,
    )

    def read_child_threads() -> None:
        try:
            reader_results.append(asyncio.run(service.list_child_threads(parent.session_id)))
        except Exception as error:  # noqa: BLE001 - 将读取线程异常传回主测试线程。
            reader_errors.append(error)

    reader = threading.Thread(target=read_child_threads, name="child-list-reader")
    reader.start()
    publication_finished_while_reader_paused = False
    try:
        assert await asyncio.to_thread(row_loaded.wait, 10)
        resume_publish.set()
        try:
            await asyncio.wait_for(asyncio.shield(create_task), timeout=0.2)
            publication_finished_while_reader_paused = True
        except TimeoutError:
            pass
    finally:
        resume_reader.set()
        await asyncio.to_thread(reader.join, 10)
        resume_publish.set()
        if not create_task.done():
            try:
                await asyncio.wait_for(create_task, timeout=10)
            except RuntimeError:
                pass
        _close_owner_thread_factory(factory)

    assert not reader.is_alive()
    assert reader_errors == []
    assert snapshot_flags == [True]
    assert not publication_finished_while_reader_paused
    assert len(reader_results) == 1
    assert reader_results[0].items == []
    result_after_publish = await service.list_child_threads(parent.session_id)
    assert result_after_publish.total == 1


async def test_team_board_hides_registering_and_projects_published_and_cancelled_members(
    service: SessionService,
) -> None:
    parent = await service.create(SessionCreateRequest(title="团队成员发布投影"))
    parent_thread_id = await service.resolve_main_thread(parent.session_id)
    factory = _owner_thread_factory(service)
    owner = factory.for_owner_session(parent.session_id)
    board = _team_board(parent.session_id, parent_thread_id)
    manager = TeamBoardManager(store=_TeamBoardStore(board), factory=factory)

    create_task, resume_publish, record = await _start_real_child_creation_before_publish(
        owner,
        session_id=parent.session_id,
        coordinator_thread_id=parent_thread_id,
        idempotency_key="team-board-before-publish",
        delegation_id="del_" + "f" * 32,
        team_id=board.team_id,
    )
    child_thread_id = record.child_thread_id
    try:
        try:
            hidden_list = manager.list_for_member(parent.session_id, parent_thread_id)
            hidden_read = manager.get_for_member(
                session_id=parent.session_id,
                team_id=board.team_id,
                thread_id=parent_thread_id,
            )
            assert [member.thread_id for member in hidden_list.items[0].members] == [
                parent_thread_id
            ]
            assert [member.thread_id for member in hidden_read.members] == [parent_thread_id]
        finally:
            resume_publish.set()
            published = await asyncio.wait_for(create_task, timeout=10)

        assert published.child_thread_id == child_thread_id
        published_board = manager.get_for_member(
            session_id=parent.session_id,
            team_id=board.team_id,
            thread_id=parent_thread_id,
        )
        published_member = next(
            member for member in published_board.members if member.thread_id == child_thread_id
        )
        assert published_member.status == "pending"

        cancelled_delegation_id = "del_" + "1" * 32
        control = _control_for(service, parent.session_id)
        try:
            cancelled_thread_id = _create_child_thread(
                control,
                owner_session_id=parent.session_id,
                coordinator_thread_id=parent_thread_id,
                key="team-board-cancelled-member",
                created_at=datetime(2026, 6, 1, 12, 2, tzinfo=UTC),
                delegation_id=cancelled_delegation_id,
                title="已取消团队成员",
                team_id=board.team_id,
                publish=False,
            )
            cancelled = control.get_collaboration_member(cancelled_delegation_id)
            with pytest.raises(KeyError):
                control.get_initial_execution_intent("team-board-cancelled-member")
        finally:
            control.close()

        cancelled_board = manager.list_for_member(
            parent.session_id, parent_thread_id
        ).items[0]
        cancelled_member = next(
            member
            for member in cancelled_board.members
            if member.thread_id == cancelled_thread_id
        )
        assert cancelled_member.status == "cancelled"
        assert cancelled_member.updated_at == datetime.fromisoformat(cancelled.updated_at)
        with pytest.raises(PermissionError):
            require_active_member(cancelled_board, cancelled_thread_id)
    finally:
        _close_owner_thread_factory(factory)


async def test_team_board_member_and_creation_validation_share_publication_snapshot(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = await service.create(SessionCreateRequest(title="团队成员快照交错"))
    parent_thread_id = await service.resolve_main_thread(parent.session_id)
    writer_factory = _owner_thread_factory(service)
    reader_factory = _owner_thread_factory(service)
    owner = writer_factory.for_owner_session(parent.session_id)
    reader_factory.for_owner_session(parent.session_id)
    board = _team_board(parent.session_id, parent_thread_id)
    manager = TeamBoardManager(store=_TeamBoardStore(board), factory=reader_factory)
    delegation_id = "del_" + "2" * 32
    create_task, resume_publish, record = await _start_real_child_creation_before_publish(
        owner,
        session_id=parent.session_id,
        coordinator_thread_id=parent_thread_id,
        idempotency_key="team-board-read-publication-race",
        delegation_id=delegation_id,
        team_id=board.team_id,
    )
    assert record.state == "preparing"
    assert owner.control_store.get_collaboration_member(delegation_id).state == "registering"

    member_row_loaded = threading.Event()
    resume_validation = threading.Event()
    observed_states: list[str] = []
    transaction_flags: list[bool] = []
    reader_results = []
    reader_errors: list[Exception] = []
    original_projection = SessionControlStore._collaboration_member_from_row

    def pause_between_member_select_and_validation(control, row):
        if (
            threading.current_thread().name == "team-board-reader"
            and control.database_path == owner.control_store.database_path
            and row["delegation_id"] == delegation_id
        ):
            observed_states.append(str(row["state"]))
            transaction_flags.append(control.connection.in_transaction)
            member_row_loaded.set()
            if not resume_validation.wait(timeout=10):
                raise TimeoutError("测试 member/creation 读取屏障未释放")
        return original_projection(control, row)

    monkeypatch.setattr(
        SessionControlStore,
        "_collaboration_member_from_row",
        pause_between_member_select_and_validation,
    )

    def read_board() -> None:
        try:
            reader_results.append(
                manager.list_for_member(parent.session_id, parent_thread_id)
            )
        except Exception as error:  # noqa: BLE001 - 将读取线程异常传回主测试线程。
            reader_errors.append(error)

    reader = threading.Thread(target=read_board, name="team-board-reader")
    reader.start()
    published = None
    publication_committed_while_reader_paused = False
    try:
        try:
            assert await asyncio.to_thread(member_row_loaded.wait, 10)
            assert observed_states == ["registering"]
            resume_publish.set()
            published = await asyncio.wait_for(asyncio.shield(create_task), timeout=10)
            publication_committed_while_reader_paused = not resume_validation.is_set()
        finally:
            resume_validation.set()
            await asyncio.to_thread(reader.join, 10)
            resume_publish.set()
            if not create_task.done():
                try:
                    await asyncio.wait_for(create_task, timeout=10)
                except RuntimeError:
                    pass

        assert not reader.is_alive()
        assert reader_errors == []
        assert transaction_flags == [True]
        assert publication_committed_while_reader_paused
        assert published is not None
        assert len(reader_results) == 1
        assert [member.thread_id for member in reader_results[0].items[0].members] == [
            parent_thread_id
        ]

        published_board = manager.get_for_member(
            session_id=parent.session_id,
            team_id=board.team_id,
            thread_id=parent_thread_id,
        )
        published_member = next(
            member
            for member in published_board.members
            if member.thread_id == published.child_thread_id
        )
        assert published_member.status == "pending"
    finally:
        _close_owner_thread_factory(writer_factory)
        _close_owner_thread_factory(reader_factory)


async def test_list_child_threads_projects_control_store_state(
    service: SessionService,
) -> None:
    parent = await service.create(SessionCreateRequest(title="父会话"))
    control = _control_for(service, parent.session_id)
    try:
        parent_thread_id = await service.resolve_main_thread(parent.session_id)
        thread_id = _create_child_thread(
            control,
            owner_session_id=parent.session_id,
            coordinator_thread_id=parent_thread_id,
            key="projected-delegation",
            created_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            delegation_id="del_" + "b" * 32,
            title="委派：做一件事",
        )
    finally:
        control.close()

    result = await service.list_child_threads(parent.session_id)

    assert result.parent_session_id == parent.session_id
    assert result.total == 1
    item = result.items[0]
    assert item.thread_id == thread_id
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
        parent_thread_id = await service.resolve_main_thread(parent.session_id)
        pending_thread = _create_child_thread(
            control,
            owner_session_id=parent.session_id,
            coordinator_thread_id=parent_thread_id,
            key="pending-member",
            created_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            delegation_id="del_" + "a" * 32,
            title="待准入",
        )
        bound_thread = _create_child_thread(
            control,
            owner_session_id=parent.session_id,
            coordinator_thread_id=parent_thread_id,
            key="bound-member",
            created_at=datetime(2026, 6, 1, 12, 1, tzinfo=UTC),
            delegation_id="del_" + "b" * 32,
            title="绑定完成",
            admission_state="bound",
        )
        aborted_thread = _create_child_thread(
            control,
            owner_session_id=parent.session_id,
            coordinator_thread_id=parent_thread_id,
            key="aborted-member",
            created_at=datetime(2026, 6, 1, 12, 2, tzinfo=UTC),
            delegation_id="del_" + "c" * 32,
            title="发布前取消",
            publish=False,
        )
        unmember_thread = _create_child_thread(
            control,
            owner_session_id=parent.session_id,
            coordinator_thread_id=parent_thread_id,
            key="unmember-child",
            created_at=datetime(2026, 6, 1, 12, 3, tzinfo=UTC),
            delegation_id=None,
            title=None,
        )
        assert control.get_collaboration_member("del_" + "c" * 32).state == "cancelled"
        assert aborted_thread not in {
            row.thread_id for row in control.list_child_thread_rows()
        }
        with pytest.raises(KeyError):
            control.get_initial_execution_intent("aborted-member")
    finally:
        control.close()

    result = await service.list_child_threads(parent.session_id)

    assert result.total == 3
    assert {item.thread_id: item.status for item in result.items} == {
        pending_thread: "pending",
        bound_thread: "running",
        unmember_thread: "pending",
    }
    unmember = next(item for item in result.items if item.thread_id == unmember_thread)
    assert unmember.collaboration_state is None
    assert unmember.admission_state == "pending"


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
    session_id = create_prefixed_id("ses")
    with pytest.raises(NotFoundError, match=session_id):
        await service.list_child_threads(session_id)


async def test_list_child_threads_orders_by_created_at_desc(
    service: SessionService,
) -> None:
    parent = await service.create(SessionCreateRequest(title="父会话"))
    control = _control_for(service, parent.session_id)
    try:
        parent_thread_id = await service.resolve_main_thread(parent.session_id)
        earlier = _create_child_thread(
            control,
            owner_session_id=parent.session_id,
            coordinator_thread_id=parent_thread_id,
            key="older-child",
            created_at=datetime(2020, 1, 1, tzinfo=UTC),
            delegation_id=None,
            title=None,
        )
        later = _create_child_thread(
            control,
            owner_session_id=parent.session_id,
            coordinator_thread_id=parent_thread_id,
            key="newer-child",
            created_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            delegation_id=None,
            title=None,
        )
    finally:
        control.close()

    result = await service.list_child_threads(parent.session_id)

    assert [item.thread_id for item in result.items] == [later, earlier]
    # 无 member 的 child row 仍可见，并保留 owner 创建的 pending intent。
    assert result.items[0].collaboration_state is None
    assert result.items[0].admission_state == "pending"
    assert result.items[0].status == "pending"


async def test_child_creation_rejects_foreign_coordinator_identity(
    service: SessionService,
) -> None:
    parent = await service.create(SessionCreateRequest(title="父会话"))
    control = _control_for(service, parent.session_id)
    try:
        parent_thread_id = await service.resolve_main_thread(parent.session_id)
        own_thread = _create_child_thread(
            control,
            owner_session_id=parent.session_id,
            coordinator_thread_id=parent_thread_id,
            key="own-coordinator",
            created_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            delegation_id="del_" + "b" * 32,
            title="属主父会话的委派",
        )
    finally:
        control.close()

    with pytest.raises(RuntimeError, match="owner/task reference"):
        invalid_control = _control_for(service, parent.session_id)
        try:
            _create_child_thread(
                invalid_control,
                owner_session_id=parent.session_id,
                coordinator_thread_id=parent_thread_id,
                key="foreign-coordinator",
                created_at=datetime(2026, 6, 1, 12, 1, tzinfo=UTC),
                delegation_id="del_" + "d" * 32,
                title="漂移 coordinator",
                coordinator_session_id=create_prefixed_id("ses"),
            )
        finally:
            invalid_control.close()

    verification = _control_for(service, parent.session_id)
    try:
        assert [
            member.delegation_id for member in verification.list_collaboration_members()
        ] == ["del_" + "b" * 32]
        assert [row.thread_id for row in verification.list_child_thread_rows()] == [
            own_thread
        ]
        with pytest.raises(KeyError):
            verification.get_thread_creation_record("foreign-coordinator")
        with pytest.raises(KeyError):
            verification.get_collaboration_member("del_" + "d" * 32)
        with pytest.raises(KeyError):
            verification.get_initial_execution_intent("foreign-coordinator")
    finally:
        verification.close()

    result = await service.list_child_threads(parent.session_id)

    # owner 不接受外部 coordinator 身份，失败前不产生 child/member。
    assert result.total == 1
    own = [item for item in result.items if item.delegation_id == "del_" + "b" * 32]
    assert own[0].thread_id == own_thread
    assert own[0].collaboration_state == "published"
