from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.schemas.event import (
    AgentStartEvent,
    AgentStartPayload,
)
from app.services.infrastructure.trace_event_store import (
    TraceCursorGoneError,
    TraceEventStore,
)
from app.services.infrastructure.turn_history.trace_page import (
    TracePageBudgetExceededError,
)


def test_store_lock_pools_are_bounded_and_stable(tmp_path: Path) -> None:
    """append/file 锁池对 session 数量必有固定上界，且同 session 恒定同一把锁。"""
    store = TraceEventStore(sessions_dir=tmp_path)

    for index in range(5000):
        store._append_lock(f"ses_store_{index}")
        store._file_lock(f"ses_store_{index}")

    # 上界恒定：锁数量不随历史会话数增长。
    assert len(store._append_locks) <= 64
    assert len(store._file_locks) <= 64
    # 同 session 恒定命中同一把锁（保住 append 与文件读写互斥语义）。
    assert store._append_lock("ses_shared") is store._append_lock("ses_shared")
    assert store._file_lock("ses_shared") is store._file_lock("ses_shared")


def test_condition_pool_is_bounded_and_stable(tmp_path: Path) -> None:
    """Condition 池对 session 数量必有固定上界，且同 session 恒定命中同一个。"""
    store = TraceEventStore(sessions_dir=tmp_path)

    for index in range(5000):
        store._condition_for(f"ses_cond_{index}")

    # 上界恒定：Condition 数量不随历史会话数增长。
    assert len(store._conditions) <= 64
    # 同 session 恒定命中同一个 Condition（保住 append 唤醒 reader 的语义）。
    assert store._condition_for("ses_shared") is store._condition_for("ses_shared")


def test_condition_wakes_same_session_waiter(tmp_path: Path) -> None:
    """分片后同 session 的 append 仍能唤醒等待中的 reader。"""

    async def run() -> bool:
        store = TraceEventStore(sessions_dir=tmp_path)
        condition = store._condition_for("ses_wake")
        woke = False

        async def waiter() -> None:
            nonlocal woke
            async with condition:
                await asyncio.wait_for(condition.wait(), timeout=2.0)
            woke = True

        task = asyncio.create_task(waiter())
        # 让 waiter 真正进入 condition.wait() 再通知。
        await asyncio.sleep(0.05)
        await store._notify("ses_wake")
        await asyncio.wait_for(task, timeout=2.0)
        return woke

    assert asyncio.run(run()) is True


def test_store_append_lock_serializes_same_session(tmp_path: Path) -> None:
    """分片后同一 session 的 append 临界区仍严格互斥（峰值恒为 1）。"""

    async def run() -> int:
        store = TraceEventStore(sessions_dir=tmp_path)
        lock = store._append_lock("ses_serial")
        concurrent = 0
        peak = 0

        async def worker() -> None:
            nonlocal concurrent, peak
            async with lock:
                concurrent += 1
                peak = max(peak, concurrent)
                await asyncio.sleep(0.01)
                concurrent -= 1

        await asyncio.gather(*(worker() for _ in range(20)))
        return peak

    assert asyncio.run(run()) == 1


def test_store_file_lock_serializes_same_session(tmp_path: Path) -> None:
    """分片后同一 session 的文件锁在跨线程下仍严格互斥（峰值恒为 1）。"""
    store = TraceEventStore(sessions_dir=tmp_path)
    lock = store._file_lock("ses_file_serial")
    concurrent = 0
    peak = 0
    counter_guard = threading.Lock()

    def worker() -> None:
        nonlocal concurrent, peak
        with lock:
            with counter_guard:
                concurrent += 1
                peak = max(peak, concurrent)
            time.sleep(0.005)
            with counter_guard:
                concurrent -= 1

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert peak == 1


@pytest.mark.asyncio
async def test_store_append_and_read(tmp_path: Path, trace_event_store_factory):
    session_id = "ses_019ba1bfed0773fc80049132b9745deb"
    store, _ = trace_event_store_factory(session_id)

    event = AgentStartEvent(
        event_id="evt_1",
        job_id="job_1",
        agent_id="default",
        timestamp=datetime.now(UTC),
        payload=AgentStartPayload(message="start", agent_id="default"),
    )
    await store.append(session_id, event)

    events = store.read_events(session_id)
    assert len(events) == 1
    assert events[0].event_id == "evt_1"
    assert events[0].type == "agent_start"


@pytest.mark.asyncio
async def test_store_stream_new_events(tmp_path: Path, trace_event_store_factory):
    session_id = "ses_019c6ab34d1574aa885c385ae9a2493e"
    store, _ = trace_event_store_factory(session_id)

    stream = store.stream_events(session_id)

    event = AgentStartEvent(
        event_id="evt_2",
        job_id="job_2",
        agent_id="default",
        timestamp=datetime.now(UTC),
        payload=AgentStartPayload(message="start", agent_id="default"),
    )
    await store.append(session_id, event)

    received = await asyncio.wait_for(stream.asend(None), timeout=2.0)
    assert received.event.event_id == "evt_2"


@pytest.mark.asyncio
async def test_store_reads_and_streams_after_event_cursor(
    tmp_path: Path,
    trace_event_store_factory,
):
    session_id = "ses_019ba02133ac7bb5857703d08511f93f"
    store, _ = trace_event_store_factory(session_id)
    now = datetime.now(UTC)

    for index in range(3):
        await store.append(
            session_id,
            AgentStartEvent(
                event_id=f"evt_{index}",
                job_id="job_cursor",
                agent_id="default",
                timestamp=now,
                payload=AgentStartPayload(message=f"start {index}", agent_id="default"),
            ),
        )

    assert [event.event_id for event in store.read_events(session_id, "evt_0")] == [
        "evt_1",
        "evt_2",
    ]

    cursor_stream = store.stream_events(session_id)
    await asyncio.wait_for(cursor_stream.asend(None), timeout=2.0)
    second_record = await asyncio.wait_for(cursor_stream.asend(None), timeout=2.0)
    await cursor_stream.aclose()
    stream = store.stream_events(session_id, second_record.cursor)
    received = await asyncio.wait_for(stream.asend(None), timeout=2.0)
    assert received.event.event_id == "evt_2"
    await stream.aclose()


@pytest.mark.asyncio
async def test_store_reads_only_latest_trace_tail(tmp_path: Path, trace_event_store_factory):
    store, _ = trace_event_store_factory("ses_011963ad10607f2b881c963a8e3cc48c")
    now = datetime.now(UTC)
    for index in range(12):
        await store.append(
            "ses_011963ad10607f2b881c963a8e3cc48c",
            AgentStartEvent(
                event_id=f"evt_tail_{index}",
                job_id="job_tail",
                agent_id="default",
                timestamp=now,
                payload=AgentStartPayload(
                    message=f"start {index}",
                    agent_id="default",
                ),
            ),
        )

    events = store.read_events("ses_011963ad10607f2b881c963a8e3cc48c", tail_limit=4)

    assert [event.event_id for event in events] == [
        "evt_tail_8",
        "evt_tail_9",
        "evt_tail_10",
        "evt_tail_11",
    ]


@pytest.mark.asyncio
async def test_store_reads_latest_event_cursor_without_replaying_trace(
    tmp_path: Path,
    trace_event_store_factory,
):
    session_id = "ses_019ba8c7e4b871bd818f02de4a5df326"
    store, _ = trace_event_store_factory(session_id)
    now = datetime.now(UTC)

    for index in range(3):
        await store.append(
            session_id,
            AgentStartEvent(
                event_id=f"evt_latest_{index}",
                job_id="job_latest_cursor",
                agent_id="default",
                timestamp=now,
                payload=AgentStartPayload(
                    message=f"start {index}",
                    agent_id="default",
                ),
            ),
        )

    cursor = store.latest_event_cursor(session_id)
    assert cursor is not None
    assert cursor.startswith("tc1.")
    store.ensure_cursor(session_id, cursor)


@pytest.mark.asyncio
async def test_trace_diagnostic_page_reads_tail_then_older_with_opaque_cursor(
    tmp_path: Path,
    trace_event_store_factory,
) -> None:
    session_id = "ses_019c4b4c932472b1832ecb149a6239f6"
    store, _ = trace_event_store_factory(session_id)
    now = datetime.now(UTC)
    for index in range(8):
        await store.append(
            session_id,
            AgentStartEvent(
                event_id=f"evt_page_{index}",
                job_id="job_trace_page",
                timestamp=now,
                payload=AgentStartPayload(message=f"page {index}", agent_id="default"),
            ),
        )

    latest = store.read_trace_page(session_id, cursor=None, limit=3)
    assert [event.event_id for event in latest.events] == [
        "evt_page_5",
        "evt_page_6",
        "evt_page_7",
    ]
    assert latest.has_more is True
    assert latest.next_cursor is not None
    assert latest.next_cursor.startswith("tp1.")
    assert "evt_page_5" not in latest.next_cursor

    older = store.read_trace_page(
        session_id,
        cursor=latest.next_cursor,
        limit=3,
    )
    assert [event.event_id for event in older.events] == [
        "evt_page_2",
        "evt_page_3",
        "evt_page_4",
    ]
    assert older.has_more is True
    assert older.next_cursor != latest.next_cursor


@pytest.mark.asyncio
async def test_trace_diagnostic_page_obeys_fixed_read_budget_without_read_text(
    tmp_path: Path,
    trace_event_store_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_019c6e58153776708053408b26e38db7"
    store, session_dir = trace_event_store_factory(session_id)
    now = datetime.now(UTC)
    for index in range(20):
        await store.append(
            session_id,
            AgentStartEvent(
                event_id=f"evt_budget_{index}",
                job_id="job_trace_budget",
                timestamp=now,
                payload=AgentStartPayload(message="x" * 64, agent_id="default"),
            ),
        )
    trace_file = session_dir / "logs" / "traces" / "events.jsonl"
    original_read_text = Path.read_text

    def reject_trace_read_text(path: Path, *args, **kwargs):
        if path == trace_file:
            raise AssertionError("Trace 诊断分页不得全量 read_text")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_trace_read_text)
    page = store.read_trace_page(
        session_id,
        cursor=None,
        limit=20,
        max_bytes=2048,
    )

    assert page.events
    assert page.bytes_read <= 2048
    assert page.has_more is True


@pytest.mark.asyncio
async def test_trace_diagnostic_page_stale_cursor_fails_fast(
    tmp_path: Path,
    trace_event_store_factory,
) -> None:
    session_id = "ses_019c035a206c74b98484fb851bbabb92"
    store, session_dir = trace_event_store_factory(session_id)
    now = datetime.now(UTC)
    for index in range(2):
        await store.append(
            session_id,
            AgentStartEvent(
                event_id=f"evt_stale_{index}",
                job_id="job_trace_stale",
                timestamp=now,
                payload=AgentStartPayload(message="stale", agent_id="default"),
            ),
        )
    page = store.read_trace_page(session_id, cursor=None, limit=1)
    assert page.next_cursor is not None
    trace_file = session_dir / "logs" / "traces" / "events.jsonl"
    trace_file.write_bytes(trace_file.read_bytes().replace(b"evt_stale_1", b"evt_stale_x"))

    with pytest.raises(TraceCursorGoneError):
        store.read_trace_page(session_id, cursor=page.next_cursor, limit=1)


@pytest.mark.asyncio
async def test_trace_diagnostic_page_rejects_event_larger_than_byte_budget(
    tmp_path: Path,
    trace_event_store_factory,
) -> None:
    session_id = "ses_019c75b1efa67bbe8c1075098c94cc9f"
    store, _ = trace_event_store_factory(session_id)
    await store.append(
        session_id,
        AgentStartEvent(
            event_id="evt_oversized",
            job_id="job_trace_oversized",
            timestamp=datetime.now(UTC),
            payload=AgentStartPayload(message="x" * 4096, agent_id="default"),
        ),
    )

    with pytest.raises(TracePageBudgetExceededError):
        store.read_trace_page(
            session_id,
            cursor=None,
            limit=1,
            max_bytes=1024,
        )


def test_store_rejects_missing_event_cursor(tmp_path: Path, trace_event_store_factory):
    session_id = "ses_019c560a7f7779a38dfdff7033fc2040"
    store, _ = trace_event_store_factory(session_id)

    with pytest.raises(TraceCursorGoneError, match="evt_missing"):
        store.ensure_cursor(session_id, "evt_missing")

    with pytest.raises(TraceCursorGoneError, match="evt_missing"):
        store.read_events(session_id, "evt_missing")
