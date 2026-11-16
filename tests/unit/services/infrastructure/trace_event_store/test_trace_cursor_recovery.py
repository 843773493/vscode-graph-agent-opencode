from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.schemas.event import (
    AgentStartEvent,
    AgentStartPayload,
    JobCompletedEvent,
    JobCompletedPayload,
    JobCreatedEvent,
    JobCreatedPayload,
    MessageCreatedEvent,
    MessageCreatedPayload,
    TextDeltaEvent,
    TextDeltaPayload,
)
from app.services.infrastructure.session_trace.trace_cursor import (
    bind_trace_cursor,
    unbind_trace_cursor,
)
from app.services.infrastructure.trace_event_store import (
    TraceCursorGoneError,
)


@pytest.mark.parametrize("kind", ["page", "stream"])
def test_public_trace_cursor_binds_session_thread_and_purpose(kind: str) -> None:
    scope = {"session_id": "ses_owner", "thread_id": "thr_owner", "kind": kind}
    cursor = bind_trace_cursor("storage-cursor", **scope)
    assert unbind_trace_cursor(cursor, **scope) == "storage-cursor"
    for changed in [
        {"session_id": "ses_other"},
        {"thread_id": "thr_sibling"},
        {"thread_id": None},
        {"kind": "stream" if kind == "page" else "page"},
    ]:
        with pytest.raises(TraceCursorGoneError):
            unbind_trace_cursor(cursor, **{**scope, **changed})
    for invalid in ["tc1.legacy", "ts2.!", "ts2.W10", "ts2.//8"]:
        with pytest.raises(TraceCursorGoneError):
            unbind_trace_cursor(invalid, **scope)


@pytest.mark.asyncio
async def test_latest_semantic_cursor_uses_index_without_full_trace_scan(
    tmp_path: Path,
    trace_event_store_factory,
) -> None:
    session_id = "ses_019c30a276077c7589fb25e2fc24b2b9"
    store, _ = trace_event_store_factory(session_id)
    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_indexed_cursor",
            job_id="job_indexed_cursor",
            timestamp=datetime.now(UTC),
            payload=JobCreatedPayload(
                session_id=session_id,
                thread_id=get_session_path_resolver(tmp_path).main_thread_id(
                    session_id
                ),
                message="cursor",
                agent_id="default",
            ),
        ),
    )

    store.ensure_cursor(session_id, "evt_indexed_cursor")


@pytest.mark.asyncio
async def test_projected_cursor_streams_later_indexed_non_projected_event(
    tmp_path: Path,
    trace_event_store_factory,
) -> None:
    session_id = "ses_019c5c7e3adc77a3b10ccaa504be3e74"
    store, _ = trace_event_store_factory(session_id)
    now = datetime.now(UTC)
    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_projected_created",
            job_id="job_projected_cursor",
            timestamp=now,
            payload=JobCreatedPayload(
                session_id=session_id,
                thread_id=get_session_path_resolver(tmp_path).main_thread_id(
                    session_id
                ),
                message="cursor",
                agent_id="default",
            ),
        ),
    )
    await store.append(
        session_id,
        JobCompletedEvent(
            event_id="evt_projected_completed",
            job_id="job_projected_cursor",
            timestamp=now,
            payload=JobCompletedPayload(result="done"),
        ),
    )
    await store.append(
        session_id,
        MessageCreatedEvent(
            event_id="evt_assistant_message",
            job_id="job_projected_cursor",
            timestamp=now,
            payload=MessageCreatedPayload(
                message_id="msg_assistant",
                session_id=session_id,
                thread_id="thr_00000000400070008000000000000002",
                role="assistant",
                content="done",
                created_at=now,
            ),
        ),
    )

    stream = store.stream_events(session_id, "evt_projected_completed")
    received = await asyncio.wait_for(stream.asend(None), timeout=2.0)
    await stream.aclose()

    assert received.event.event_id == "evt_assistant_message"


@pytest.mark.asyncio
@pytest.mark.parametrize("text_size", [1, 400, 4096])
async def test_text_delta_transport_cursor_resumes_without_trace_scan(
    text_size: int,
    tmp_path: Path,
    trace_event_store_factory,
) -> None:
    session_id = "ses_019c300213fe72fb8a361a7a5302067d"
    store, _ = trace_event_store_factory(session_id)
    now = datetime.now(UTC)
    await store.append(
        session_id,
        JobCreatedEvent(
            event_id="evt_delta_created",
            job_id="job_delta_cursor",
            timestamp=now,
            payload=JobCreatedPayload(
                session_id=session_id,
                thread_id=get_session_path_resolver(tmp_path).main_thread_id(
                    session_id
                ),
                message="delta",
                agent_id="default",
            ),
        ),
    )
    for index in range(2):
        await store.append(
            session_id,
            TextDeltaEvent(
                event_id=f"evt_delta_{index}",
                part_id="part_delta_cursor",
                job_id="job_delta_cursor",
                timestamp=now,
                payload=TextDeltaPayload(kind="markdown", text=str(index) * text_size),
            ),
        )

    first_stream = store.stream_events(session_id, "evt_delta_created")
    first_delta = await asyncio.wait_for(first_stream.asend(None), timeout=2.0)
    await first_stream.aclose()
    assert first_delta.event.event_id == "evt_delta_0"

    store.ensure_cursor(session_id, first_delta.cursor)
    resumed = store.stream_events(session_id, first_delta.cursor)
    second_delta = await asyncio.wait_for(resumed.asend(None), timeout=2.0)
    await resumed.aclose()

    assert second_delta.event.event_id == "evt_delta_1"


@pytest.mark.asyncio
async def test_unindexed_raw_event_id_is_rejected_without_full_trace_scan(
    tmp_path: Path,
    trace_event_store_factory,
) -> None:
    session_id = "ses_019b973e84bf7e0f84978ddbb9456411"
    store, _ = trace_event_store_factory(session_id)
    await store.append(
        session_id,
        AgentStartEvent(
            event_id="evt_raw_unindexed",
            job_id="job_raw_unindexed",
            timestamp=datetime.now(UTC),
            payload=AgentStartPayload(message="start", agent_id="default"),
        ),
    )
    with pytest.raises(TraceCursorGoneError, match="evt_raw_unindexed"):
        store.ensure_cursor(session_id, "evt_raw_unindexed")
