from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.api import resources as resources_api
from app.api import session_navigation as api
from app.api.persistent_tasks import open_persistent_task_sse
from app.schemas.internal_v2.persistent_task import (
    PersistentTaskListDTO,
    TaskRuntimeStatusDTO,
)
from app.schemas.internal_v2.session_navigation.operations import NavigationSnapshotDTO
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.persistent_tasks.events import (
    PersistentTaskChanged,
    task_channel,
)

SESSION_ID = "ses_019bef80066f7038ae8e88ccb2e5e2ea"
THREAD_ID = "thr_018f0000000070008000000000000001"


@pytest.fixture
def task_snapshot_service():
    channel = task_channel(EventChannelService(), "task-snapshot-test")
    snapshot = PersistentTaskListDTO(items=[], runtime=TaskRuntimeStatusDTO(state="running", recovering_tasks=0))
    return SimpleNamespace(runtime=SimpleNamespace(events=channel), snapshot=lambda _session: snapshot)


@pytest.mark.parametrize("overflow", [False, True])
async def test_goal_notifications_keep_history_but_event_gaps_invalidate_it(task_snapshot_service, overflow):
    stream = open_persistent_task_sse(task_snapshot_service)
    try:
        initial = json.loads((await anext(stream)).split("data: ", 1)[1])
        assert initial["history_invalidated"] is True
        for _ in range(65 if overflow else 1):
            task_snapshot_service.runtime.events.publish(PersistentTaskChanged(history_invalidated=False))
        invalidations = [json.loads((await anext(stream)).split("data: ", 1)[1])["history_invalidated"]
                         for _ in range(64 if overflow else 1)]
        assert invalidations == ([False] * 63 + [True] if overflow else [False])
    finally:
        await stream.aclose()


async def test_idle_navigation_confirms_original_snapshot_on_heartbeat(monkeypatch):
    closed = asyncio.Event()
    snapshot = NavigationSnapshotDTO(workspace_id="workspace", catalog_revision=7, event_seq_watermark=2, generation=7, projection_revision="old")
    reads = []

    async def changes():
        try:
            yield snapshot
            await asyncio.Event().wait()
        finally:
            closed.set()

    def read():
        reads.append(snapshot.catalog_revision)
        return snapshot.model_copy()

    monkeypatch.setattr(api, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    stream = api._stream_navigation_changes(SimpleNamespace(operations=SimpleNamespace(changes=changes), navigation_snapshot=read))
    try:
        assert (await anext(stream)).startswith("event: session_navigation\n")
        snapshot.catalog_revision = 8
        assert await asyncio.wait_for(anext(stream), 0.5) == ": heartbeat\n\n"
        frame = await asyncio.wait_for(anext(stream), 0.5)
        assert json.loads(frame.split("data: ", 1)[1])["catalog_revision"] == 8
        snapshot.projection_revision = "new"
        assert await asyncio.wait_for(anext(stream), 0.5) == ": heartbeat\n\n"
        frame = await asyncio.wait_for(anext(stream), 0.5)
        assert json.loads(frame.split("data: ", 1)[1])["projection_revision"] == "new"
        assert reads == [7, 8, 8]
    finally:
        await stream.aclose()
    assert closed.is_set()


async def test_navigation_commit_keeps_frame_when_scalar_version_is_unchanged():
    snapshot = NavigationSnapshotDTO(workspace_id="workspace", catalog_revision=7, event_seq_watermark=2, generation=7)

    async def changes():
        yield snapshot
        yield snapshot

    service = SimpleNamespace(operations=SimpleNamespace(changes=changes), navigation_snapshot=lambda: snapshot)
    stream = api._stream_navigation_changes(service)
    try:
        first = await anext(stream)
        assert await anext(stream) == first
    finally:
        await stream.aclose()



async def test_terminal_changes_are_initial_and_pushed_while_heartbeat_does_not_query(monkeypatch):
    changed = asyncio.Event()
    closed = asyncio.Event()

    async def changes():
        try:
            yield None
            await changed.wait()
            yield frozenset({"term_changed"})
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(resources_api, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    stream = resources_api._stream_terminal_changes(SimpleNamespace(changes=changes))
    try:
        assert await anext(stream) == 'event: resources_changed\ndata: {"resource_ids":null}\n\n'
        assert await asyncio.wait_for(anext(stream), 0.5) == ': heartbeat\n\n'
        changed.set()
        assert await asyncio.wait_for(anext(stream), 0.5) == 'event: resources_changed\ndata: {"resource_ids":["term_changed"]}\n\n'
    finally:
        await stream.aclose()
    assert closed.is_set()
