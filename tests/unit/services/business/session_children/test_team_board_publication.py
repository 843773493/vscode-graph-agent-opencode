"""Team board 对 child 注册和发布状态的投影。"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime

import pytest

from app.core.session_control_store import SessionControlStore
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.session_service import SessionService
from app.services.business.team.board_manager import TeamBoardManager
from app.services.business.team.rules import require_active_member
from tests.unit.services.business.session_children.helpers import (
    _close_owner_thread_factory,
    _control_for,
    _create_child_thread,
    _owner_thread_factory,
    _start_real_child_creation_before_publish,
    _team_board,
    _TeamBoardStore,
)


async def test_team_board_hides_registering_and_projects_published_and_cancelled_members(
    session_service: SessionService,
) -> None:
    parent = await session_service.create(
        SessionCreateRequest(title="团队成员发布投影")
    )
    parent_thread_id = await session_service.resolve_main_thread(parent.session_id)
    factory = _owner_thread_factory(session_service)
    owner = factory.for_owner_session(parent.session_id)
    board = _team_board(parent.session_id, parent_thread_id)
    manager = TeamBoardManager(store=_TeamBoardStore(board), factory=factory)

    (
        create_task,
        resume_publish,
        record,
    ) = await _start_real_child_creation_before_publish(
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
            assert [member.thread_id for member in hidden_read.members] == [
                parent_thread_id
            ]
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
            member
            for member in published_board.members
            if member.thread_id == child_thread_id
        )
        assert published_member.status == "pending"

        cancelled_delegation_id = "del_" + "1" * 32
        control = _control_for(session_service, parent.session_id)
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
        assert cancelled_member.updated_at == datetime.fromisoformat(
            cancelled.updated_at
        )
        with pytest.raises(PermissionError):
            require_active_member(cancelled_board, cancelled_thread_id)
    finally:
        _close_owner_thread_factory(factory)


async def test_team_board_member_and_creation_validation_share_publication_snapshot(
    session_service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = await session_service.create(
        SessionCreateRequest(title="团队成员快照交错")
    )
    parent_thread_id = await session_service.resolve_main_thread(parent.session_id)
    writer_factory = _owner_thread_factory(session_service)
    reader_factory = _owner_thread_factory(session_service)
    owner = writer_factory.for_owner_session(parent.session_id)
    reader_factory.for_owner_session(parent.session_id)
    board = _team_board(parent.session_id, parent_thread_id)
    manager = TeamBoardManager(store=_TeamBoardStore(board), factory=reader_factory)
    delegation_id = "del_" + "2" * 32
    (
        create_task,
        resume_publish,
        record,
    ) = await _start_real_child_creation_before_publish(
        owner,
        session_id=parent.session_id,
        coordinator_thread_id=parent_thread_id,
        idempotency_key="team-board-read-publication-race",
        delegation_id=delegation_id,
        team_id=board.team_id,
    )
    assert record.state == "preparing"
    assert (
        owner.control_store.get_collaboration_member(delegation_id).state
        == "registering"
    )

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
