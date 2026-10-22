from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.schemas.event import (
    AgentStartEvent,
    AgentStartPayload,
    JobCreatedEvent,
    JobCreatedPayload,
    TextDeltaEvent,
    TextDeltaPayload,
)
from app.services.infrastructure.trace_event_store import (
    TraceEventStore,
)
from app.services.infrastructure.turn_history.trace_index import TraceTurnIndex
from app.services.infrastructure.turn_history.trace_writer import TraceEventWriter


@pytest.mark.asyncio
@pytest.mark.parametrize("cutpoint", ["after_index", "after_message", "partial_trace"])
async def test_next_append_recovers_incomplete_trace_transaction(
    tmp_path: Path,
    trace_event_store_factory,
    monkeypatch: pytest.MonkeyPatch,
    cutpoint: str,
) -> None:
    # R18 catalog 模式适配：f-string 派生 ID 改显式映射（与 r18-replace.py
    # 同一 md5 canonical 函数，按逐参数完整字面量整串派生，确定性可复算）。
    cutpoint_session_ids = {
        "after_index": "ses_019c4d058e517547860a0209d86f2d88",
        "after_message": "ses_019b8ee6c05f7335852d29f0e1644730",
        "partial_trace": "ses_019c58987d97711d8c2af89d18195938",
    }
    session_id = cutpoint_session_ids[cutpoint]
    store, session_dir = trace_event_store_factory(session_id,
    )
    original_append = TraceEventWriter._append_bytes

    def crash_at_cutpoint(file: Path, payload: bytes, *, durable: bool) -> int:
        if cutpoint == "after_index" and file.name == "messages.jsonl":
            raise OSError("模拟 index 落盘后崩溃")
        if file.name == "events.jsonl" and cutpoint == "after_message":
            raise OSError("模拟 message 落盘后崩溃")
        if file.name == "events.jsonl" and cutpoint == "partial_trace":
            with file.open("ab") as stream:
                stream.write(payload[: max(1, len(payload) // 2)])
                stream.flush()
            raise OSError("模拟 trace 尾行部分写入后崩溃")
        return original_append(file, payload, durable=durable)

    monkeypatch.setattr(
        TraceEventWriter,
        "_append_bytes",
        staticmethod(crash_at_cutpoint),
    )
    with pytest.raises(OSError, match="模拟"):
        await store.append(
            session_id,
            JobCreatedEvent(
                event_id="evt_cutpoint_failed",
                job_id="job_cutpoint_failed",
                timestamp=datetime.now(UTC),
                payload=JobCreatedPayload(
                    session_id=session_id,
                    thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
                    message="failed",
                    agent_id="default",
                ),
            ),
        )
    monkeypatch.setattr(
        TraceEventWriter,
        "_append_bytes",
        staticmethod(original_append),
    )

    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_cutpoint_next",
            job_id="job_cutpoint_next",
            timestamp=datetime.now(UTC),
            payload=JobCreatedPayload(
                session_id=session_id,
                thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
                message="next",
                agent_id="default",
            ),
        ),
    )

    assert [event.event_id for event in store.read_events(session_id)] == [
        "evt_cutpoint_next"
    ]
    assert [event.event_id for event in store.read_message_events(session_id)] == [
        "evt_cutpoint_next"
    ]
    assert not (session_dir / "logs" / "traces" / "events.jsonl").read_bytes().startswith(
        b'{"event_id":"evt_cutpoint_failed"'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cutpoint", ["after_index", "after_message", "partial_trace"])
async def test_non_indexed_append_recovers_incomplete_trace_transaction(
    tmp_path: Path,
    trace_event_store_factory,
    monkeypatch: pytest.MonkeyPatch,
    cutpoint: str,
) -> None:
    session_id = {
        # R18 catalog 模式适配：f-string 派生 ID 改显式映射（同一 md5
        # canonical 函数，按逐参数完整字面量整串派生）。
        "after_index": "ses_019c6ecff472708a88bf3e1b106c320a",
        "after_message": "ses_019b8f4b17bf757fab809623549166a2",
        "partial_trace": "ses_019c445fd569703c83257812f26e7374",
    }[cutpoint]
    store, _ = trace_event_store_factory(session_id)
    now = datetime.now(UTC)
    await store.append(
        session_id,
        TextDeltaEvent(
            event_id="evt_preserved_delta",
            part_id="part_preserved_delta",
            job_id="job_preserved_delta",
            timestamp=now,
            payload=TextDeltaPayload(kind="markdown", text="keep"),
        ),
    )
    original_append = TraceEventWriter._append_bytes

    def crash_at_cutpoint(file: Path, payload: bytes, *, durable: bool) -> int:
        if cutpoint == "after_index" and file.name == "messages.jsonl":
            raise OSError("模拟 index 落盘后崩溃")
        if file.name == "events.jsonl" and cutpoint == "after_message":
            raise OSError("模拟 message 落盘后崩溃")
        if file.name == "events.jsonl" and cutpoint == "partial_trace":
            with file.open("ab") as stream:
                stream.write(payload[: max(1, len(payload) // 2)])
                stream.flush()
            raise OSError("模拟 trace 尾行部分写入后崩溃")
        return original_append(file, payload, durable=durable)

    monkeypatch.setattr(
        TraceEventWriter,
        "_append_bytes",
        staticmethod(crash_at_cutpoint),
    )
    with pytest.raises(OSError, match="模拟"):
        await store.append(
            session_id,
            JobCreatedEvent(
                event_id="evt_failed_semantic",
                job_id="job_failed_semantic",
                timestamp=now,
                payload=JobCreatedPayload(
                    session_id=session_id,
                    thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
                    message="failed",
                    agent_id="default",
                ),
            ),
        )
    monkeypatch.setattr(
        TraceEventWriter,
        "_append_bytes",
        staticmethod(original_append),
    )

    await store.append(
        session_id,
        AgentStartEvent(
            event_id="evt_non_indexed_next",
            job_id="job_non_indexed_next",
            timestamp=now,
            payload=AgentStartPayload(message="next", agent_id="default"),
        ),
    )
    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_indexed_after_non_indexed",
            job_id="job_indexed_after_non_indexed",
            timestamp=now,
            payload=JobCreatedPayload(
                session_id=session_id,
                thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
                message="indexed after recovery",
                agent_id="default",
            ),
        ),
    )

    assert [event.event_id for event in store.read_events(session_id)] == [
        "evt_preserved_delta",
        "evt_non_indexed_next",
        "evt_indexed_after_non_indexed",
    ]
    assert [event.event_id for event in store.read_message_events(session_id)] == [
        "evt_indexed_after_non_indexed"
    ]


@pytest.mark.asyncio
async def test_partial_non_indexed_tail_is_repaired_before_restart_append_and_stream(
    tmp_path: Path,
    trace_event_store_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_019bf0ef5dff77bd884cd91fe2c76a7e"
    store, _ = trace_event_store_factory(session_id)
    now = datetime.now(UTC)
    await store.append(
        session_id,
        TextDeltaEvent(
            event_id="evt_complete_delta",
            part_id="part_complete_delta",
            job_id="job_partial_non_indexed",
            timestamp=now,
            payload=TextDeltaPayload(kind="markdown", text="keep"),
        ),
    )
    original_append = TraceEventWriter._append_bytes

    def partial_non_indexed(file: Path, payload: bytes, *, durable: bool) -> int:
        if file.name == "events.jsonl":
            with file.open("ab") as stream:
                stream.write(payload[: max(1, len(payload) // 2)])
                stream.flush()
            raise OSError("模拟非索引事件半行崩溃")
        return original_append(file, payload, durable=durable)

    monkeypatch.setattr(
        TraceEventWriter,
        "_append_bytes",
        staticmethod(partial_non_indexed),
    )
    with pytest.raises(OSError, match="非索引事件半行"):
        await store.append(
            session_id,
            AgentStartEvent(
                event_id="evt_partial_agent_start",
                job_id="job_partial_non_indexed",
                timestamp=now,
                payload=AgentStartPayload(message="partial", agent_id="default"),
            ),
        )
    monkeypatch.setattr(
        TraceEventWriter,
        "_append_bytes",
        staticmethod(original_append),
    )

    restarted_store = TraceEventStore(sessions_dir=tmp_path)
    await restarted_store.append(
        session_id,
        AgentStartEvent(
            event_id="evt_after_restart",
            job_id="job_partial_non_indexed",
            timestamp=now,
            payload=AgentStartPayload(message="after", agent_id="default"),
        ),
    )
    stream = restarted_store.stream_events(session_id)
    first = await asyncio.wait_for(stream.asend(None), timeout=2.0)
    second = await asyncio.wait_for(stream.asend(None), timeout=2.0)
    await stream.aclose()

    assert [first.event.event_id, second.event.event_id] == [
        "evt_complete_delta",
        "evt_after_restart",
    ]


@pytest.mark.asyncio
async def test_partial_trace_tail_invading_committed_watermark_fails_fast(
    tmp_path: Path,
    trace_event_store_factory,
) -> None:
    session_id = "ses_019c7453ec467cb683e9176476e13f88"
    store, session_dir = trace_event_store_factory(session_id)
    now = datetime.now(UTC)
    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_committed",
            job_id="job_committed",
            timestamp=now,
            payload=JobCreatedPayload(
                session_id=session_id,
                thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
                message="committed",
                agent_id="default",
            ),
        ),
    )
    trace_file = session_dir / "logs" / "traces" / "events.jsonl"
    payload = trace_file.read_bytes()
    assert payload.endswith(b"\n")
    trace_file.write_bytes(payload[:-1] + b" ")

    with pytest.raises(RuntimeError, match="侵入已提交语义水位"):
        await store.append(
            session_id,
            AgentStartEvent(
                event_id="evt_rejected_after_intrusion",
                job_id="job_committed",
                timestamp=now,
                payload=AgentStartPayload(message="reject", agent_id="default"),
            ),
        )


@pytest.mark.asyncio
async def test_next_append_succeeds_when_caller_crashes_after_index_manifest(
    tmp_path: Path,
    trace_event_store_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_019c0a48b1467c2fa003a36834b4ed29"
    store, _ = trace_event_store_factory(session_id)
    original_commit = TraceTurnIndex.commit
    failed = False

    def commit_then_crash(self, prepared):
        nonlocal failed
        original_commit(self, prepared)
        if not failed:
            failed = True
            raise OSError("模拟 manifest 落盘后调用方崩溃")

    monkeypatch.setattr(TraceTurnIndex, "commit", commit_then_crash)
    with pytest.raises(OSError, match="manifest 落盘后"):
        await store.append(
            session_id,
            JobCreatedEvent(
                event_id="evt_manifest_committed",
                job_id="job_manifest_committed",
                timestamp=datetime.now(UTC),
                payload=JobCreatedPayload(
                    session_id=session_id,
                    thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
                    message="committed",
                    agent_id="default",
                ),
            ),
        )
    monkeypatch.setattr(TraceTurnIndex, "commit", original_commit)

    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_after_manifest",
            job_id="job_after_manifest",
            timestamp=datetime.now(UTC),
            payload=JobCreatedPayload(
                session_id=session_id,
                thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
                message="after",
                agent_id="default",
            ),
        ),
    )

    assert [event.event_id for event in store.read_events(session_id)] == [
        "evt_manifest_committed",
        "evt_after_manifest",
    ]


@pytest.mark.asyncio
async def test_partial_semantic_trace_recovery_preserves_prior_text_delta(
    tmp_path: Path,
    trace_event_store_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ses_019c0108d9ee7f1e828639a9763327a5"
    store, _ = trace_event_store_factory(session_id)
    now = datetime.now(UTC)
    await store.append(
        session_id,
        TextDeltaEvent(
            event_id="evt_preserved_delta",
            part_id="part_preserved_delta",
            job_id="job_preserved_delta",
            timestamp=now,
            payload=TextDeltaPayload(kind="markdown", text="keep"),
        ),
    )
    original_append = TraceEventWriter._append_bytes

    def partial_trace(file: Path, payload: bytes, *, durable: bool) -> int:
        if file.name == "events.jsonl":
            with file.open("ab") as stream:
                stream.write(payload[: max(1, len(payload) // 2)])
                stream.flush()
            raise OSError("模拟 delta 后语义事件 partial trace")
        return original_append(file, payload, durable=durable)

    monkeypatch.setattr(
        TraceEventWriter,
        "_append_bytes",
        staticmethod(partial_trace),
    )
    with pytest.raises(OSError, match="partial trace"):
        await store.append(
            session_id,
            JobCreatedEvent(
                event_id="evt_partial_semantic",
                job_id="job_partial_semantic",
                timestamp=now,
                payload=JobCreatedPayload(
                    session_id=session_id,
                    thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
                    message="partial",
                    agent_id="default",
                ),
            ),
        )
    monkeypatch.setattr(
        TraceEventWriter,
        "_append_bytes",
        staticmethod(original_append),
    )

    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_after_partial",
            job_id="job_after_partial",
            timestamp=now,
            payload=JobCreatedPayload(
                session_id=session_id,
                thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
                message="after",
                agent_id="default",
            ),
        ),
    )

    assert [event.event_id for event in store.read_events(session_id)] == [
        "evt_preserved_delta",
        "evt_after_partial",
    ]
