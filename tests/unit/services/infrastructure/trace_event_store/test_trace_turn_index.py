from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.schemas.event import (
    AgentStartEvent,
    AgentStartPayload,
    GoalClearedEvent,
    GoalClearedPayload,
    JobCreatedEvent,
    JobCreatedPayload,
    StatusChangeEvent,
    StatusChangePayload,
)
from app.services.infrastructure.turn_history.trace_index import TraceTurnIndex


@pytest.mark.asyncio
async def test_turn_migration_snapshot_uses_immutable_message_boundary(
    tmp_path: Path,
    trace_event_store_factory,
) -> None:
    session_id = "ses_019b818b88cd72e4bd9346c3b7ec9780"
    store, _ = trace_event_store_factory(session_id)
    first = JobCreatedEvent(
        event_id="evt_snapshot_1",
        job_id="job_snapshot_1",
        timestamp=datetime.now(UTC),
        payload=JobCreatedPayload(
            session_id=session_id,
            thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
            message="first",
            agent_id="default",
        ),
    )
    second = JobCreatedEvent(
        event_id="evt_snapshot_2",
        job_id="job_snapshot_2",
        timestamp=datetime.now(UTC),
        payload=JobCreatedPayload(
            session_id=session_id,
            thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
            message="second",
            agent_id="default",
        ),
    )
    await store.append(session_id, first)
    snapshot = store.capture_turn_migration_snapshot(session_id)
    await store.append(session_id, second)

    migrated = list(
        store.iter_message_events(
            session_id,
            before_offset=snapshot.message_trace_size,
        )
    )

    assert snapshot.event_cursor == first.event_id
    assert [event.event_id for event in migrated] == [first.event_id]
    assert [event.event_id for event in store.iter_message_events(session_id)] == [
        first.event_id,
        second.event_id,
    ]


@pytest.mark.asyncio
async def test_turn_bootstrap_index_keeps_job_start_beyond_128_events(
    tmp_path: Path,
    trace_event_store_factory,
) -> None:
    session_id = "ses_019c32ce459a72058d2ee761765cd38b"
    store, _ = trace_event_store_factory(session_id)
    now = datetime.now(UTC)
    created = JobCreatedEvent(
        event_id="evt_long_created",
        job_id="job_long",
        timestamp=now,
        payload=JobCreatedPayload(
            session_id=session_id,
            thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
            message="long",
            agent_id="default",
        ),
    )
    await store.append(session_id, created)
    for index in range(140):
        await store.append(
            session_id,
            StatusChangeEvent(
                event_id=f"evt_long_status_{index:03d}",
                job_id="job_long",
                timestamp=now,
                payload=StatusChangePayload(
                    status="running",
                    reason=f"step-{index}",
                    session_id=session_id,
                ),
            ),
        )

    batch = store.read_turn_bootstrap_batch(
        session_id,
        max_events=128,
        max_bytes=256 * 1024,
    )

    assert batch.events[0].event.event_id == created.event_id
    assert batch.events[-1].event.event_id == "evt_long_status_139"
    assert len(batch.events) <= 128
    assert batch.has_older_events is True


@pytest.mark.asyncio
async def test_turn_bootstrap_uses_compact_index_without_parsing_huge_trace_line(
    tmp_path: Path,
    trace_event_store_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_019b9cc5cb0d71ce80686388728ad77e"
    store, session_dir = trace_event_store_factory(session_id)
    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_huge_indexed",
            job_id="job_huge_indexed",
            timestamp=datetime.now(UTC),
            payload=JobCreatedPayload(
                session_id=session_id,
                thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
                message="x" * (2 * 1024 * 1024),
                agent_id="default",
            ),
        ),
    )

    def reject_full_event_read(*args, **kwargs):
        raise AssertionError("bootstrap 不得读取完整 Trace 大行")

    monkeypatch.setattr(TraceTurnIndex, "_read_source_event", reject_full_event_read)
    batch = store.read_turn_bootstrap_batch(
        session_id,
        max_events=128,
        max_bytes=64 * 1024,
    )

    assert len(batch.events) == 1
    assert len(batch.events[0].event.payload.message) <= 2048
    assert batch.has_older_events is True
    assert (session_dir / "logs" / "traces" / "events.jsonl").stat().st_size > 2 * 1024 * 1024


def test_turn_bootstrap_fails_closed_when_manifest_is_missing(
    tmp_path: Path,
    trace_event_store_factory,
) -> None:
    session_id = "ses_019bd176f3c57a58829b024478fe8773"
    store, session_dir = trace_event_store_factory(session_id)
    traces = session_dir / "logs" / "traces"
    traces.mkdir(parents=True)
    (traces / "messages.jsonl").write_bytes(b'{"event_id":"unindexed"}\n')

    with pytest.raises(RuntimeError) as error:
        store.read_turn_bootstrap_batch(
            session_id,
            max_events=128,
            max_bytes=64 * 1024,
        )

    assert "Trace Turn bootstrap 阶段缺少索引 manifest" in str(error.value)
    assert str(traces / "turn-events.index.json") in str(error.value)


@pytest.mark.asyncio
async def test_turn_index_recovers_commit_after_trace_files_were_flushed(
    tmp_path: Path,
    trace_event_store_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_019c4169dad7706c86613137c1972acb"
    store, _ = trace_event_store_factory(session_id)
    original_commit = TraceTurnIndex.commit

    def fail_commit(self, prepared):
        raise OSError("模拟 index manifest 提交前崩溃")

    monkeypatch.setattr(TraceTurnIndex, "commit", fail_commit)
    with pytest.raises(OSError, match="提交前崩溃"):
        await store.append(
            session_id,
            JobCreatedEvent(
                event_id="evt_index_recover",
                job_id="job_index_recover",
                timestamp=datetime.now(UTC),
                payload=JobCreatedPayload(
                    session_id=session_id,
                    thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
                    message="recover",
                    agent_id="default",
                ),
            ),
        )
    monkeypatch.setattr(TraceTurnIndex, "commit", original_commit)

    events = store.read_trace_page(session_id, cursor=None, limit=10).events
    assert store.event_threads(session_id, events) == {
        "evt_index_recover": get_session_path_resolver(tmp_path).main_thread_id(session_id),
    }

    batch = store.read_turn_bootstrap_batch(
        session_id,
        max_events=128,
        max_bytes=64 * 1024,
    )
    assert [item.event.event_id for item in batch.events] == ["evt_index_recover"]

    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_after_index_recover",
            job_id="job_after_index_recover",
            timestamp=datetime.now(UTC),
            payload=JobCreatedPayload(
                session_id=session_id,
                thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
                message="after recover",
                agent_id="default",
            ),
        ),
    )
    recovered = store.read_turn_bootstrap_batch(
        session_id,
        max_events=128,
        max_bytes=64 * 1024,
    )
    assert recovered.events[-1].event.event_id == "evt_after_index_recover"


@pytest.mark.asyncio
async def test_trace_owner_rejects_missing_drifting_or_foreign_job_facts(
    tmp_path: Path, trace_event_store_factory,
) -> None:
    session_id = "ses_019c4169dad7706c86613137c1972acb"
    store, _ = trace_event_store_factory(session_id)
    thread_id = get_session_path_resolver(tmp_path).main_thread_id(session_id)
    now = datetime.now(UTC)
    orphan = AgentStartEvent(
        event_id="evt_orphan", job_id="job_orphan", timestamp=now,
        payload=AgentStartPayload(agent_id="default"),
    )
    await store.append(session_id, orphan)
    with pytest.raises(RuntimeError, match="缺少持久 Thread 归属"):
        store.event_threads(session_id, [orphan])

    def created(event_id: str, owner_session: str, owner_thread: str) -> JobCreatedEvent:
        return JobCreatedEvent(
            event_id=event_id, job_id="job_drift", timestamp=now,
            payload=JobCreatedPayload(
                session_id=owner_session, thread_id=owner_thread,
                message="归属", agent_id="default",
            ),
        )

    first = created("evt_first_owner", session_id, thread_id)
    await store.append(session_id, first)
    assert store.event_threads(session_id, [first]) == {first.event_id: thread_id}
    drift = created("evt_drift_owner", session_id, "thr_018f0000000070008000000000000001")
    assert drift.payload.thread_id != thread_id
    await store.append(session_id, drift)
    with pytest.raises(RuntimeError, match="Thread 归属漂移"):
        store.event_threads(session_id, [first])

    foreign = created("evt_foreign_owner", "ses_018f0000000070008000000000000001", thread_id)
    foreign.job_id = "job_foreign"
    await store.append(session_id, foreign)
    with pytest.raises(RuntimeError, match="归属与 Session 不一致"):
        store.event_threads(session_id, [foreign])


def test_goal_trace_owner_is_explicitly_main_without_job_binding(
    tmp_path: Path, trace_event_store_factory,
) -> None:
    session_id = "ses_019c4169dad7706c86613137c1972acb"
    store, _ = trace_event_store_factory(session_id)
    event = GoalClearedEvent(
        event_id="evt_goal_main", job_id=f"goal:{session_id}", timestamp=datetime.now(UTC),
        payload=GoalClearedPayload(session_id=session_id),
    )
    assert store.event_threads(session_id, [event]) == {
        event.event_id: get_session_path_resolver(tmp_path).main_thread_id(session_id),
    }
