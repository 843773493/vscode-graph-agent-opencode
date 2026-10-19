"""来源 middleware 不得在异步模型节点提前争抢 rollout 同步读锁。"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.skill_runtime import WorkspaceSkillsMiddleware
from app.services.infrastructure.rollout_context.storage.primitives import (
    _RolloutFileLock,
)
from tests.integration.backend.sessions.rollout_context.test_agent_production_context import (
    factory_context,
    registry_db,
)

__all__ = ["factory_context", "registry_db"]


@pytest.mark.asyncio
async def test_model_source_observation_does_not_read_before_preparation_owns_lock(
    factory_context, monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver, session, thread, _connection, _selection, manager, captured, _tools, dispatch, _files = factory_context
    middleware = next(item for item in captured["middleware"] if isinstance(item, WorkspaceSkillsMiddleware))
    old_assemblies = saver.list_context_assemblies(session, thread)
    pending = manager.prepare_pending()
    path = saver._storage.storage.root(session, thread_id=thread).parent / ".rollout.write.lock"
    entered, release, read_attempted = (asyncio.Event() for _ in range(3))
    loop = asyncio.get_running_loop()
    acquire = _RolloutFileLock.acquire

    def record_read(lock):
        if lock._path == path.resolve() and not lock._exclusive:
            loop.call_soon_threadsafe(read_attempted.set)
        return acquire(lock)

    async def hold_writer():
        lock = _RolloutFileLock(path, exclusive=True)
        await lock.acquire_async()
        try:
            entered.set()
            await release.wait()
        finally:
            lock.release()

    monkeypatch.setattr(_RolloutFileLock, "acquire", record_read)
    holder = asyncio.create_task(hold_writer())
    preparation = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        # 旧实现此时在同一事件循环同步读 rebuild，直接触发锁竞争错误。
        assert middleware.before_model({}, None) is None
        assert not read_attempted.is_set()
        preparation = asyncio.create_task(asyncio.to_thread(dispatch, "source-read-lock"))
        await asyncio.wait_for(read_attempted.wait(), 3)
        assert not preparation.done()
        release.set()
        outbound = await asyncio.wait_for(asyncio.shield(preparation), 15)
    finally:
        release.set()
        await holder
        if preparation is not None:
            await asyncio.gather(preparation, return_exceptions=True)
    assemblies = saver.list_context_assemblies(session, thread)
    assert len(assemblies) == len(old_assemblies) + 1
    assert tuple(assembly.to_dict() for assembly in assemblies[:-1]) == tuple(
        assembly.to_dict() for assembly in old_assemblies
    )
    assert outbound.messages
    assert pending is not None and any(delta.source_id == "instruction:agent-instructions" for delta in pending.deltas)
    root = assemblies[-1].seal_binding.root
    assert root is not None and any(source.source_identity == "instruction:agent-instructions" for source in root.sources)
