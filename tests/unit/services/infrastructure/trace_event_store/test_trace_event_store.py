from __future__ import annotations

import asyncio
import runpy
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.schemas.event import (
    AgentStartEvent,
    AgentStartPayload,
    JobCreatedEvent,
    JobCreatedPayload,
)
from app.services.infrastructure.trace_event_store import (
    TraceCursorGoneError,
)
from app.services.infrastructure.turn_history.trace_page import (
    TracePageBudgetExceededError,
)


def test_offline_trace_upgrade_preserves_events_and_builds_thread_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.sqlite_state import SQLiteStateDatabase
    from app.services.infrastructure.turn_history.trace_database import (
        TraceDatabase,
        _migrations,
    )

    path = tmp_path / "trace.sqlite"
    session_id = "ses_019c5c7e3adc77a3b10ccaa504be3e74"
    raw = JobCreatedEvent(
        event_id="evt_trace_upgrade", job_id="job_trace_upgrade", timestamp=datetime.now(UTC),
        payload=JobCreatedPayload(session_id=session_id, thread_id="thread_trace_upgrade", message="保留", agent_id="default"),
    ).model_dump_json() + "\n"
    with (SQLiteStateDatabase(path=path, schema_version=1, migrations=_migrations()[:1]) as old,
          closing(old.connection()) as connection):
        connection.execute("INSERT INTO trace_events(start, raw) VALUES (?, ?)", (0, raw))

    with pytest.raises(RuntimeError, match="请停写后显式执行"), TraceDatabase(path).connection():
        pass
    monkeypatch.setattr("sys.argv", ["trace_database", str(path)])
    entry = "app.services.infrastructure.turn_history.trace_database"
    monkeypatch.delitem(sys.modules, entry)
    runpy.run_module(entry, run_name="__main__")

    with TraceDatabase(path).connection() as connection:
        assert connection is not None
        assert tuple(connection.execute("SELECT start, raw, thread_id FROM trace_events").fetchone()) == (
            0, raw, "thread_trace_upgrade",
        )
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2
    backups = list(tmp_path.glob("trace.sqlite.v1-*.bak"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as connection:
        assert connection.execute("SELECT start, raw FROM trace_events").fetchall() == [(0, raw)]
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 1
        assert "thread_id" not in {row[1] for row in connection.execute("PRAGMA table_info(trace_events)")}

    runpy.run_module(entry, run_name="__main__")
    assert list(tmp_path.glob("trace.sqlite.v1-*.bak")) == backups


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
async def test_store_stream_new_events(tmp_path: Path, trace_event_store_factory, monkeypatch):
    session_id = "ses_019c6ab34d1574aa885c385ae9a2493e"
    store, _ = trace_event_store_factory(session_id)

    import threading

    from app.core.sqlite_state import SQLiteStateDatabase

    initializing, release = threading.Event(), threading.Event()
    initialize = SQLiteStateDatabase._initialize

    def paused_initialize(database):
        initializing.set()
        assert release.wait(2)
        initialize(database)

    monkeypatch.setattr(SQLiteStateDatabase, "_initialize", paused_initialize)
    stream = store.stream_events(session_id)
    receiving = asyncio.create_task(anext(stream))

    event = AgentStartEvent(
        event_id="evt_2",
        job_id="job_2",
        agent_id="default",
        timestamp=datetime.now(UTC),
        payload=AgentStartPayload(message="start", agent_id="default"),
    )
    writing = asyncio.create_task(store.append(session_id, event))
    try:
        assert await asyncio.to_thread(initializing.wait, 2)
        assert store.read_trace_page(session_id, cursor=None, limit=1).events == []
    finally:
        release.set()
    await writing
    received = await asyncio.wait_for(receiving, timeout=2.0)
    assert received.event.event_id == "evt_2"
    await stream.aclose()


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
    store, _session_dir = trace_event_store_factory(session_id)
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
    store, _session_dir = trace_event_store_factory(session_id)
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
    from tests.support.trace_storage import inject_trace_record, trace_bytes
    path = store._database_path(session_id)
    raw = trace_bytes(path).splitlines()[-1].replace(b"evt_stale_1", b"evt_stale_x")
    inject_trace_record(path, raw.decode() + "\n", event_id="evt_stale_1")

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


@pytest.mark.asyncio
async def test_live_trace_orders_external_commits_and_recovers_after_overflow(trace_event_store_factory):
    from app.services.infrastructure.events.event_channel_service import (
        EventChannelOverflowError,
    )
    from app.services.infrastructure.trace_event_store import TraceEventStore

    session_id = "ses_019c5c7e3adc77a3b10ccaa504be3e74"
    store, _ = trace_event_store_factory(session_id)
    external = TraceEventStore(sessions_dir=store._path_resolver.sessions_root)
    thread_id = store._path_resolver.main_thread_id(session_id)
    for job_id, owner in [("job_live", thread_id), ("job_other", "other-thread")]:
        await store.append(session_id, JobCreatedEvent(
            event_id=f"owner_{job_id}", job_id=job_id, timestamp=datetime.now(UTC),
            payload=JobCreatedPayload(session_id=session_id, thread_id=owner, message="owner", agent_id="default"),
        ))
    owner_cursor = store.latest_event_cursor(session_id)

    def event(index):
        return AgentStartEvent(
            event_id=f"evt_live_{index}", job_id="job_live", timestamp=datetime.now(UTC),
            payload=AgentStartPayload(message=str(index), agent_id="default"),
        )

    await store.append(session_id, event(0))
    assert store._event_service.channel_names == ()
    stream = store.stream_events(session_id, owner_cursor, thread_id=thread_id)
    first = await anext(stream)
    receiving = asyncio.create_task(anext(stream))

    async def wait_for_subscription():
        while not store._event_service.channel_names:
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(wait_for_subscription(), 2)
        # 外部实例先提交，当前实例后提交；直接交付不能跨过前一条。
        await external.append(session_id, event(1))
        await store.append(session_id, event(2))
        second = await asyncio.wait_for(receiving, 2)
        third = await asyncio.wait_for(anext(stream), 2)
        assert [first.event.event_id, second.event.event_id, third.event.event_id] == [
            "evt_live_0", "evt_live_1", "evt_live_2",
        ]
        # 其它 Thread 的输出不占目标订阅队列。
        for index in range(65):
            await store.append(session_id, event(f"other_{index}").model_copy(update={"job_id": "job_other"}))
        assert store._event_service.channel_names
        for index in range(3, 68):
            await store.append(session_id, event(index))
        assert store._event_service.channel_names == ()
        with pytest.raises(EventChannelOverflowError):
            await anext(stream)
    finally:
        receiving.cancel()
        await asyncio.gather(receiving, return_exceptions=True)
        await stream.aclose()

    resumed = store.stream_events(session_id, third.cursor, thread_id=thread_id)
    try:
        recovered = [await asyncio.wait_for(anext(resumed), 2) for _ in range(65)]
        assert [record.event.event_id for record in recovered] == [f"evt_live_{i}" for i in range(3, 68)]
    finally:
        await resumed.aclose()
    assert store._event_service.channel_names == ()
