"""真实事件提交复用未改写分支，同时保留旧快照、失败与公开读取的隔离。"""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from app.services.infrastructure.message_stream_store import MessageStreamError
from tests.unit.services.infrastructure.test_message_stream_store import (
    MessageStreamStoreCase,
    message_stream_store as message_stream_store,  # noqa: PLC0414 - 复用按当前测试文件隔离的 fixture
)


@pytest.mark.asyncio
async def test_delta_reuses_untouched_branches_and_preserves_published_snapshots(
    message_stream_store: MessageStreamStoreCase,
) -> None:
    store, resolver, session_id = message_stream_store
    writer = await store.open(
        session_id=session_id, thread_id=resolver.main_thread_id(session_id), turn_id="copy-cost",
    )
    await writer.commit("tool_call", {
        "tool_call_id": "known-call", "arguments": {"parts": ["original"]},
    })
    for block_id in ("first", "second"):
        await writer.commit("block.delta", {
            "block_id": block_id, "operation": "item_upsert",
            "item": {"id": "item", "nested": {"parts": [block_id]}},
        })
    before = store._states[writer.turn_stream_id]
    original = deepcopy(before)
    subscription = await store.subscribe(writer.turn_stream_id)
    try:
        await writer.commit("block.delta", {
            "block_id": "first", "operation": "item_patch",
            "item": {"id": "item", "new_field": "changed"},
        })
        record = await asyncio.wait_for(subscription.get(), timeout=1)
        after = store._states[writer.turn_stream_id]
        assert record.checkpoint is after
        assert before == original
        # 高频 delta 的分配只随目标块增长，不递归复制其余模型、工具和正文。
        assert after["tool_calls"] is before["tool_calls"]
        assert after["blocks"][1] is before["blocks"][1]
        assert after["blocks"][0] is not before["blocks"][0]
        assert after["blocks"][0]["items"][0]["new_field"] == "changed"
        frozen = deepcopy(record.checkpoint)
        await writer.commit("block.delta", {
            "block_id": "second", "operation": "append", "text": "next",
        })
        assert record.checkpoint == frozen
        snapshot = await writer.snapshot()
        snapshot["payload"]["tool_calls"][0]["arguments"]["parts"].append("caller")
        current = await store.get_state(writer.turn_stream_id)
        assert current["tool_calls"][0]["arguments"]["parts"] == ["original"]
    finally:
        await store.unsubscribe(subscription)


@pytest.mark.asyncio
async def test_rejected_delta_keeps_cached_and_durable_state_unchanged(
    message_stream_store: MessageStreamStoreCase,
) -> None:
    store, resolver, session_id = message_stream_store
    writer = await store.open(
        session_id=session_id, thread_id=resolver.main_thread_id(session_id), turn_id="copy-failure",
    )
    await writer.commit("block.delta", {
        "block_id": "first", "operation": "append", "text": "original",
    })
    before = store._states[writer.turn_stream_id]
    original = deepcopy(before)
    events = await store.list_events(session_id=session_id, turn_stream_id=writer.turn_stream_id)
    with pytest.raises(MessageStreamError, match="结构化 block.delta 缺少 item"):
        await writer.commit("block.delta", {
            "block_id": "first", "operation": "item_patch", "item": None,
        })
    assert store._states[writer.turn_stream_id] is before
    assert before == original
    assert await store.list_events(session_id=session_id, turn_stream_id=writer.turn_stream_id) == events
