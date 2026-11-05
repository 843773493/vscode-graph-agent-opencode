"""原事件窗口、完整行引用和归属的有界查询；不把投影装具当作执行验收。"""

import os
from datetime import UTC, datetime

import pytest

from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_session_path_resolver
from app.schemas.event import (
    AgentStartEvent,
    AgentStartPayload,
    JobCreatedEvent,
    JobCreatedPayload,
)
from app.services.infrastructure.turn_history import trace_search
from app.services.infrastructure.turn_history.trace_index import TraceTurnIndex
from app.services.infrastructure.turn_history.trace_page import TRACE_PAGE_MAX_BYTES
from app.services.infrastructure.turn_history.trace_search import (
    TraceSearchBudget,
    TraceSearchError,
)


@pytest.fixture
async def records(trace_event_store_factory, tmp_path):
    session_id = create_prefixed_id("ses")
    store, directory = trace_event_store_factory(session_id)
    main = get_session_path_resolver(tmp_path).main_thread_id(session_id)
    child = create_prefixed_id("thr")
    for index, thread in enumerate((main, child)):
        await store.append(session_id, JobCreatedEvent(event_id=f"created-{index}", job_id=f"job-{index}",
            timestamp=datetime.now(UTC), payload=JobCreatedPayload(session_id=session_id, thread_id=thread,
                message="创建", agent_id="default")))
        await store.append(session_id, AgentStartEvent(event_id=f"start-{index}", job_id=f"job-{index}",
            timestamp=datetime.now(UTC), payload=AgentStartPayload(message="位置😀 needle😀\n下一行 needle😀", agent_id="default")))
    return store, session_id, directory / "logs/traces/events.jsonl", main, child


def search(store, session, query="needle😀", *, limit=1, position=None, budget=None):
    return store.search_session_events(session, query, limit=limit, position=position,
        budget=budget or TraceSearchBudget(check=lambda: None))


def test_reverse_pages_continue_within_record_and_keep_real_thread(records):
    store, session, _file, main, child = records
    position = None
    hits = []
    for _ in range(6):
        page = search(store, session, position=position)
        hits.extend(page.items)
        position = page.position
        if page.complete:
            break
        assert position is not None
    assert page.complete and len(hits) == 4
    assert [hit.reference.thread_id for hit in hits] == [child, child, main, main]
    assert [hit.reference.event_id for hit in hits] == ["start-1", "start-1", "start-0", "start-0"]
    assert hits[0].reference.start_codepoint < hits[1].reference.start_codepoint
    for hit in hits:
        reference = hit.reference
        located = store.locate(reference)
        assert located.record.event_id == reference.event_id and located.record.thread_id == reference.thread_id
        assert located.text[reference.start_codepoint:reference.end_codepoint] == "needle😀"
        preview_offset = reference.start_codepoint - hit.preview_start_codepoint
        assert hit.preview[preview_offset:preview_offset + len("needle😀")] == "needle😀"


@pytest.mark.parametrize("mixed_jobs", [False, True])
async def test_search_reuses_job_ownership_only_within_locked_request(records, monkeypatch, mixed_jobs):
    store, session, _file, main, child = records
    jobs = ["job-1", "job-0" if mixed_jobs else "job-1", "job-1"]
    for ordinal, job in enumerate(jobs):
        await store.append(session, AgentStartEvent(
            event_id=f"window-{ordinal}", job_id=job, timestamp=datetime.now(UTC),
            payload=AgentStartPayload(message="x" * 300000 + "window-needle", agent_id="default"),
        ))
    lookup = TraceTurnIndex.job_threads
    queries = []

    def observe(index, session_id, job_ids):
        if job_ids:
            queries.append(set(job_ids))
        return lookup(index, session_id, job_ids)

    monkeypatch.setattr(TraceTurnIndex, "job_threads", observe)
    page = search(store, session, "window-needle", limit=100)
    assert page.complete
    assert [hit.reference.event_id for hit in page.items] == ["window-2", "window-1", "window-0"]
    assert [hit.reference.thread_id for hit in page.items] == [child, main if mixed_jobs else child, child]
    assert queries == ([{"job-1"}, {"job-0"}] if mixed_jobs else [{"job-1"}])

    await store.append(session, JobCreatedEvent(
        event_id="new-owner-conflict", job_id="job-1", timestamp=datetime.now(UTC),
        payload=JobCreatedPayload(session_id=session, thread_id=main, message="conflict", agent_id="default"),
    ))
    with pytest.raises(RuntimeError, match="Thread 归属漂移"):
        search(store, session, "window-needle", limit=100)


async def test_append_invalidates_cursor_but_not_unchanged_target(records):
    store, session, _file, _main, child = records
    page = search(store, session)
    assert page.position is not None
    hit = page.items[0]
    await store.append(session, AgentStartEvent(event_id="newer", job_id="job-1", timestamp=datetime.now(UTC),
        payload=AgentStartPayload(message="新增", agent_id="default")))
    with pytest.raises(TraceSearchError) as caught:
        search(store, session, position=page.position)
    assert (caught.value.code, caught.value.reason) == ("stale_trace_search_cursor", "file_changed")
    assert store.locate(hit.reference).record.thread_id == child


def test_full_line_version_rejects_same_size_middle_mutation(records):
    store, session, file, _main, _child = records
    page = search(store, session)
    assert page.position is not None
    hit = page.items[0]
    original = file.stat()
    raw = file.read_bytes()
    file.write_bytes(raw.replace(b"needle", b"NEEDLE"))
    os.utime(file, ns=(original.st_atime_ns, original.st_mtime_ns))
    with pytest.raises(TraceSearchError) as caught:
        store.locate(hit.reference)
    assert (caught.value.code, caught.value.reason) == ("stale_trace_reference", "record_changed")
    with pytest.raises(TraceSearchError) as cursor_error:
        search(store, session, position=page.position)
    assert cursor_error.value.reason == "record_changed"


@pytest.mark.parametrize("change,reason", [("thread", "owner_mismatch"), ("event", "owner_mismatch"),
    ("position", "position_out_of_bounds"), ("delete", "file_deleted"), ("oversize", "record_size_limit")])
def test_locate_rejects_invalid_reference_without_substitution(records, change, reason):
    store, session, file, main, _child = records
    reference = search(store, session).items[0].reference
    if change == "thread":
        reference = reference.model_copy(update={"thread_id": main})
    elif change == "event":
        reference = reference.model_copy(update={"event_id": "another"})
    elif change == "position":
        reference = reference.model_copy(update={"end_codepoint": 100000})
    elif change == "oversize":
        reference = reference.model_copy(update={"line_end": reference.line_start + TRACE_PAGE_MAX_BYTES + 1})
    else:
        file.unlink()
    with pytest.raises(TraceSearchError) as caught:
        store.locate(reference)
    assert caught.value.code == "stale_trace_reference" and caught.value.reason == reason


def test_no_match_does_not_hash_or_resolve_threads(records, monkeypatch):
    store, session, _file, _main, _child = records
    def unexpected(*_args, **_kwargs):
        raise AssertionError("非命中不需要正文摘要或 Thread 查询")
    monkeypatch.setattr(trace_search.hashlib, "sha256", unexpected)
    monkeypatch.setattr(store, "_event_threads", unexpected)
    page = search(store, session, "不存在的独有词")
    assert page.complete and page.items == [] and page.position is None


async def test_scan_budget_resumes_older_events_without_omission(records, monkeypatch):
    store, session, _file, _main, _child = records
    for index in range(130):
        await store.append(session, AgentStartEvent(event_id=f"extra-{index}", job_id="job-1", timestamp=datetime.now(UTC),
            payload=AgentStartPayload(message="无命中", agent_id="default")))
    first_budget = TraceSearchBudget(check=lambda: None)
    first = search(store, session, budget=first_budget)
    assert first.limited_reason == "scan_limit" and first.position is not None
    assert first_budget.scanned_records == 128 and first_budget.scanned_bytes <= trace_search.MAX_SEARCH_BYTES
    second = search(store, session, limit=100, position=first.position)
    assert second.complete and len(second.items) == 4


async def test_window_byte_budget_and_oversize_are_distinct(records, monkeypatch):
    store, session, _file, _main, _child = records
    for index in range(3):
        await store.append(session, AgentStartEvent(event_id=f"large-{index}", job_id="job-1", timestamp=datetime.now(UTC),
            payload=AgentStartPayload(message="x" * 300000 + "large-needle", agent_id="default")))
    monkeypatch.setattr(trace_search, "MAX_SEARCH_BYTES", TRACE_PAGE_MAX_BYTES)
    position = None
    hits = []
    for _ in range(5):
        budget = TraceSearchBudget(check=lambda: None)
        page = search(store, session, "large-needle", limit=100, position=position, budget=budget)
        assert budget.scanned_bytes <= TRACE_PAGE_MAX_BYTES
        hits.extend(page.items)
        position = page.position
        if page.complete:
            break
        assert page.limited_reason == "byte_limit" and position is not None
    assert page.complete and [hit.reference.event_id for hit in hits] == ["large-2", "large-1", "large-0"]
    await store.append(session, AgentStartEvent(event_id="oversize", job_id="job-1", timestamp=datetime.now(UTC),
        payload=AgentStartPayload(message="x" * (TRACE_PAGE_MAX_BYTES + 1), agent_id="default")))
    page = search(store, session)
    assert not page.complete and page.limited_reason == "item_size_limit" and page.position is None


def test_cursor_rejects_non_line_and_body_boundaries(records):
    store, session, _file, _main, _child = records
    position = search(store, session).position
    assert position is not None
    for invalid in (position.model_copy(update={"before_offset": 1}), position.model_copy(update={"offset": 100000})):
        with pytest.raises(TraceSearchError) as caught:
            search(store, session, position=invalid)
        assert caught.value.code == "invalid_trace_search_cursor" and caught.value.reason == "position_out_of_bounds"


async def test_exact_window_sized_event_is_not_discarded_as_partial_prefix(records):
    store, session, _file, _main, _child = records
    event = AgentStartEvent(event_id="exact-window", job_id="job-1", timestamp=datetime.now(UTC),
        payload=AgentStartPayload(message="exact-needle", agent_id="default"))
    padding = TRACE_PAGE_MAX_BYTES - len(event.model_dump_json().encode()) - 1
    event = event.model_copy(update={"payload": event.payload.model_copy(update={"message": "exact-needle" + "x" * padding})})
    await store.append(session, event)
    assert len(store.read_trace_page(session, cursor=None, limit=1).records[0].line) == TRACE_PAGE_MAX_BYTES
    page = search(store, session, "exact-needle")
    assert len(page.items) == 1 and page.items[0].reference.event_id == "exact-window"
