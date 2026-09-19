from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.core.background_task_registry import (
    BackgroundTaskHandle,
    BackgroundTaskRegistry,
)
from app.services.infrastructure.background_task_history_store import (
    BackgroundTaskHistoryStore,
)


@pytest.mark.asyncio
async def test_closed_task_remains_in_persistent_history(
    tmp_path,
    session_bundle_factory,
):
    sessions_dir = tmp_path / ".boxteam" / "sessions"
    session_bundle_factory(sessions_dir, "ses_b7ec78c3e00646508a9324b3d65fefd0")
    store = BackgroundTaskHistoryStore(sessions_dir=sessions_dir)
    registry = BackgroundTaskRegistry(history_store=store)

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    handle = registry.spawn(
        session_id="ses_b7ec78c3e00646508a9324b3d65fefd0",
        task_name="emit_system_time_messages",
        runner=wait_forever,
    )

    await registry.cancel("ses_b7ec78c3e00646508a9324b3d65fefd0", handle.task_id)
    assert registry.list_handles("ses_b7ec78c3e00646508a9324b3d65fefd0") == []
    assert [item.status for item in registry.list_closed_handles("ses_b7ec78c3e00646508a9324b3d65fefd0")] == [
        "cancelled"
    ]

    await registry.delete("ses_b7ec78c3e00646508a9324b3d65fefd0", handle.task_id)
    persisted = store.list_session("ses_b7ec78c3e00646508a9324b3d65fefd0")
    assert len(persisted) == 1
    assert persisted[0].task_id == handle.task_id
    assert persisted[0].status == "deleted"


def test_registry_marks_previous_process_active_tasks_lost(
    tmp_path,
    session_bundle_factory,
):
    sessions_dir = tmp_path / ".boxteam" / "sessions"
    session_bundle_factory(sessions_dir, "ses_29e5b12a664c4bad8baaf88f2b34a3ab")
    store = BackgroundTaskHistoryStore(sessions_dir=sessions_dir)
    store.upsert(
        BackgroundTaskHandle(
            task_id="bgt_stale",
            session_id="ses_29e5b12a664c4bad8baaf88f2b34a3ab",
            task_name="emit_system_time_messages",
            status="running",
            created_at=datetime.now(UTC),
            started_at=datetime.now(UTC),
        )
    )

    registry = BackgroundTaskRegistry(history_store=store)

    closed = registry.list_closed_handles("ses_29e5b12a664c4bad8baaf88f2b34a3ab")
    assert len(closed) == 1
    assert closed[0].status == "lost"
    assert closed[0].ended_at is not None
    assert "失去运行实体" in str(closed[0].metadata["status_note"])


@pytest.mark.asyncio
async def test_closed_history_can_be_marked_deleted_after_registry_restart(
    tmp_path,
    session_bundle_factory,
):
    sessions_dir = tmp_path / ".boxteam" / "sessions"
    session_bundle_factory(sessions_dir, "ses_e525c94af9c04865850835dc39625280")
    store = BackgroundTaskHistoryStore(sessions_dir=sessions_dir)
    handle = BackgroundTaskHandle(
        task_id="bgt_closed",
        session_id="ses_e525c94af9c04865850835dc39625280",
        task_name="emit_system_time_messages",
        status="completed",
        created_at=datetime(2026, 7, 13, 9, 0, 0, tzinfo=UTC),
        started_at=datetime(2026, 7, 13, 9, 0, 1, tzinfo=UTC),
        ended_at=datetime(2026, 7, 13, 9, 0, 2, tzinfo=UTC),
    )
    store.upsert(handle)

    restarted_registry = BackgroundTaskRegistry(history_store=store)
    deleted = await restarted_registry.delete(handle.session_id, handle.task_id)

    assert deleted.status == "deleted"
    assert restarted_registry.list_closed_handles(handle.session_id)[0].status == "deleted"
