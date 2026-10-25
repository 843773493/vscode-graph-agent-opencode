"""公共资源列表的跨领域预算；领域列表由显式异步替身隔离。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import resources
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A, WORKSPACE_ID


@pytest.fixture
def listing(monkeypatch: pytest.MonkeyPatch):
    deadlines: list[asyncio.Timeout] = []
    calls: list[tuple[str, dict[str, object]]] = []
    actions: dict[str, Callable[[], Awaitable[None]]] = {}
    real_timeout = asyncio.timeout

    def capture(seconds: float) -> asyncio.Timeout:
        assert seconds == 30
        deadline = real_timeout(seconds)
        deadlines.append(deadline)
        return deadline

    def spend(seconds: float) -> None:
        if deadlines:
            current = deadlines[-1]
            current.reschedule(current.when() - seconds)

    def owner(kind: str) -> SimpleNamespace:
        async def read(**filters: object) -> list[object]:
            calls.append((kind, filters))
            if kind in actions:
                await actions[kind]()
            return []
        return SimpleNamespace(workspace_id=WORKSPACE_ID, list_resources=read)

    terminal, browser = owner("terminal"), owner("browser")

    async def invoke(kind: str | None = None):
        return await resources.list_resources(
            kind=kind, session_id=SESSION_A, thread_id=THREAD_A,
            retention_scope="thread", source_session_id=SESSION_A, source_thread_id=THREAD_A,
            terminal_owner=terminal, browser_owner=browser, request_id="list-request", _="test",
        )

    monkeypatch.setattr(resources, "timeout", capture, raising=False)
    return SimpleNamespace(deadlines=deadlines, calls=calls, actions=actions, spend=spend, invoke=invoke)


async def test_two_owner_waits_share_the_original_budget_and_drain_the_current_wait(listing) -> None:
    drained = asyncio.Event()

    async def first() -> None:
        listing.spend(20)

    async def second() -> None:
        listing.spend(11)
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    listing.actions.update(terminal=first, browser=second)
    with pytest.raises(HTTPException) as failure:
        await asyncio.wait_for(listing.invoke(), 0.2)
    assert failure.value.status_code == 503
    assert isinstance(failure.value.__cause__, TimeoutError)
    assert drained.is_set()
    assert [kind for kind, _ in listing.calls] == ["terminal", "browser"]
    assert len(listing.deadlines) == 1 and listing.deadlines[0].expired()


@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_kind_filter_has_the_same_budget_and_never_reads_the_other_owner(listing, kind: str) -> None:
    async def blocked() -> None:
        listing.spend(31)
        await asyncio.Event().wait()

    listing.actions[kind] = blocked
    with pytest.raises(HTTPException) as failure:
        await asyncio.wait_for(listing.invoke(kind), 0.2)
    assert failure.value.status_code == 503
    assert [selected for selected, _ in listing.calls] == [kind]


async def test_success_keeps_query_filters_owner_order_and_request_identity(listing) -> None:
    response = await listing.invoke()
    assert response.request_id == "list-request"
    assert response.data.workspace_id == WORKSPACE_ID
    assert response.data.items == []
    assert listing.calls == [(kind, {
        "session_id": SESSION_A, "thread_id": THREAD_A, "retention_scope": "thread",
        "source_session_id": SESSION_A, "source_thread_id": THREAD_A,
    }) for kind in ("terminal", "browser")]


@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_original_owner_timeout_is_preserved_as_the_http_error_cause(listing, kind: str) -> None:
    original = TimeoutError("原传输期限")

    async def failed() -> None:
        raise original

    listing.actions[kind] = failed
    with pytest.raises(HTTPException) as failure:
        await listing.invoke()
    assert failure.value.status_code == 503
    assert failure.value.__cause__ is original
    assert not listing.deadlines[0].expired()


async def test_caller_cancel_drains_the_current_owner_without_starting_the_next(listing) -> None:
    entered, drained = asyncio.Event(), asyncio.Event()

    async def blocked() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    listing.actions["terminal"] = blocked
    pending = asyncio.create_task(listing.invoke())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        pending.cancel("原调用方取消")
        with pytest.raises(asyncio.CancelledError, match="原调用方取消"):
            await pending
        assert drained.is_set()
        assert [kind for kind, _ in listing.calls] == ["terminal"]
        assert not listing.deadlines[0].expired()
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
