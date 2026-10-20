"""来源 middleware 不得在异步模型节点提前争抢 rollout 同步读锁。"""

from __future__ import annotations

import asyncio

import pytest
from langchain.agents.middleware import ModelRequest
from langgraph.runtime import ExecutionInfo, Runtime

from app.agents.skill_runtime import WorkspaceSkillsMiddleware
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.resource_activation import ResourceActivationPolicySnapshot
from app.services.infrastructure.rollout_context.storage.primitives import (
    _RolloutFileLock,
)
from app.services.orchestration.resource_activation.contracts import (
    reset_current_execution_resource_activation,
    set_current_execution_resource_activation,
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


@pytest.mark.asyncio
async def test_async_provider_preparation_waits_for_writer_without_blocking_its_task(
    factory_context, registry_db, monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver, session, thread, _connection, _selection, _manager, captured, tools, _dispatch, _files = factory_context
    bridge = captured["execution"]
    admission = saver.execution_admission(
        session, thread_id=thread, execution_id=registry_db[3]["initial_execution_id"],
    )
    request = ModelRequest(
        model=captured["model"], messages=[], state={"messages": []}, tools=list(tools.values()),
        runtime=Runtime(execution_info=ExecutionInfo(
            thread_id=thread, checkpoint_ns="", checkpoint_id="factory-checkpoint",
            task_id="async-provider-lock", node_attempt=1,
        )),
    )
    path = saver._storage.storage.root(session, thread_id=thread).parent / ".rollout.write.lock"
    entered, release, read_attempted = (asyncio.Event() for _ in range(3))
    loop = asyncio.get_running_loop()
    acquire = _RolloutFileLock.acquire

    def record_read(lock):
        if lock._path == path.resolve() and not lock._exclusive:
            loop.call_soon_threadsafe(read_attempted.set)
        return acquire(lock)

    async def hold_writer():
        async with _RolloutFileLock(path, exclusive=True):
            entered.set()
            await release.wait()

    with (
        session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
        execution_admission_scope(admission, langgraph_thread_id=thread),
    ):
        parent = await bridge._resource_activation.freeze_execution(
            admission=admission, policy=ResourceActivationPolicySnapshot(
                default_boundary="turn", boundaries={"mcp_tool_catalog": "model_call"},
            ),
        )
        token = set_current_execution_resource_activation(parent, admission=admission)
        monkeypatch.setattr(_RolloutFileLock, "acquire", record_read)
        holder = asyncio.create_task(hold_writer())
        preparation = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            preparation = asyncio.create_task(bridge._aprepare(request))
            await asyncio.wait_for(read_attempted.wait(), 3)
            # 持锁 Task 仍能运行；模型准备必须等待它释放，不能同步争锁失败。
            assert not preparation.done()
            release.set()
            prepared = await asyncio.wait_for(asyncio.shield(preparation), 15)
        finally:
            release.set()
            await holder
            if preparation is not None:
                await asyncio.gather(preparation, return_exceptions=True)
            reset_current_execution_resource_activation(token)
    assemblies = saver.list_context_assemblies(session, thread)
    assert len(assemblies) == 1
    assert prepared["snapshot"] == assemblies[0]
    assert prepared["snapshot"].execution_id == admission.execution_id
    assert prepared["messages"]
