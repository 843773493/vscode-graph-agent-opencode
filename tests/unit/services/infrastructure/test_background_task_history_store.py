from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from app.core.background_task_registry import (
    BackgroundTaskHandle,
    BackgroundTaskRegistry,
)
from app.services.infrastructure.background_task_history_store import (
    BackgroundTaskHistoryStore,
)


@pytest.mark.asyncio
async def test_closed_task_remains_in_persistent_history(tmp_path):
    store = BackgroundTaskHistoryStore(boxteam_root=tmp_path / ".boxteam")
    registry = BackgroundTaskRegistry(history_store=store)

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    handle = registry.spawn(
        session_id="ses_history",
        task_name="monitor_session_agent_end",
        runner=wait_forever,
    )

    await registry.cancel("ses_history", handle.task_id)
    assert registry.list_handles("ses_history") == []
    assert [item.status for item in registry.list_closed_handles("ses_history")] == [
        "cancelled"
    ]

    await registry.delete("ses_history", handle.task_id)
    persisted = store.list_session("ses_history")
    assert len(persisted) == 1
    assert persisted[0].task_id == handle.task_id
    assert persisted[0].status == "deleted"


def test_registry_marks_previous_process_active_tasks_lost(tmp_path):
    store = BackgroundTaskHistoryStore(boxteam_root=tmp_path / ".boxteam")
    store.upsert(
        BackgroundTaskHandle(
            task_id="bgt_stale",
            session_id="ses_stale",
            task_name="emit_system_time_messages",
            status="running",
            created_at=datetime.now(),
            started_at=datetime.now(),
        )
    )

    registry = BackgroundTaskRegistry(history_store=store)

    closed = registry.list_closed_handles("ses_stale")
    assert len(closed) == 1
    assert closed[0].status == "lost"
    assert closed[0].ended_at is not None
    assert "失去运行实体" in str(closed[0].metadata["status_note"])


@pytest.mark.asyncio
async def test_closed_history_can_be_marked_deleted_after_registry_restart(tmp_path):
    store = BackgroundTaskHistoryStore(boxteam_root=tmp_path / ".boxteam")
    handle = BackgroundTaskHandle(
        task_id="bgt_closed",
        session_id="ses_restart_delete",
        task_name="monitor_session_agent_end",
        status="completed",
        created_at=datetime(2026, 7, 13, 9, 0, 0),
        started_at=datetime(2026, 7, 13, 9, 0, 1),
        ended_at=datetime(2026, 7, 13, 9, 0, 2),
    )
    store.upsert(handle)

    restarted_registry = BackgroundTaskRegistry(history_store=store)
    deleted = await restarted_registry.delete(handle.session_id, handle.task_id)

    assert deleted.status == "deleted"
    assert restarted_registry.list_closed_handles(handle.session_id)[0].status == "deleted"
