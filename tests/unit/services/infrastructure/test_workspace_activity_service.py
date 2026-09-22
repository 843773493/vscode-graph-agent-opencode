from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app.services.infrastructure.workspace_activity import workspace_activity
from app.services.infrastructure.workspace_state_store import (
    WorkspaceActivityCursorGoneError,
    WorkspaceActivityService,
)


@pytest.mark.asyncio
async def test_workspace_activity_service_persists_and_streams_lightweight_events(tmp_path):
    service = WorkspaceActivityService(workspace_root=tmp_path / "workspace")
    try:
        stream = service.stream(after=0)
        pending = asyncio.create_task(anext(stream))
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
        await stream.aclose()
    finally:
        service.close()


def test_workspace_activity_prunes_old_events(tmp_path):
    service = WorkspaceActivityService(workspace_root=tmp_path / "workspace")
    try:
        with pytest.raises(ValueError, match="保留天数"):
            service.store.prune_activity(retention_days=0)
    finally:
        service.close()


def test_workspace_activity_rejects_cursor_after_all_older_events_are_pruned(tmp_path):
    service = WorkspaceActivityService(workspace_root=tmp_path / "workspace")
    try:
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
    finally:
        service.close()


def test_workspace_activity_rejects_cursor_inside_non_contiguous_retained_window(tmp_path):
    """保留区间内出现空洞时，游标落在空洞里也必须报 CursorGone。"""

    service = WorkspaceActivityService(workspace_root=tmp_path / "workspace")
    try:
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

        assert service.store.activity_bounds() == (1, 5)
        with pytest.raises(WorkspaceActivityCursorGoneError, match="游标已失效"):
            service.list(after=3)
    finally:
        service.close()


def test_workspace_activity_accepts_cursor_at_contiguous_retained_boundary(tmp_path):
    """游标恰好停在保留区间首条的前一条时，属于连续窗口，必须正常返回。"""

    service = WorkspaceActivityService(workspace_root=tmp_path / "workspace")
    try:
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

        assert service.store.activity_bounds() == (5, 6)
        assert [item.event_seq for item in service.list(after=4)] == [5, 6]
        with pytest.raises(WorkspaceActivityCursorGoneError, match="游标已失效"):
            service.list(after=3)
    finally:
        service.close()


def test_workspace_activity_bounds_and_prune_after_clear(tmp_path):
    service = WorkspaceActivityService(workspace_root=tmp_path / "workspace")
    try:
        assert service.store.activity_bounds() == (None, 0)
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
        assert service.store.activity_bounds() == (1, 2)
        assert service.store.prune_activity(retention_days=30) == 1
        assert service.store.activity_bounds() == (2, 2)
        assert [item.event_id for item in service.store.list_activity()] == ["event-2"]
    finally:
        service.close()


def test_workspace_activity_row_projection_preserves_every_column(tmp_path):
    """行投影必须逐列还原，含去重回读路径的 occurred_at。"""

    service = WorkspaceActivityService(workspace_root=tmp_path / "workspace")
    try:
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
        listed = service.store.list_activity()
        assert len(listed) == 1
        assert listed[0] == created == duplicate
        assert duplicate.occurred_at == "2026-05-06T07:08:09+00:00"
        assert listed[0].occurred_at == "2026-05-06T07:08:09+00:00"
        assert (listed[0].event_seq, listed[0].status) == (1, "completed")
    finally:
        service.close()


def test_workspace_activity_rejects_invalid_paging_parameters(tmp_path):
    service = WorkspaceActivityService(workspace_root=tmp_path / "workspace")
    try:
        with pytest.raises(ValueError, match="分页参数无效"):
            service.store.list_activity(after=-1)
        with pytest.raises(ValueError, match="分页参数无效"):
            service.store.list_activity(limit=0)
        with pytest.raises(ValueError, match="分页参数无效"):
            service.store.list_activity(limit=2001)
    finally:
        service.close()


@pytest.mark.asyncio
async def test_workspace_activity_stream_emits_heartbeat_when_idle(tmp_path, monkeypatch):
    service = WorkspaceActivityService(workspace_root=tmp_path / "workspace")
    try:
        async def immediate_timeout(awaitable, *, timeout):
            awaitable.close()
            raise TimeoutError

        monkeypatch.setattr(workspace_activity.asyncio, "wait_for", immediate_timeout)
        stream = service.stream(after=0)
        assert await anext(stream) is None
        await stream.aclose()
    finally:
        stream = None
        service.close()


@pytest.mark.asyncio
async def test_workspace_activity_stream_skips_events_already_replayed(tmp_path):
    service = WorkspaceActivityService(workspace_root=tmp_path / "workspace")
    try:
        first = await service.append(
            event_id="event-1",
            session_id="session-1",
            status="completed",
            summary="任务完成",
        )
        stream = service.stream(after=0)
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
        await stream.aclose()
    finally:
        service.close()


@pytest.mark.asyncio
async def test_workspace_activity_rejects_slow_subscriber(tmp_path):
    service = WorkspaceActivityService(workspace_root=tmp_path / "workspace")
    try:
        backlog: asyncio.Queue = asyncio.Queue(maxsize=1)
        backlog.put_nowait("occupied")
        service._subscribers.add(backlog)
        with pytest.raises(RuntimeError, match="消费速度不足"):
            await service.append(
                event_id="event-1",
                session_id="session-1",
                status="completed",
                summary="任务完成",
            )
        service._subscribers.discard(backlog)
    finally:
        service.close()
