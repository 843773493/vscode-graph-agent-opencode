from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from app.abstractions.background_message_bus import BackgroundMessageKind
from app.core.background_message_bus import BackgroundMessageBus
from app.core.path_utils import get_session_path_resolver
from app.services.infrastructure.background_work_store import BackgroundWorkStore
from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def background_message_bus_case(request, session_bundle_factory):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace_root = context.workspace_root
    shutil.rmtree(workspace_root)
    workspace_root.mkdir()
    sessions = workspace_root / ".boxteam" / "sessions"
    session_ids = [
        "ses_019c3df7f8527d568e834acf3c0f6612",
        "ses_019c70f868da751480de63aa344e28c9",
    ]
    for session_id in session_ids:
        session_bundle_factory(sessions, session_id)
    resolver = get_session_path_resolver(sessions)
    main_thread_ids = [resolver.main_thread_id(session_id) for session_id in session_ids]
    store = BackgroundWorkStore(sessions_dir=sessions)
    return store, session_ids, main_thread_ids, BackgroundMessageBus(store=store)


@pytest.mark.asyncio
async def test_collect_checkpoint_ack_replays_identical_batch_after_restart(background_message_bus_case):
    store, session_ids, main_thread_ids, bus = background_message_bus_case
    for index, content in enumerate(("first", "second", "stop")):
        await bus.emit(session_ids[0], main_thread_ids[0], "agent", content, message_id=f"message-{index}",
                       source_id="clock-stream", kind=BackgroundMessageKind.interrupt if content == "stop" else BackgroundMessageKind.normal)
    batch = await bus.collect(session_ids[0], main_thread_ids[0], "agent", consumption_id="consumer-one", source_id="clock-stream", timeout_seconds=1)
    assert batch.interrupted and not batch.timed_out
    assert [message.content for message in batch.messages] == ["first", "second", "stop"]
    restarted = BackgroundMessageBus(store=store)
    bus.acknowledge(session_ids[0], batch.consumption_id)
    replay = await restarted.collect(session_ids[0], main_thread_ids[0], "agent", consumption_id="consumer-one", source_id="clock-stream", timeout_seconds=1)
    assert replay == batch
    with pytest.raises(RuntimeError, match="请求内容冲突"):
        await restarted.collect(session_ids[0], main_thread_ids[0], "other-agent", consumption_id="consumer-one", source_id="clock-stream", timeout_seconds=1)


@pytest.mark.asyncio
async def test_mailbox_is_durable_exact_owner_and_idempotent(background_message_bus_case):
    store, session_ids, main_thread_ids, bus = background_message_bus_case
    original = await bus.emit(session_ids[0], main_thread_ids[0], "agent", "first", message_id="stable-id", source_id="one")
    replay = await bus.emit(session_ids[0], main_thread_ids[0], "agent", "first", message_id="stable-id", source_id="one")
    assert replay == original
    with pytest.raises(RuntimeError, match="内容冲突"):
        await bus.emit(session_ids[0], main_thread_ids[0], "agent", "changed", message_id="stable-id", source_id="one")
    cold = BackgroundMessageBus(store=store)
    assert await cold.list_messages(session_ids[0], main_thread_ids[0], "agent") == [original]
    assert await cold.list_messages(session_ids[0], main_thread_ids[0], "another-agent") == []
    assert await cold.list_messages(session_ids[1], main_thread_ids[1], "agent") == []
    with pytest.raises(ValueError, match="不属于"):
        await cold.list_messages(session_ids[1], main_thread_ids[1], "agent", after_message_id="stable-id")
    bus.close_session(session_ids[0])
    assert await cold.list_messages(session_ids[0], main_thread_ids[0], "agent") == [original]


@pytest.mark.asyncio
async def test_interrupted_consumer_resumes_checkpoint_without_losing_messages(background_message_bus_case):
    store, session_ids, main_thread_ids, bus = background_message_bus_case
    await bus.emit(session_ids[0], main_thread_ids[0], "agent", "first", message_id="first", source_id="one")
    task = asyncio.create_task(bus.collect(session_ids[0], main_thread_ids[0], "agent", consumption_id="resume", timeout_seconds=1, poll_interval_seconds=0.01))
    for _ in range(100):
        with store.control(session_ids[0], read_only=True) as control:
            count = control.connection.execute("SELECT count(*) FROM background_consumptions WHERE consumption_id='resume' AND batch_json LIKE '%first%'").fetchone()[0]
        if count:
            break
        await asyncio.sleep(0.01)
    assert count == 1
    with pytest.raises(RuntimeError, match="尚未排空"):
        bus.close_session(session_ids[0])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await bus.emit(session_ids[0], main_thread_ids[0], "agent", "stop", message_id="stop", kind="interrupt")
    batch = await BackgroundMessageBus(store=store).collect(session_ids[0], main_thread_ids[0], "agent", consumption_id="resume", timeout_seconds=1, poll_interval_seconds=0.01)
    assert [message.message_id for message in batch.messages] == ["first", "stop"]
    assert batch.interrupted
    bus.close_session(session_ids[0])
