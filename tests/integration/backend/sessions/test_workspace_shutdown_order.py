"""真实挂载和消息执行后，关闭失败保留原runtime、存储与维护互斥。"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from app import main
from app.core.checkpoint_config import build_checkpoint_config
from app.core.path_utils import workspace_context
from app.core.resource_uri import parse_attachment_uri
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.schemas.internal_v2.job import JobStatus
from app.schemas.internal_v2.message import (
    MessageCreateRequest,
    MessageRunRequest,
    RunOptions,
)
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceBusyError,
    AttachmentMaintenanceLock,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.mcp import McpCatalogOwner
from tests.integration.backend.sessions.test_attachment_directory_entry import upload
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    MountedCase,
    _plan_model_call,
)
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    mounted_case as mounted_case,  # noqa: PLC0414
)


def _causes(error: BaseException):
    yield error
    if isinstance(error, BaseExceptionGroup):
        for item in error.exceptions:
            yield from _causes(item)


@pytest.mark.parametrize("phase", ["runtime", "runtime_timeout", "backend_wait", "body_runtime", "attachments", "catalog"])
async def test_backend_shutdown_preserves_dependencies_and_retries_original_owner(
    mounted_case: MountedCase, inprocess_model_replay, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    case = mounted_case
    replay, controller = inprocess_model_replay
    injected = ConnectionError("原owner关闭失败")
    calls = 0
    closed_root = list(case.roots.values())[-1]
    store_close, catalog_close = AttachmentBlobStore.close, SessionCatalogStore.close

    def fail_attachment(store):
        nonlocal calls
        if phase == "attachments" and store._workspace_root == closed_root and not calls:
            calls += 1
            raise injected
        return store_close(store)

    def fail_catalog(store):
        nonlocal calls
        if phase == "catalog" and store.database_path.is_relative_to(closed_root) and not calls:
            calls += 1
            raise injected
        return catalog_close(store)

    monkeypatch.setattr(AttachmentBlobStore, "close", fail_attachment)
    monkeypatch.setattr(SessionCatalogStore, "close", fail_catalog)
    mcp_tasks = {}
    mcp_start, mcp_shutdown = McpCatalogOwner.start, McpCatalogOwner.shutdown

    async def start_mcp(owner):
        mcp_tasks[owner] = asyncio.current_task()
        await mcp_start(owner)

    async def stop_mcp(owner):
        assert asyncio.current_task() is mcp_tasks[owner], "MCP启动及重试关闭必须由同一原任务持有"
        await mcp_shutdown(owner)

    monkeypatch.setattr(McpCatalogOwner, "start", start_mcp)
    monkeypatch.setattr(McpCatalogOwner, "shutdown", stop_mcp)
    backend_deadlines: list[asyncio.Timeout] = []

    def backend_deadline(seconds: float) -> asyncio.Timeout:
        assert seconds == 30
        deadline = asyncio.timeout(seconds)
        backend_deadlines.append(deadline)
        return deadline

    monkeypatch.setattr(inspect.getmodule(main.BackendLifespan), "timeout", backend_deadline, raising=False)
    lifetime = main.lifespan(main.app)
    await lifetime.__aenter__()
    exited = False
    entered, finish = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    original_time = loop.time
    clock_task = None
    observations = []
    try:
        peer, failed = case.containers
        sources = []
        for container in case.containers:
            with workspace_context(container.workspace_id, container.workspace_root, session_catalog=container.session_catalog):
                session = await container.session_service.create(SessionCreateRequest(title="关闭依赖验收"))
                tid = container.session_catalog.resolver.main_thread_id(session.session_id)
                _plan_model_call(replay, container, session.session_id, tid)
                accepted = await container.session_orchestrator.create_message(session.session_id, MessageRunRequest(
                    message=MessageCreateRequest(content="使用上传文件", attachments=[upload(b"shutdown original")]),
                    run=RunOptions(),
                ))
                job = container.job_service._jobs[accepted.job_id]
                await asyncio.wait_for(asyncio.shield(job.task), 30)
                assert job.status == JobStatus.completed, job.error_message
                await container.job_service.drain_finalizations()
                ref, = job.attachments
                sid = session.session_id
                with session_thread_owner_scope(MutationIntentOwner(sid, tid), langgraph_thread_id=tid):
                    checkpoint = container.checkpointer.get_tuple(build_checkpoint_config(tid))
                assert checkpoint is not None
                history = container.checkpointer._storage.storage.jsonl_path(sid, thread_id=tid)
                sources.append((sid, tid, ref, checkpoint, history, history.read_bytes()))
        assert controller.transport.call_count == 2
        sid, tid, ref, checkpoint, history, canonical = sources[-1]
        state = failed.thread_residency_tracker._states[(sid, tid)]
        scope = state.runtime.lifetime_scope
        attachment_id = parse_attachment_uri(ref.file_id, require_file=True).attachment_id
        generation = state.generation

        def observe_late_cleanup():
            # 原scope真实收尾时仍能消费同一存储与locator，不写测试业务状态。
            record = failed.attachment_blob_store._catalog.get_directory(attachment_id)
            node = failed.session_catalog.resolver.resolve_session_node(sid)
            observations.append((record.attachment_id, node, history.read_bytes()))

        scope.register(observe_late_cleanup, label="原scope迟到收尾的存储观察")

        async def release(session, thread, current_generation):
            nonlocal calls
            if (session, thread) != (sid, tid):
                return
            assert current_generation == generation
            calls += 1
            if phase in {"runtime_timeout", "backend_wait"}:
                entered.set()
                await finish.wait()
            elif calls == 1:
                raise injected

        if phase in {"runtime", "runtime_timeout", "backend_wait", "body_runtime"}:
            failed.thread_residency_tracker.add_unload_listener(release)
        if phase in {"runtime_timeout", "backend_wait"}:
            async def advance_deadline():
                await entered.wait()
                backend = main.app.state.workspace_lifespan
                peer_workspace = next(item for item in backend.workspaces if item.container is peer)
                # 推进时钟前先核对独立挂载真实关闭，避免全局跳时误伤健康peer。
                await asyncio.wait_for(asyncio.shield(peer_workspace._owner), 5)
                if phase == "backend_wait":
                    assert len(backend_deadlines) == 1
                    backend_deadlines[0].reschedule(loop.time())
                else:
                    # 本场景只推进runtime时域；Backend总预算由backend_wait单独验证。
                    for deadline in backend_deadlines:
                        deadline.reschedule(deadline.when() + 31)
                    monkeypatch.setattr(loop, "time", lambda: original_time() + 31)
            clock_task = asyncio.create_task(advance_deadline())
        body_error = ValueError("原工作区业务错误") if phase == "body_runtime" else None
        with pytest.raises(TimeoutError if phase == "backend_wait" else BaseExceptionGroup) as failure:
            exited = True
            exit_call = lifetime.__aexit__(type(body_error) if body_error else None, body_error, None)
            if phase == "backend_wait":
                await asyncio.wait_for(exit_call, 10)
            else:
                await exit_call
        if clock_task is not None:
            await clock_task
        monkeypatch.setattr(loop, "time", original_time)
        errors = list(_causes(failure.value))
        if phase == "runtime_timeout":
            assert any(isinstance(item, TimeoutError) and "shutdown-drain" in str(item) and tid in str(item) for item in errors)
        elif phase == "backend_wait":
            assert "Backend关闭等待超过30秒" in str(failure.value) and failed.workspace_id in str(failure.value)
        else:
            assert injected in errors
        if body_error is not None:
            assert body_error in errors
        assert failed.session_catalog.resolver.resolve_session_node(sid).is_dir()
        backend = main.app.state.workspace_lifespan
        assert backend is not None and not backend.closed
        assert main.app.state.workspace_container_registry is backend.registry
        assert history.read_bytes() == canonical
        if phase != "catalog":
            assert failed.attachment_blob_store._catalog.get_directory(attachment_id).attachment_id == attachment_id
            assert backend.registry.get(failed.workspace_id) is failed
        for container in case.containers:
            with pytest.raises(AttachmentMaintenanceBusyError), AttachmentMaintenanceLock(
                container.workspace_root / ".boxteam/attachments", exclusive=True,
            ):
                pytest.fail("原关闭未完成时不能归还维护锁")
        assert peer.resource_platform.process_root_scope.is_closed
        peer_calls = [entry for entry in case.closed if entry[0] == peer.workspace_id]
        assert peer_calls == [(peer.workspace_id, stage) for stage in ("jobs", "runtime", "platform")]
        if phase in {"runtime", "runtime_timeout", "backend_wait", "body_runtime"}:
            assert not failed.resource_platform.process_root_scope.is_closed
            with session_thread_owner_scope(MutationIntentOwner(sid, tid), langgraph_thread_id=tid):
                restored = failed.checkpointer.get_tuple(build_checkpoint_config(tid))
            assert restored.checkpoint == checkpoint.checkpoint and restored.metadata == checkpoint.metadata
            assert restored.pending_writes == checkpoint.pending_writes
        if phase in {"runtime_timeout", "backend_wait"}:
            original_unload = state.runtime_unload
            assert original_unload is not None and not original_unload.done() and state.generation == generation
            finish.set()
            await asyncio.wait_for(asyncio.shield(original_unload), 5)
            assert calls == 1 and len(observations) == 1
        original_backend_owner = getattr(backend, "_close_owner", None)
        await backend.close()
        if phase == "backend_wait":
            assert backend._close_owner is original_backend_owner
        assert backend.closed and main.app.state.workspace_lifespan is None
        assert main.app.state.workspace_container_registry is None
        assert len(observations) == 1 and observations[0][0] == attachment_id and observations[0][2] == canonical
        assert [entry for entry in case.closed if entry[0] == peer.workspace_id] == peer_calls
        assert sum(entry == (failed.workspace_id, "jobs") for entry in case.closed) == 1
        for container, source in zip(case.containers, sources, strict=True):
            assert container.resource_platform.process_root_scope.is_closed
            assert source[4].read_bytes() == source[5]
            with pytest.raises(RuntimeError, match="已关闭|closed"):
                _ = container.session_catalog.resolver.catalog_store.connection
            with AttachmentMaintenanceLock(container.workspace_root / ".boxteam/attachments", exclusive=True):
                pass
    finally:
        monkeypatch.setattr(loop, "time", original_time)
        finish.set()
        if clock_task is not None:
            clock_task.cancel()
            await asyncio.gather(clock_task, return_exceptions=True)
        if not exited:
            await lifetime.__aexit__(None, None, None)
        backend = getattr(main.app.state, "workspace_lifespan", None)
        if backend is not None:
            await backend.close()
