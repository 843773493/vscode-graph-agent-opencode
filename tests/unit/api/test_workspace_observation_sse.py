from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import workspace_observation as api
from app.schemas.internal_v2.workspace_observation import WorkspaceObservationRequest

SESSION_ID = "ses_019bef80066f7038ae8e88ccb2e5e2ea"
THREAD_ID = "thr_018f0000000070008000000000000001"


@pytest.fixture
def observation_sources(monkeypatch: pytest.MonkeyPatch):
    calls = []
    closed = set()
    queues = {kind: asyncio.Queue() for kind in ("activity", "trace", "files")}

    async def frames(kind: str) -> AsyncIterator[str]:
        try:
            while True:
                frame = await queues[kind].get()
                if isinstance(frame, Exception):
                    raise frame
                if frame is None:
                    return
                yield frame
        finally:
            closed.add(kind)

    def activity(service, *, after):
        calls.append(("activity", after))
        return frames("activity")

    async def trace(session_id, cursor, session_service, config_service, *, thread_id=None):
        calls.append(("trace", session_id, cursor, thread_id))
        if cursor == "expired":
            raise HTTPException(410, detail={"code": "trace_cursor_gone", "requested_cursor": cursor})
        return frames("trace")

    def files(payload, watch_service, workspace_service):
        calls.append(("files", payload.paths, payload.after_event_id))
        if payload.paths == ["forbidden"]:
            raise HTTPException(403, detail="路径禁止访问")
        return frames("files")

    monkeypatch.setattr(api, "_stream_session_activity_sse", activity)
    monkeypatch.setattr(api, "open_session_trace_sse", trace)
    monkeypatch.setattr(api, "open_workspace_file_sse", files)
    monkeypatch.setattr(api, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    return SimpleNamespace(calls=calls, closed=closed, queues=queues)


@pytest.fixture
def open_observations(observation_sources):
    async def open_stream(subscriptions):
        return await api.stream_workspace_observation(
            WorkspaceObservationRequest(subscriptions=subscriptions), "token",
            activity_service=object(), session_service=object(), config_service=object(),
            watch_service=object(), workspace_service=SimpleNamespace(
                file_operations=SimpleNamespace(store=SimpleNamespace(latest_event_id=lambda: 27)),
            ),
        )
    return open_stream


async def read_envelope(stream):
    frame = await asyncio.wait_for(anext(stream), 0.5)
    assert frame.startswith("event: observation\ndata: ")
    return json.loads(frame.split("data: ", 1)[1])


async def test_observations_keep_independent_cursors_and_original_frames(
    open_observations, observation_sources,
):
    response = await open_observations([
        {"subscription_id": "catalog", "kind": "session_activity", "activity_after": 8},
        {"subscription_id": "trace", "kind": "session_trace", "session_id": SESSION_ID, "thread_id": THREAD_ID, "trace_after": "tc1.old"},
        {"subscription_id": "files", "kind": "workspace_files", "paths": ["shortcut"]},
    ])
    stream = response.body_iterator
    try:
        ready = [await read_envelope(stream) for _ in range(3)]
        assert {item["subscription_id"] for item in ready} == {"catalog", "trace", "files"}
        assert next(item for item in ready if item["subscription_id"] == "files")["cursor"] == "27"
        assert observation_sources.calls == [("activity", 8), ("trace", SESSION_ID, "tc1.old", THREAD_ID), ("files", ["shortcut"], 27)]
        original = "id: tc1.new\nevent: trace\ndata: {\"text\":\"中文\"}\n\n"
        await observation_sources.queues["trace"].put(original)
        assert await read_envelope(stream) == {"subscription_id": "trace", "state": "frame", "frame": original}
        assert response.headers["Cache-Control"] == "no-cache"
        assert response.headers["X-Accel-Buffering"] == "no"
    finally:
        await stream.aclose()
    assert observation_sources.closed == {"activity", "trace", "files"}


@pytest.mark.parametrize("status, kind, fields", [
    (410, "session_trace", {"session_id": SESSION_ID, "trace_after": "expired"}),
    (403, "workspace_files", {"paths": ["forbidden"]}),
])
async def test_one_owner_failure_does_not_terminate_other_owner(
    open_observations, observation_sources, status, kind, fields,
):
    response = await open_observations([
        {"subscription_id": "failed", "kind": kind, **fields},
        {"subscription_id": "catalog", "kind": "session_activity", "activity_after": 12},
    ])
    stream = response.body_iterator
    try:
        initial = [await read_envelope(stream) for _ in range(2)]
        failed = next(item for item in initial if item["subscription_id"] == "failed")
        assert failed["state"] == "error" and failed["status"] == status
        assert json.loads(failed["detail_json"])
        original = "id: 13\nevent: session_activity\ndata: {}\n\n"
        await observation_sources.queues["activity"].put(original)
        assert (await read_envelope(stream))["frame"] == original
        await observation_sources.queues["activity"].put(None)
        assert await read_envelope(stream) == {"subscription_id": "catalog", "state": "closed"}
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), 0.5)
    finally:
        await stream.aclose()
    assert observation_sources.closed == {"activity"}


async def test_owner_runtime_error_is_visible_and_other_owner_keeps_streaming(
    open_observations, observation_sources,
):
    response = await open_observations([
        {"subscription_id": "files", "kind": "workspace_files", "files_after": 21},
        {"subscription_id": "catalog", "kind": "session_activity"},
    ])
    stream = response.body_iterator
    try:
        for _ in range(2):
            await read_envelope(stream)
        await observation_sources.queues["files"].put(RuntimeError("watcher 失败"))
        error = await read_envelope(stream)
        assert error["subscription_id"] == "files" and error["status"] == 500
        assert json.loads(error["detail_json"]) == {
            "owner": "workspace_observation", "stage": "stream", "type": "RuntimeError", "errno": None,
        }
        await observation_sources.queues["activity"].put(": ping\n\n")
        assert (await read_envelope(stream))["frame"] == ": ping\n\n"
    finally:
        await stream.aclose()
    assert observation_sources.closed == {"files", "activity"}


@pytest.mark.parametrize("subscriptions", [
    [],
    [{"subscription_id": "same", "kind": "session_activity"}] * 2,
    [{"subscription_id": "missing", "kind": "session_trace"}],
    [{"subscription_id": "mixed", "kind": "session_activity", "files_after": 2}],
    [{"subscription_id": "bad", "kind": "session_trace", "session_id": "invalid"}],
    [{"subscription_id": "bad", "kind": "session_trace", "session_id": SESSION_ID, "thread_id": "invalid"}],
    [{"subscription_id": "bad", "kind": "session_activity", "thread_id": THREAD_ID}],
])
def test_observation_request_rejects_ambiguous_or_invalid_scope(subscriptions):
    with pytest.raises(ValidationError):
        WorkspaceObservationRequest(subscriptions=subscriptions)
