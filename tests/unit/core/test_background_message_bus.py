from __future__ import annotations

import pytest

from app.abstractions.background_message_bus import BackgroundMessageKind
from app.core.background_message_bus import (
    BackgroundMessageBus,
    emit_background_message,
)


@pytest.fixture(autouse=True)
def reset_background_message_service():
    yield


@pytest.mark.asyncio
async def test_collect_background_messages_stops_on_interrupt():
    service = BackgroundMessageBus()
    session_id = "session_test"
    agent_id = "deep_agent"
    source_id = "clock-stream"

    service.emit(session_id, agent_id, "first", kind=BackgroundMessageKind.normal, source_id=source_id)
    service.emit(session_id, agent_id, "second", kind=BackgroundMessageKind.normal, source_id=source_id)
    service.emit(session_id, agent_id, "stop", kind=BackgroundMessageKind.interrupt, source_id=source_id)

    batch = await service.collect(
        session_id,
        agent_id,
        source_id=source_id,
        timeout_seconds=1,
        poll_interval_seconds=0.05,
    )

    assert batch.interrupted is True
    assert batch.timed_out is False
    assert [message.content for message in batch.messages] == ["first", "second", "stop"]
    assert batch.messages[-1].kind == BackgroundMessageKind.interrupt


@pytest.mark.asyncio
async def test_collect_background_messages_filters_source():
    service = BackgroundMessageBus()
    session_id = "session_test"
    agent_id = "deep_agent"

    service.emit(session_id, agent_id, "alpha-1", source_id="alpha")
    service.emit(session_id, agent_id, "beta-1", source_id="beta")

    batch = await service.collect(
        session_id,
        agent_id,
        source_id="alpha",
        timeout_seconds=1,
        poll_interval_seconds=0.05,
    )

    assert batch.interrupted is False
    assert batch.timed_out is True
    assert [message.content for message in batch.messages] == ["alpha-1"]


def test_emit_background_message_requires_explicit_identifiers():
    with pytest.raises(RuntimeError, match="session_id 不能为空"):
        emit_background_message("missing session", agent_id="deep_agent")

    with pytest.raises(RuntimeError, match="agent_id 不能为空"):
        emit_background_message("missing agent", session_id="session_test")


@pytest.mark.asyncio
async def test_close_session_releases_all_agent_backlogs_and_preserves_other_session():
    service = BackgroundMessageBus()
    for agent_id in ("agent-a", "agent-b"):
        service.emit("deleted-session", agent_id, "已删除会话的消息")
    retained = service.emit("retained-session", "agent-a", "保留会话的消息")

    service.close_session("deleted-session")
    service.close_session("deleted-session")

    assert all(key[0] != "deleted-session" for key in service._messages)
    for agent_id in ("agent-a", "agent-b"):
        assert await service.list_messages("deleted-session", agent_id) == []
    assert await service.list_messages("retained-session", "agent-a") == [retained]


@pytest.mark.asyncio
async def test_close_session_rejects_active_collector_without_dropping_backlog():
    service = BackgroundMessageBus()
    original = service.emit("session", "agent", "收集者仍需读取")
    queue = await service.subscribe("session", "agent")

    with pytest.raises(RuntimeError, match="后台消息收集尚未排空.*session.*agent"):
        service.close_session("session")

    assert await service.list_messages("session", "agent") == [original]
    following = service.emit("session", "agent", "失败后保留订阅")
    assert queue.get_nowait() == following
    await service.unsubscribe("session", "agent", queue)
    service.close_session("session")
    assert service._messages == {}
