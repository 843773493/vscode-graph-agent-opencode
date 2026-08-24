from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app.services.infrastructure.workspace_state_store import (
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
