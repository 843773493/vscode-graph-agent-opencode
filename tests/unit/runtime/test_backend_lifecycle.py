"""Backend关闭唯一owner、全批等待预算及成功步骤不重发。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI

from app.main import BackendLifespan


@dataclass
class Container:
    workspace_id: str


@dataclass
class Workspace:
    container: Container
    trace: list[str]
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    failures: list[BaseException] = field(default_factory=list)
    attempts: int = 0
    closed: bool = False
    pending_close_stage: str = "writer"

    async def close(self) -> None:
        if self.closed:
            return
        self.attempts += 1
        self.trace.append(self.container.workspace_id)
        self.started.set()
        await self.release.wait()
        if self.failures:
            raise self.failures.pop(0)
        self.closed = True


@dataclass
class Registry:
    closes: int = 0

    def close(self) -> None:
        self.closes += 1


@dataclass
class Stream:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    failures: list[BaseException] = field(default_factory=list)
    closes: int = 0

    async def aclose(self) -> None:
        self.closes += 1
        self.started.set()
        await self.release.wait()
        if self.failures:
            raise self.failures.pop(0)


@dataclass
class Case:
    backend: BackendLifespan
    registry: Registry
    stream: Stream
    workspaces: list[Workspace]
    deadlines: list[asyncio.Timeout]
    released_access: list[bool]
    application: FastAPI


@pytest.fixture
async def case(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Case]:
    application = FastAPI()
    registry, stream = Registry(), Stream()
    stream.release.set()
    backend = BackendLifespan(stream, application)
    backend.registry = registry
    trace: list[str] = []
    workspaces = [Workspace(Container(name), trace) for name in ("peer", "blocked")]
    backend.workspaces.extend(workspaces)
    application.state.workspace_lifespan = backend
    application.state.workspace_container_registry = registry
    released_access: list[bool] = []
    backend._access.callback(lambda: released_access.append(True))
    deadlines: list[asyncio.Timeout] = []

    def capture(seconds: float) -> asyncio.Timeout:
        assert seconds == 30
        context = asyncio.timeout(seconds)
        deadlines.append(context)
        return context

    monkeypatch.setattr(inspect.getmodule(BackendLifespan), "timeout", capture, raising=False)
    try:
        yield Case(backend, registry, stream, workspaces, deadlines, released_access, application)
    finally:
        for workspace in workspaces:
            workspace.release.set()
            workspace.failures.clear()
        stream.release.set()
        stream.failures.clear()
        owner = getattr(backend, "_close_owner", None)
        if owner is not None:
            await asyncio.gather(owner, return_exceptions=True)
        await backend.close()


async def test_whole_backend_budget_keeps_one_owner_and_independent_mounts(case: Case) -> None:
    peer, blocked = case.workspaces
    peer.release.set()
    waiting = asyncio.create_task(case.backend.close())
    try:
        await asyncio.wait_for(blocked.started.wait(), 2)
        await asyncio.sleep(0)
        assert peer.closed, "一个挂载挂起不能阻止其它挂载完成原关闭链"
        assert peer.trace == ["blocked", "peer"]
        assert len(case.deadlines) == 1
        owner = case.backend._close_owner
        case.deadlines[0].reschedule(asyncio.get_running_loop().time())
        with pytest.raises(TimeoutError, match="stage=workspaces.*blocked:writer"):
            await asyncio.wait_for(waiting, 2)
        assert owner is not None and not owner.done()
        assert case.registry.closes == 0 and case.released_access == []
        assert case.application.state.workspace_container_registry is case.registry
        assert case.application.state.workspace_lifespan is case.backend
        retry = asyncio.create_task(case.backend.close())
        await asyncio.sleep(0)
        assert case.backend._close_owner is owner and blocked.attempts == 1
        blocked.release.set()
        await asyncio.wait_for(retry, 2)
        assert case.backend.closed and case.backend._close_owner is owner
        assert [workspace.attempts for workspace in case.workspaces] == [1, 1]
        assert case.registry.closes == 1 and case.released_access == [True]
        assert case.stream.closes == 1 and case.application.state.workspace_lifespan is None
    finally:
        blocked.release.set()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_cancelling_one_waiter_keeps_the_shared_close_owner(case: Case) -> None:
    first = asyncio.create_task(case.backend.close())
    second = asyncio.create_task(case.backend.close())
    try:
        await asyncio.wait_for(case.workspaces[-1].started.wait(), 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, 2)
        assert not second.done()
        assert [workspace.attempts for workspace in case.workspaces] == [1, 1]
        for workspace in case.workspaces:
            workspace.release.set()
        await asyncio.wait_for(second, 2)
        assert case.backend.closed and case.registry.closes == case.stream.closes == 1
    finally:
        for workspace in case.workspaces:
            workspace.release.set()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_late_failure_is_reported_and_retry_keeps_completed_mounts(case: Case, caplog: pytest.LogCaptureFixture) -> None:
    peer, blocked = case.workspaces
    peer.release.set()
    failure = ConnectionError("原writer迟到失败")
    blocked.failures.append(failure)
    waiting = asyncio.create_task(case.backend.close())
    try:
        await asyncio.wait_for(blocked.started.wait(), 2)
        assert case.deadlines
        case.deadlines[0].reschedule(asyncio.get_running_loop().time())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(waiting, 2)
        owner = case.backend._close_owner
        blocked.release.set()
        with pytest.raises(BaseExceptionGroup) as caught:
            await asyncio.wait_for(asyncio.shield(owner), 2)
        assert caught.value.exceptions == (failure,)
        assert "Backend关闭owner失败" in caplog.text
        assert case.registry.closes == 0 and case.released_access == []
        await case.backend.close()
        assert case.backend._close_owner is not owner
        assert peer.attempts == 1 and blocked.attempts == 2
        assert case.registry.closes == case.stream.closes == 1
    finally:
        blocked.release.set()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_model_stream_wait_uses_original_batch_budget_and_preserves_successful_steps(case: Case) -> None:
    for workspace in case.workspaces:
        workspace.release.set()
    case.stream.release.clear()
    failure = TimeoutError("原模型传输关闭失败")
    case.stream.failures.append(failure)
    waiting = asyncio.create_task(case.backend.close())
    try:
        await asyncio.wait_for(case.stream.started.wait(), 2)
        assert len(case.deadlines) == 1
        case.deadlines[0].reschedule(asyncio.get_running_loop().time())
        with pytest.raises(TimeoutError, match="stage=model_stream"):
            await asyncio.wait_for(waiting, 2)
        owner = case.backend._close_owner
        case.stream.release.set()
        with pytest.raises(TimeoutError) as caught:
            await asyncio.wait_for(asyncio.shield(owner), 2)
        assert caught.value is failure
        await case.backend.close()
        assert case.registry.closes == 1 and case.released_access == [True]
        assert case.stream.closes == 2
        assert all(workspace.attempts == 1 for workspace in case.workspaces)
    finally:
        case.stream.release.set()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_committed_close_wins_when_deadline_delivery_races_the_receipt(case: Case) -> None:
    for workspace in case.workspaces:
        workspace.release.set()
    case.stream.release.clear()
    waiting = asyncio.create_task(case.backend.close())
    try:
        await asyncio.wait_for(case.stream.started.wait(), 2)
        assert len(case.deadlines) == 1
        case.deadlines[0].reschedule(asyncio.get_running_loop().time())
        case.stream.release.set()
        await asyncio.wait_for(waiting, 2)
        assert case.deadlines[0].expired() and case.backend.closed
        assert case.registry.closes == case.stream.closes == 1
    finally:
        case.stream.release.set()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_original_close_timeout_keeps_its_identity(case: Case) -> None:
    for workspace in case.workspaces:
        workspace.release.set()
    failure = TimeoutError("原传输故障，不是Backend等待期限")
    case.stream.failures.append(failure)
    with pytest.raises(TimeoutError) as caught:
        await case.backend.close()
    assert caught.value is failure
    assert not case.backend.closed
    assert case.registry.closes == 1 and case.released_access == [True]
    await case.backend.close()
    assert case.backend.closed and case.registry.closes == 1
