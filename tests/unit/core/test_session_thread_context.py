from __future__ import annotations

import asyncio

import pytest

from app.core.session_thread_context import (
    current_session_thread_owner,
    require_current_session_thread_owner,
    session_thread_owner_scope,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner


def test_scope_restores_previous_owner_after_nested_exit() -> None:
    outer = MutationIntentOwner(session_id="ses_outer", thread_id="thr_outer")
    inner = MutationIntentOwner(session_id="ses_inner", thread_id="thr_inner")

    with session_thread_owner_scope(outer, langgraph_thread_id="thr_outer"):
        with session_thread_owner_scope(inner, langgraph_thread_id="thr_inner"):
            assert require_current_session_thread_owner(
                langgraph_thread_id="thr_inner"
            ) == inner
        assert current_session_thread_owner(langgraph_thread_id="thr_outer") == outer

    assert current_session_thread_owner() is None


def test_scope_rejects_conflicting_langgraph_thread_without_binding() -> None:
    owner = MutationIntentOwner(session_id="ses_a", thread_id="thr_a")

    with (
        pytest.raises(ValueError, match="冲突"),
        session_thread_owner_scope(owner, langgraph_thread_id="thr_b"),
    ):
        pytest.fail("冲突 owner 不得进入 scope")

    assert current_session_thread_owner() is None


@pytest.mark.asyncio
async def test_scope_propagates_to_task_and_to_thread_without_cross_task_leak() -> None:
    owner_a = MutationIntentOwner(session_id="ses_a", thread_id="thr_a")
    owner_b = MutationIntentOwner(session_id="ses_b", thread_id="thr_b")
    release = asyncio.Event()

    async def read_after_release() -> MutationIntentOwner:
        await release.wait()
        return require_current_session_thread_owner(langgraph_thread_id="thr_a")

    with session_thread_owner_scope(owner_a, langgraph_thread_id="thr_a"):
        child = asyncio.create_task(read_after_release())
        worker_owner = await asyncio.to_thread(
            require_current_session_thread_owner,
            langgraph_thread_id="thr_a",
        )
    with session_thread_owner_scope(owner_b, langgraph_thread_id="thr_b"):
        release.set()
        child_owner = await child
        assert require_current_session_thread_owner(
            langgraph_thread_id="thr_b"
        ) == owner_b

    assert worker_owner == owner_a
    assert child_owner == owner_a
    assert current_session_thread_owner() is None
