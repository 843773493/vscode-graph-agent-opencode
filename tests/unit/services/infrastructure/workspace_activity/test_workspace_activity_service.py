from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.services.infrastructure.workspace_activity import workspace_activity
from app.services.infrastructure.workspace_state_store import (
    WorkspaceActivityCursorGoneError,
    WorkspaceActivityService,
)


@pytest.fixture
def workspace_activity_service(tmp_path: Path) -> Iterator[WorkspaceActivityService]:
    service = WorkspaceActivityService(workspace_root=tmp_path / "workspace")
    try:
        yield service
    finally:
        service.close()


@pytest.mark.asyncio
async def test_workspace_activity_service_persists_and_streams_lightweight_events(workspace_activity_service):
    service = workspace_activity_service
    stream = service.stream(after=0)
    pending = asyncio.create_task(anext(stream))
    try:
        await asyncio.sleep(0)
        await service.append(
            event_id="event-1",
            session_id="session-1",
            status="completed",
            summary="任务完成",
        )
        record = await pending
        assert record.session_id == "session-1"
        assert record.summary == "任务完成"
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()


def test_workspace_activity_prunes_old_events(workspace_activity_service):
    with pytest.raises(ValueError, match="保留天数"):
        workspace_activity_service.store.prune_activity(retention_days=0)


def test_workspace_activity_rejects_cursor_after_all_older_events_are_pruned(workspace_activity_service):
    service = workspace_activity_service
    first = service.store.append_activity(
        event_id="event-1",
        session_id="session-1",
        status="completed",
        summary="任务完成",
    )
    service.store.append_activity(
        event_id="event-2",
        session_id="session-2",
        status="failed",
        summary="任务失败",
    )
    connection = sqlite3.connect(service.store.path)
    try:
        connection.execute("DELETE FROM workspace_activity")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="游标已失效"):
        service.list(after=first.event_seq)


def test_workspace_activity_list_reads_records_and_cursor_bounds_on_one_connection(
    workspace_activity_service,
    monkeypatch,
):
    service = workspace_activity_service
    service.store.append_activity(
        event_id="event-1",
        session_id="session-1",
        status="completed",
        summary="任务完成",
    )
    database = service.store._database
    open_connection = database.connection
    connection_calls = 0

    def count_connection():
        nonlocal connection_calls
        connection_calls += 1
        return open_connection()

    monkeypatch.setattr(database, "connection", count_connection)

    assert [record.event_id for record in service.list()] == ["event-1"]
    assert connection_calls == 1


def test_workspace_activity_rejects_cursor_inside_non_contiguous_retained_window(workspace_activity_service):
    """保留区间内出现空洞时，游标落在空洞里也必须报 CursorGone。"""

    service = workspace_activity_service
    for index in range(1, 6):
        service.store.append_activity(
            event_id=f"event-{index}",
            session_id="session-1",
            status="completed",
            summary="任务完成",
        )
    connection = sqlite3.connect(service.store.path)
    try:
        connection.execute("DELETE FROM workspace_activity WHERE event_seq = 4")
        connection.commit()
    finally:
        connection.close()

    page = service.store.read_activity_page()
    assert (page.first_retained_seq, page.last_allocated_seq) == (1, 5)
    with pytest.raises(WorkspaceActivityCursorGoneError, match="游标已失效"):
        service.list(after=3)


def test_workspace_activity_accepts_cursor_at_contiguous_retained_boundary(workspace_activity_service):
    """游标恰好停在保留区间首条的前一条时，属于连续窗口，必须正常返回。"""

    service = workspace_activity_service
    for index in range(1, 7):
        service.store.append_activity(
            event_id=f"event-{index}",
            session_id="session-1",
            status="completed",
            summary="任务完成",
        )
    connection = sqlite3.connect(service.store.path)
    try:
        connection.execute("DELETE FROM workspace_activity WHERE event_seq <= 4")
        connection.commit()
    finally:
        connection.close()

    page = service.store.read_activity_page()
    assert (page.first_retained_seq, page.last_allocated_seq) == (5, 6)
    assert [item.event_seq for item in service.list(after=4)] == [5, 6]
    with pytest.raises(WorkspaceActivityCursorGoneError, match="游标已失效"):
        service.list(after=3)


def test_workspace_activity_page_reports_bounds_after_prune(workspace_activity_service):
    service = workspace_activity_service
    page = service.store.read_activity_page()
    assert (page.first_retained_seq, page.last_allocated_seq) == (None, 0)
    service.store.append_activity(
        event_id="event-1",
        session_id="session-1",
        status="completed",
        summary="任务完成",
        occurred_at="2000-01-01T00:00:00+00:00",
    )
    service.store.append_activity(
        event_id="event-2",
        session_id="session-2",
        status="failed",
        summary="任务失败",
        occurred_at="2999-01-01T00:00:00+00:00",
    )
    page = service.store.read_activity_page()
    assert (page.first_retained_seq, page.last_allocated_seq) == (1, 2)
    assert service.store.prune_activity(retention_days=30) == 1
    page = service.store.read_activity_page()
    assert (page.first_retained_seq, page.last_allocated_seq) == (2, 2)
    assert [item.event_id for item in page.records] == ["event-2"]


def test_workspace_activity_row_projection_preserves_every_column(workspace_activity_service):
    """行投影必须逐列还原，含去重回读路径的 occurred_at。"""

    service = workspace_activity_service
    created = service.store.append_activity(
        event_id="event-1",
        session_id="session-1",
        status="completed",
        summary="任务完成",
        occurred_at="2026-05-06T07:08:09+00:00",
    )
    duplicate = service.store.append_activity(
        event_id="event-1",
        session_id="session-other",
        status="cancelled",
        summary="重复事件",
        occurred_at="2099-01-01T00:00:00+00:00",
    )
    listed = service.store.read_activity_page().records
    assert len(listed) == 1
    assert listed[0] == created == duplicate
    assert duplicate.occurred_at == "2026-05-06T07:08:09+00:00"
    assert listed[0].occurred_at == "2026-05-06T07:08:09+00:00"
    assert (listed[0].event_seq, listed[0].status) == (1, "completed")


def test_workspace_activity_rejects_invalid_paging_parameters(workspace_activity_service):
    with pytest.raises(ValueError, match="分页参数无效"):
        workspace_activity_service.store.read_activity_page(after=-1)
    with pytest.raises(ValueError, match="分页参数无效"):
        workspace_activity_service.store.read_activity_page(limit=0)
    with pytest.raises(ValueError, match="分页参数无效"):
        workspace_activity_service.store.read_activity_page(limit=2001)


@pytest.mark.asyncio
async def test_workspace_activity_stream_emits_heartbeat_when_idle(workspace_activity_service, monkeypatch):
    async def immediate_timeout(awaitable, *, timeout):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(workspace_activity.asyncio, "wait_for", immediate_timeout)
    stream = workspace_activity_service.stream(after=0)
    try:
        assert await anext(stream) is None
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_workspace_activity_stream_skips_events_already_replayed(workspace_activity_service):
    service = workspace_activity_service
    first = await service.append(
        event_id="event-1",
        session_id="session-1",
        status="completed",
        summary="任务完成",
    )
    stream = service.stream(after=0)
    try:
        assert (await anext(stream)).event_seq == first.event_seq

        duplicate = await service.append(
            event_id="event-1",
            session_id="session-1",
            status="completed",
            summary="任务完成",
        )
        assert duplicate.event_seq == first.event_seq
        second = await service.append(
            event_id="event-2",
            session_id="session-2",
            status="failed",
            summary="任务失败",
        )
        assert (await anext(stream)).event_seq == second.event_seq
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_workspace_activity_rejects_slow_subscriber(workspace_activity_service):
    service = workspace_activity_service
    backlog: asyncio.Queue = asyncio.Queue(maxsize=1)
    backlog.put_nowait("occupied")
    service._subscribers.add(backlog)
    try:
        with pytest.raises(RuntimeError, match="消费速度不足"):
            await service.append(
                event_id="event-1",
                session_id="session-1",
                status="completed",
                summary="任务完成",
            )
    finally:
        service._subscribers.discard(backlog)
