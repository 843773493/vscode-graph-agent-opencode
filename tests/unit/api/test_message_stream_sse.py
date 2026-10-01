from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

import pytest

from app.api.message_stream import _stream_message_sse
from app.core.path_utils import get_session_path_resolver
from app.services.infrastructure.message_stream_store import MessageStreamStore
from tests.support.catalog_session_bundle import seed_catalog_session_bundle

SESSION_ID = "ses_12345678123446788234567812345678"


class _Request:
    """只实现 ``_stream_message_sse`` 需要的断开探测。"""

    def __init__(self, disconnected: bool = False) -> None:
        self._disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self._disconnected


class _IdleRecords:
    """永不产出事件的记录流，用于观测空闲心跳。"""

    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self) -> _IdleRecords:
        return self

    async def __anext__(self) -> Mapping[str, object]:
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def message_stream_store(tmp_path: Path) -> tuple[MessageStreamStore, str]:
    sessions_root = tmp_path / "workspace" / ".boxteam" / "sessions"
    resolver = get_session_path_resolver(sessions_root)
    resolver.initialize()
    seed_catalog_session_bundle(sessions_root, SESSION_ID)
    return MessageStreamStore(path_resolver=resolver), SESSION_ID


@pytest.mark.asyncio
async def test_message_stream_sse_emits_heartbeat_while_idle() -> None:
    """无事件时在心跳间隔后收到 SSE 注释心跳。"""
    records = _IdleRecords()
    stream = _stream_message_sse(
        records,
        request=_Request(),
        heartbeat_interval_seconds=0.01,
    )

    frame = await asyncio.wait_for(anext(stream), timeout=0.5)

    assert frame == ": heartbeat\n\n"
    # 心跳必须是 SSE 注释，不得伪装成业务事件（否则前端会收到未知 type）。
    assert "event:" not in frame
    assert "data:" not in frame
    await stream.aclose()
    assert records.closed is True


@pytest.mark.asyncio
async def test_message_stream_sse_emits_event_block_without_heartbeat(
    message_stream_store: tuple[MessageStreamStore, str],
) -> None:
    """有事件时输出完整事件帧，且不掺入心跳。"""
    store, session_id = message_stream_store
    writer = await store.open(session_id=session_id, turn_id="job_sse_event")
    await writer.commit(
        "block.started",
        {"block_id": "block_1", "block_index": 0, "carrier_type": "reasoning"},
    )
    stream = _stream_message_sse(
        store.stream_records(
            session_id=session_id,
            turn_stream_id=writer.turn_stream_id,
            after_seq=0,
        ),
        request=_Request(),
        heartbeat_interval_seconds=1,
    )

    frames: list[str] = []
    for _ in range(2):
        frames.append(await asyncio.wait_for(anext(stream), timeout=0.5))

    assert frames[0].startswith("id: 1\nevent: stream.opened\ndata: ")
    assert frames[1].startswith("id: 2\nevent: block.started\ndata: ")
    assert all(frame.endswith("\n\n") for frame in frames)
    assert all(": heartbeat" not in frame for frame in frames)
    await stream.aclose()


@pytest.mark.asyncio
async def test_message_stream_sse_stops_heartbeat_after_disconnect() -> None:
    """客户端断开时不再发心跳并清理记录流。"""
    records = _IdleRecords()
    stream = _stream_message_sse(
        records,
        request=_Request(disconnected=True),
        heartbeat_interval_seconds=0.01,
    )

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), timeout=0.5)

    assert records.closed is True
