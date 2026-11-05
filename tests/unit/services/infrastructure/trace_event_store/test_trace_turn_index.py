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
    GoalUpdatedEvent,
    GoalUpdatedPayload,
    JobCreatedEvent,
    JobCreatedPayload,
    StatusChangeEvent,
    StatusChangePayload,
)
from app.services.infrastructure.turn_history.trace_index import TraceTurnIndex
from app.services.infrastructure.turn_history.trace_index_storage import (
    TraceIndexStorage,
)


@pytest.mark.asyncio
async def test_indexed_trace_append_recovers_once_per_event(
    tmp_path: Path,
    trace_event_store_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_019c3e6c2a7a7208aa9f84e0f0478714"
    store, session_dir = trace_event_store_factory(session_id)
    thread_id = get_session_path_resolver(tmp_path).main_thread_id(session_id)
    recover = TraceIndexStorage.recover
    recoveries = 0

    def observe_recover(storage, *args, **kwargs):
        nonlocal recoveries
        recoveries += 1
        return recover(storage, *args, **kwargs)

    monkeypatch.setattr(TraceIndexStorage, "recover", observe_recover)
    for ordinal in range(3):
        event = JobCreatedEvent(
            event_id=f"evt_append_recovery_{ordinal}",
            job_id=f"job_append_recovery_{ordinal}",
            timestamp=datetime.now(UTC),
            payload=JobCreatedPayload(
                session_id=session_id, thread_id=thread_id,
                message=f"append-{ordinal}", agent_id="default",
            ),
        )
        recoveries = 0
        receipt = await store.append(session_id, event)
        traces = session_dir / "logs" / "traces"
        assert receipt.trace_end_offset == (traces / "events.jsonl").stat().st_size
        assert receipt.projected_event_offset == (traces / "messages.jsonl").stat().st_size
        assert recoveries == 1
        batch = store.read_turn_bootstrap_batch(session_id, max_events=128, max_bytes=65536)
        assert batch.event_cursor == event.event_id
        assert [entry.event.event_id for entry in batch.events] == [event.event_id]
    assert len(store.read_events(session_id)) == 3
    assert len(store.read_message_events(session_id)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["bootstrap", "recovery", "migration", "cursor", "threads"])
async def test_trace_read_loads_current_manifest_once(
    tmp_path: Path,
    trace_event_store_factory,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    session_id = "ses_019c3e6c2a7a7208aa9f84e0f0478713"
    store, session_dir = trace_event_store_factory(session_id)
    thread_id = get_session_path_resolver(tmp_path).main_thread_id(session_id)
    manifest_path = session_dir / "logs" / "traces" / "turn-events.index.json"
    read_text = Path.read_text
    manifest_reads = 0

    def observe_read(path, *args, **kwargs):
        nonlocal manifest_reads
        if path == manifest_path:
            manifest_reads += 1
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", observe_read)
    for ordinal in range(2):
        event = JobCreatedEvent(
            event_id=f"evt_manifest_read_{ordinal}",
            job_id=f"job_manifest_read_{ordinal}",
            timestamp=datetime.now(UTC),
            payload=JobCreatedPayload(
                session_id=session_id, thread_id=thread_id,
                message=f"current-{ordinal}", agent_id="default",
            ),
        )
        receipt = await store.append(session_id, event)
        manifest_reads = 0
        if operation == "bootstrap":
            batch = store.read_turn_bootstrap_batch(session_id, max_events=128, max_bytes=65536)
            assert [entry.event.event_id for entry in batch.events] == [event.event_id]
            assert batch.event_cursor == event.event_id
        elif operation == "recovery":
            batch = store.read_turn_recovery_batch(
                session_id, after_event_id=event.event_id, max_events=128, max_bytes=65536,
            )
            assert batch.complete and not batch.events
            assert batch.event_offset == receipt.projected_event_offset
        elif operation == "migration":
            snapshot = store.capture_turn_migration_snapshot(session_id)
            assert snapshot.event_cursor == event.event_id
            assert snapshot.projected_event_offset == receipt.projected_event_offset
        elif operation == "cursor":
            assert store._offset_after_event(
                session_id, store._trace_file(session_id), event.event_id,
            ) == receipt.trace_end_offset
        else:
            assert store.event_threads(session_id, [event]) == {event.event_id: thread_id}
        # 第二轮必须取得新提交，复用仅限一次受锁保护的调用，不能跨请求缓存。
        assert manifest_reads == 1


def test_trace_turn_prepare_serializes_source_event_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_019c3e6c2a7a7208aa9f84e0f0478712"
    event = JobCreatedEvent(
        event_id="evt_single_event_dump",
        job_id="job_single_event_dump",
        timestamp=datetime.now(UTC),
        payload=JobCreatedPayload(
            session_id=session_id,
            thread_id="thr_019c3e6c2a7a7208aa9f84e0f0478712",
            message="serialize once",
            agent_id="default",
        ),
    )
    serialized_size = len(event.model_dump_json().encode("utf-8")) + 1
    original_model_dump = JobCreatedEvent.model_dump
    model_dump_calls = 0

    def count_model_dump(self, *args, **kwargs):
        nonlocal model_dump_calls
        model_dump_calls += 1
        return original_model_dump(self, *args, **kwargs)

    monkeypatch.setattr(JobCreatedEvent, "model_dump", count_model_dump)
    index = TraceTurnIndex(tmp_path / "logs" / "traces")
    manifest, trace_start = index.recover_before_append()
    prepared = index.prepare(
        event,
        manifest=manifest,
        trace_start=trace_start,
        serialized_size=serialized_size,
        projects_turn=True,
    )

    assert prepared.entry.event_id == event.event_id
    assert model_dump_calls == 1


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
                thread_id=get_session_path_resolver(tmp_path).main_thread_id(
                    session_id
                ),
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
    assert (
        session_dir / "logs" / "traces" / "events.jsonl"
    ).stat().st_size > 2 * 1024 * 1024


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
                    thread_id=get_session_path_resolver(tmp_path).main_thread_id(
                        session_id
                    ),
                    message="recover",
                    agent_id="default",
                ),
            ),
        )
    monkeypatch.setattr(TraceTurnIndex, "commit", original_commit)

    events = store.read_trace_page(session_id, cursor=None, limit=10).events
    assert store.event_threads(session_id, events) == {
        "evt_index_recover": get_session_path_resolver(tmp_path).main_thread_id(
            session_id
        ),
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
                thread_id=get_session_path_resolver(tmp_path).main_thread_id(
                    session_id
                ),
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
@pytest.mark.parametrize("damage", ["line_end", "schema"])
async def test_trace_owner_validates_last_index_line_after_finding_owner(
    tmp_path: Path,
    trace_event_store_factory,
    damage: str,
) -> None:
    session_id = "ses_019c3e6c2a7a7208aa9f84e0f0478718"
    store, directory = trace_event_store_factory(session_id)
    event = JobCreatedEvent(
        event_id="evt_owner_before_damage", job_id="job_owner_before_damage",
        timestamp=datetime.now(UTC),
        payload=JobCreatedPayload(
            session_id=session_id,
            thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
            message="owner", agent_id="default",
        ),
    )
    await store.append(session_id, event)
    await store.append(session_id, AgentStartEvent(
        event_id="evt_damaged_last_index_line", job_id=event.job_id,
        timestamp=event.timestamp, payload=AgentStartPayload(agent_id="default"),
    ))
    index_path = directory / "logs/traces/turn-events.index.jsonl"
    raw = index_path.read_bytes()
    last_start = raw.rfind(b"\n", 0, len(raw) - 1) + 1
    damaged = (raw[:-1] + b" " if damage == "line_end" else
        raw[:last_start] + raw[last_start:].replace(b'"schema_version":1', b'"schema_version":9', 1))
    assert damaged != raw and len(damaged) == len(raw)
    index_path.write_bytes(damaged)
    with pytest.raises((RuntimeError, ValueError), match="已提交边界|schema_version"):
        store.event_threads(session_id, [event])


@pytest.mark.asyncio
async def test_trace_owner_rejects_missing_drifting_or_foreign_job_facts(
    tmp_path: Path,
    trace_event_store_factory,
) -> None:
    session_id = "ses_019c4169dad7706c86613137c1972acb"
    store, _ = trace_event_store_factory(session_id)
    thread_id = get_session_path_resolver(tmp_path).main_thread_id(session_id)
    now = datetime.now(UTC)
    orphan = AgentStartEvent(
        event_id="evt_orphan",
        job_id="job_orphan",
        timestamp=now,
        payload=AgentStartPayload(agent_id="default"),
    )
    await store.append(session_id, orphan)
    with pytest.raises(RuntimeError, match="缺少持久 Thread 归属"):
        store.event_threads(session_id, [orphan])

    def created(
        event_id: str, owner_session: str, owner_thread: str
    ) -> JobCreatedEvent:
        return JobCreatedEvent(
            event_id=event_id,
            job_id="job_drift",
            timestamp=now,
            payload=JobCreatedPayload(
                session_id=owner_session,
                thread_id=owner_thread,
                message="归属",
                agent_id="default",
            ),
        )

    first = created("evt_first_owner", session_id, thread_id)
    await store.append(session_id, first)
    assert store.event_threads(session_id, [first]) == {first.event_id: thread_id}
    drift = created(
        "evt_drift_owner", session_id, "thr_018f0000000070008000000000000001"
    )
    assert drift.payload.thread_id != thread_id
    await store.append(session_id, drift)
    with pytest.raises(RuntimeError, match="Thread 归属漂移"):
        store.event_threads(session_id, [first])

    foreign = created(
        "evt_foreign_owner", "ses_018f0000000070008000000000000001", thread_id
    )
    foreign.job_id = "job_foreign"
    await store.append(session_id, foreign)
    with pytest.raises(RuntimeError, match="归属与 Session 不一致"):
        store.event_threads(session_id, [foreign])


@pytest.mark.parametrize("updated", [False, True])
def test_goal_trace_owner_is_explicitly_main_without_job_binding(
    tmp_path: Path,
    trace_event_store_factory,
    updated: bool,
) -> None:
    session_id = "ses_019c4169dad7706c86613137c1972acb"
    store, _ = trace_event_store_factory(session_id)
    common = {"event_id": "evt_goal_main", "job_id": f"goal:{session_id}",
              "timestamp": datetime.now(UTC)}
    owner = {"session_id": session_id,
             "thread_id": get_session_path_resolver(tmp_path).main_thread_id(session_id)}
    event = (GoalUpdatedEvent(**common, payload=GoalUpdatedPayload(**owner, goal={}))
             if updated else GoalClearedEvent(**common, payload=GoalClearedPayload(**owner)))
    assert store.event_threads(session_id, [event]) == {
        event.event_id: get_session_path_resolver(tmp_path).main_thread_id(session_id),
    }
    for drift in ({"session_id": "ses_foreign"}, {"thread_id": "thr_child"}):
        wrong = event.model_copy(
            update={"payload": event.payload.model_copy(update=drift)}
        )
        with pytest.raises(ValueError, match="main Thread 不一致"):
            store.event_threads(session_id, [event, wrong])
