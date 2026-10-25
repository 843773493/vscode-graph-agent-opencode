"""删除准备窗口的真实任务和调度锁等待，不构造业务持久记录。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack
from unittest.mock import Mock

import pytest

from app.abstractions.job_executor import JobExecutorProtocol
from app.core.job_event_bus import JobEventBus
from app.core.session_subtree_delete import SubtreeDeleteWaitTimeout
from app.services.business.job.service import JobService


@pytest.fixture
def deletion_jobs(
    job_service_factory: Callable[..., JobService],
    session_lifecycle_guard: Callable[[str], None],
) -> JobService:
    return job_service_factory(
        job_event_bus=JobEventBus(), job_executor=Mock(spec=JobExecutorProtocol),
        session_lifecycle_guard=session_lifecycle_guard,
    )


@pytest.fixture
async def advance_clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    loop = asyncio.get_running_loop()
    real_time = loop.time
    offset = 0.0

    def advance(seconds: float) -> None:
        nonlocal offset
        offset += seconds

    monkeypatch.setattr(loop, "time", lambda: real_time() + offset)
    return advance


@pytest.mark.parametrize("stage", ["dispatch", "preparation", "preparation-lock"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_delete_deadline_and_cancel_do_not_wait_for_dispatch_lock_or_cancel_preparation(
    deletion_jobs: JobService, advance_clock: Callable[[float], None],
    stage: str, cancel: bool,
) -> None:
    jobs = deletion_jobs
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def preparing() -> None:
        started.set()
        await release.wait()

    async def operation() -> str:
        calls.append("delete")
        return "deleted"

    preparation = asyncio.create_task(jobs.run_session_preparation("ses_owner", preparing))
    await asyncio.wait_for(started.wait(), 1)
    try:
        async with AsyncExitStack() as locks:
            if stage == "dispatch":
                await locks.enter_async_context(jobs._dispatch_lock)
            deleting = asyncio.create_task(jobs.run_session_delete_operation(
                "ses_owner", operation, deadline=asyncio.get_running_loop().time() + 30,
            ))
            try:
                await asyncio.sleep(0)
                if stage != "dispatch":
                    async with asyncio.timeout(1):
                        while "ses_owner" not in jobs._deleting_sessions:
                            await asyncio.sleep(0)
                    if stage == "preparation-lock":
                        await locks.enter_async_context(jobs._dispatch_lock)
                assert not deleting.done()
                if cancel:
                    deleting.cancel("显式取消删除等待")
                    with pytest.raises(asyncio.CancelledError, match="显式取消删除等待"):
                        await asyncio.wait_for(deleting, 1)
                else:
                    advance_clock(31)
                    phase = "job-dispatch" if stage == "dispatch" else "job-preparation"
                    with pytest.raises(SubtreeDeleteWaitTimeout, match=f"session_ids=ses_owner, phase={phase}"):
                        await asyncio.wait_for(asyncio.shield(deleting), 1)
                assert "ses_owner" not in jobs._deleting_sessions
                assert jobs._session_preparations == {"ses_owner": 1}
                assert not preparation.done() and calls == []
            finally:
                deleting.cancel()
                await asyncio.gather(deleting, return_exceptions=True)
        # 旧准备任务仍由原owner结算；旁系和本Session可继续正常准入。
        assert await jobs.run_session_preparation("ses_peer", lambda: asyncio.sleep(0, result="peer")) == "peer"
        release.set()
        await preparation
        assert jobs._session_preparations == {}
        assert await jobs.run_session_delete_operation(
            "ses_owner", operation, deadline=asyncio.get_running_loop().time() + 30,
        ) == "deleted"
        assert calls == ["delete"]
    finally:
        release.set()
        await asyncio.gather(preparation, return_exceptions=True)


async def test_overlapping_delete_rejection_keeps_original_owner_and_event_wakes_all_sessions(
    deletion_jobs: JobService,
) -> None:
    jobs = deletion_jobs
    releases = {sid: asyncio.Event() for sid in ["ses_first", "ses_peer"]}
    entered = {sid: asyncio.Event() for sid in releases}
    deleted: list[str] = []

    async def prepare(sid: str) -> None:
        entered[sid].set()
        await releases[sid].wait()

    async def remove(sid: str) -> None:
        deleted.append(sid)

    preparations = [asyncio.create_task(jobs.run_session_preparation(sid, lambda sid=sid: prepare(sid))) for sid in releases]
    await asyncio.gather(*(event.wait() for event in entered.values()))
    deletions = [asyncio.create_task(jobs.run_session_delete_operation(
        sid, lambda sid=sid: remove(sid), deadline=asyncio.get_running_loop().time() + 30,
    )) for sid in releases]
    try:
        async with asyncio.timeout(1):
            while jobs._deleting_sessions != set(releases):
                await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="会话删除已在进行"):
            await jobs.run_sessions_delete_operation(
                ["ses_new", "ses_first"], lambda: remove("unexpected"),
                deadline=asyncio.get_running_loop().time() + 30,
            )
        assert jobs._deleting_sessions == set(releases)
        releases["ses_peer"].set()
        await asyncio.wait_for(asyncio.shield(deletions[1]), 1)
        assert deleted == ["ses_peer"] and not deletions[0].done()
        releases["ses_first"].set()
        await asyncio.wait_for(asyncio.shield(deletions[0]), 1)
        assert deleted == ["ses_peer", "ses_first"]
    finally:
        for event in releases.values():
            event.set()
        for task in deletions:
            task.cancel()
        await asyncio.gather(*preparations, *deletions, return_exceptions=True)


async def test_delete_rejects_expired_budget_before_any_admission_or_side_effect(
    deletion_jobs: JobService,
) -> None:
    calls: list[str] = []

    async def operation() -> None:
        calls.append("unexpected")

    for sessions in [[], ["ses_owner"]]:
        with pytest.raises(SubtreeDeleteWaitTimeout, match="phase=job-dispatch"):
            await deletion_jobs.run_sessions_delete_operation(
                sessions, operation, deadline=asyncio.get_running_loop().time() - 1,
            )
    assert calls == [] and deletion_jobs._deleting_sessions == set()


async def test_delete_preserves_callback_timeout_identity(deletion_jobs: JobService) -> None:
    original = TimeoutError("原删除owner自己的超时")

    async def operation() -> None:
        raise original

    with pytest.raises(TimeoutError) as caught:
        await deletion_jobs.run_session_delete_operation(
            "ses_owner", operation, deadline=asyncio.get_running_loop().time() + 30,
        )
    assert caught.value is original and deletion_jobs._deleting_sessions == set()
