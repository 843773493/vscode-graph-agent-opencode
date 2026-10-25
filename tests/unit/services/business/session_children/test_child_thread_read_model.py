"""SessionService child-thread 列表与状态读模型。"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime

import pytest

from app.core.exceptions import NotFoundError
from app.core.identifier import create_prefixed_id
from app.core.session_control_store import SessionControlStore
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.session_service import SessionService
from tests.unit.services.business.session_children.helpers import (
    SUBAGENT_TYPE,
    _control_for,
    _create_child_thread,
    _owner_thread_factory,
    _start_real_child_creation_before_publish,
)


async def test_list_child_threads_is_a_guarded_sqlite_snapshot_during_real_publish(
    session_service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = await session_service.create(SessionCreateRequest(title="读取与发布交错"))
    parent_thread_id = await session_service.resolve_main_thread(parent.session_id)
    factory = _owner_thread_factory(session_service)
    with factory.for_owner_session(parent.session_id) as owner:
        delegation_id = "del_" + "e" * 32
        (
            create_task,
            resume_publish,
            record,
        ) = await _start_real_child_creation_before_publish(
            owner,
            session_id=parent.session_id,
            coordinator_thread_id=parent_thread_id,
            idempotency_key="consumer-read-publication-race",
            delegation_id=delegation_id,
            team_id="team_read_race",
        )
        assert record.state == "preparing"
        assert (
            owner.control_store.get_collaboration_member(delegation_id).state
            == "registering"
        )

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

                async def read_guarded():
                    async with session_service.session_read_admission(parent.session_id):
                        return await session_service.list_child_threads(parent.session_id)

                reader_results.append(asyncio.run(read_guarded()))
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

        assert not reader.is_alive()
        assert reader_errors == []
        assert snapshot_flags == [True]
        assert not publication_finished_while_reader_paused
        assert len(reader_results) == 1
        assert reader_results[0].items == []
        async with session_service.session_read_admission(parent.session_id):
            result_after_publish = await session_service.list_child_threads(
                parent.session_id
            )
        assert result_after_publish.total == 1


async def test_list_child_threads_projects_control_store_state(
    session_service: SessionService,
) -> None:
    parent = await session_service.create(SessionCreateRequest(title="父会话"))
    control = _control_for(session_service, parent.session_id)
    try:
        parent_thread_id = await session_service.resolve_main_thread(parent.session_id)
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

    async with session_service.session_read_admission(parent.session_id):
        result = await session_service.list_child_threads(parent.session_id)

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
    session_service: SessionService,
) -> None:
    parent = await session_service.create(SessionCreateRequest(title="状态派生"))
    control = _control_for(session_service, parent.session_id)
    try:
        parent_thread_id = await session_service.resolve_main_thread(parent.session_id)
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

    async with session_service.session_read_admission(parent.session_id):
        result = await session_service.list_child_threads(parent.session_id)

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
    session_service: SessionService,
) -> None:
    parent = await session_service.create(SessionCreateRequest(title="无子会话"))

    async with session_service.session_read_admission(parent.session_id):
        result = await session_service.list_child_threads(parent.session_id)

    assert result.parent_session_id == parent.session_id
    assert result.items == []
    assert result.total == 0


async def test_list_child_threads_parent_not_found(
    session_service: SessionService,
) -> None:
    session_id = create_prefixed_id("ses")
    with pytest.raises(NotFoundError, match=session_id):
        async with session_service.session_read_admission(session_id):
            await session_service.list_child_threads(session_id)


async def test_list_child_threads_orders_by_created_at_desc(
    session_service: SessionService,
) -> None:
    parent = await session_service.create(SessionCreateRequest(title="父会话"))
    control = _control_for(session_service, parent.session_id)
    try:
        parent_thread_id = await session_service.resolve_main_thread(parent.session_id)
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

    async with session_service.session_read_admission(parent.session_id):
        result = await session_service.list_child_threads(parent.session_id)

    assert [item.thread_id for item in result.items] == [later, earlier]
    # 无 member 的 child row 仍可见，并保留 owner 创建的 pending intent。
    assert result.items[0].collaboration_state is None
    assert result.items[0].admission_state == "pending"
    assert result.items[0].status == "pending"
