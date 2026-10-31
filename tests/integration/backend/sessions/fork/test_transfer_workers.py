"""冻结文件传输的原生等待保留原操作，取消后先结算再清理。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock, get_ident
from typing import BinaryIO, cast

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.runnables import RunnableConfig

from app.api import sessions
from app.container import AppContainer
from app.core.checkpoint_config import build_checkpoint_config
from app.core.resource_uri import resource_display_uri, thread_resource_uri
from app.core.session_thread_context import session_thread_owner_scope
from app.core.trace_middleware import TraceMiddleware
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.schemas.internal_v2.session import SessionDTO
from app.services.infrastructure.rollout_context.checkpoint import fork_owner
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.fork import source_capture, transfer
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_message as fork_execution_message,  # noqa: PLC0414
)
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_source as fork_execution_source,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    SourceForkOwnerTransport,
    _fork_request,
    _main_thread_id,
    _main_thread_node,
    _production_fork_context,
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    fork_gateway_request_context as fork_gateway_request_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    production_fork_container as production_fork_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    remote_fork_target as remote_fork_target,  # noqa: PLC0414
)


@pytest.mark.parametrize(("phase", "interruption"), [
    ("export_manifest", "cancel"), ("export_chunk", "deadline"),
    ("receive_open", "cancel"), ("receive_write", "cancel"), ("receive_flush", "deadline"),
    ("receive_close", "deadline"), ("receive_publish", "cancel"),
    ("marker_read", "cancel"), ("cleanup", "cancel"), ("pool", "cancel"),
])
async def test_public_fork_file_wait_keeps_owner_and_settles_before_cleanup(
    production_fork_container: AppContainer, fork_execution_source: SessionDTO,
    remote_fork_target: tuple[AppContainer, SourceForkOwnerTransport],
    monkeypatch: pytest.MonkeyPatch, phase: str, interruption: str,
) -> None:
    source = fork_execution_source
    context = _production_fork_context(production_fork_container)
    source_thread_id = _main_thread_id(context, source.session_id)
    target, transport = remote_fork_target
    request = (await _fork_request(context, source.session_id, creation_kind="new_session_main",
                                  mode="full_rollout_copy")).model_copy(update={
        "source_uri": thread_resource_uri(source.workspace_id, source.session_id, source_thread_id,
            gateway_authority="gateway_4aae160f813ed87bcf65a8bb8bfd3729"),
        "target_container_uri": resource_display_uri(scope="workspace", scope_id=target.workspace_id,
            kind="session", tail_segments=(), gateway_authority="gateway_2d2ab24c1f08e28f5f0217140c8be744"),
    })
    canonical = _main_thread_node(context, source.session_id) / "rollout/rollout.jsonl"
    original = canonical.read_bytes()
    with session_thread_owner_scope(MutationIntentOwner(source.session_id, source_thread_id),
                                   langgraph_thread_id=source_thread_id):
        checkpoint = context.saver.get_tuple(cast(RunnableConfig, build_checkpoint_config(source_thread_id)))
    assert checkpoint is not None
    assemblies = context.saver._storage.assemblies.list_context_assemblies(source.session_id, source_thread_id)
    visible = {item.session_id for item in (await target.session_service.list()).items}
    key = target.session_context_fork_service._creation_key(request.idempotency_key)
    target_root = target.checkpointer._storage.storage.sessions_dir
    target_captures = target_root.parent / "navigation/fork-source-captures"
    entered, release = Event(), Event()
    arming = Lock()
    attempts: list[int] = []
    opened: list[BinaryIO] = []
    loop = asyncio.get_running_loop()
    event_loop_thread = get_ident()
    previous_pool = getattr(loop, "_default_executor")  # noqa: B009 - 结束后恢复原默认池。
    assert isinstance(previous_pool, ThreadPoolExecutor)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fork-file-wait") if phase == "pool" else None
    occupied = None
    original_io, real_open = transfer._storage_io, Path.open
    receive, dispatch = transfer.receive_transfer_file, transport.fork_transfer
    upstream = ConnectionError("冻结分块传输中断")
    failed_transfer = False
    import_errors: list[asyncio.CancelledError] = []
    import_snapshot = fork_owner.import_captured_artifact

    async def observe_import(*args, **kwargs):
        try:
            return await import_snapshot(*args, **kwargs)
        except asyncio.CancelledError as error:
            import_errors.append(error)
            raise

    def pause() -> None:
        with arming:
            if entered.is_set():
                return
            attempts.append(get_ident())
            entered.set()
        if not release.wait(12):
            raise RuntimeError("文件I/O阻塞了事件循环，测试保护期限已到")

    @contextmanager
    def native_io(path: Path, message: str, stage: str, *, artifact: str) -> Iterator[None]:
        selected = stage == phase and (
            phase.startswith("export_") or Path(path).is_relative_to(target_captures)
        )
        if selected and phase != "receive_open":
            pause()
        with original_io(path, message, stage, artifact=artifact):
            yield

    def open_file(path: Path, *args, **kwargs):
        stream = real_open(path, *args, **kwargs)
        if args and args[0] == "xb" and path.is_relative_to(target_captures):
            opened.append(stream)
            # 已打开但返回值尚未交给调用者，取消也必须关闭这个原句柄。
            if phase == "receive_open":
                pause()
        return stream

    async def receive_file(*args, **kwargs) -> None:
        nonlocal occupied
        if pool is not None and occupied is None:
            occupied = pool.submit(pause)
            loop.set_default_executor(pool)
        await receive(*args, **kwargs)

    async def send(payload):
        nonlocal failed_transfer
        if phase == "cleanup" and payload.action == "chunk" and not failed_transfer:
            failed_transfer = True
            raise upstream
        return await dispatch(payload)

    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(sessions.router, prefix="/api/v1")
    app.dependency_overrides[sessions.verify_local_token] = lambda: "test-local-token"
    app.dependency_overrides[sessions.get_session_service] = lambda: target.session_service
    app.dependency_overrides[sessions.get_session_context_fork_service] = lambda: target.session_context_fork_service
    monkeypatch.setattr(transfer, "_storage_io", native_io)
    monkeypatch.setattr(source_capture, "_storage_io", native_io)
    monkeypatch.setattr(Path, "open", open_file)
    monkeypatch.setattr(transfer, "receive_transfer_file", receive_file)
    monkeypatch.setattr(transport, "fork_transfer", send)
    monkeypatch.setattr(fork_owner, "import_captured_artifact", observe_import)
    budget = asyncio.timeout(None)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://workspace",
                                headers={"X-BoxTeam-Gateway-Id": "gateway_2d2ab24c1f08e28f5f0217140c8be744"}) as client:
        async def fork() -> httpx.Response:
            async with budget:
                return await client.post(f"/api/v1/sessions/{source.session_id}/fork-context",
                                         json=request.model_dump(mode="json"))

        operation = asyncio.create_task(fork())
        contender = None
        acquired = asyncio.Event()
        try:
            async with asyncio.timeout(20):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            assert len(attempts) == 1 and attempts[0] != event_loop_thread
            assert not operation.done() and not release.is_set()
            if phase == "receive_open":
                assert opened and not opened[0].closed
            if phase == "pool":
                assert opened == []

            async def take_same_key() -> None:
                service = context.forks if phase.startswith("export_") else target.session_context_fork_service
                async with service._key_locks.lock_for(("export:" if phase.startswith("export_") else "") + key):
                    acquired.set()

            contender = asyncio.create_task(take_same_key())
            response = await asyncio.wait_for(client.get("/api/v1/sessions"), 3)
            assert response.status_code == 200, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"]
            assert {item["session_id"] for item in response.json()["data"]["items"]} == visible
            assert not acquired.is_set() and canonical.read_bytes() == original
            if interruption == "deadline":
                budget.reschedule(loop.time())
            else:
                operation.cancel("取消冻结文件传输")
            await asyncio.sleep(0.02)
            assert not operation.done() and not acquired.is_set()
            release.set()
            expected = TimeoutError if interruption == "deadline" else asyncio.CancelledError
            with pytest.raises(expected) as caught:
                await asyncio.wait_for(operation, 15)
            if interruption == "cancel":
                assert str(caught.value) == "取消冻结文件传输"
            if phase == "cleanup":
                # 正式watchdog独立task重抛外层取消；在进入观察器前核对原owner异常对象。
                assert len(import_errors) == 1
                chain = import_errors[0]
                seen = set()
                while chain is not upstream:
                    assert id(chain) not in seen
                    seen.add(id(chain))
                    chain = chain.__cause__ or chain.__context__
                    assert chain is not None
            await asyncio.wait_for(contender, 3)
            assert acquired.is_set() and all(stream.closed for stream in opened)
            if phase != "export_manifest":
                assert opened
            assert canonical.read_bytes() == original
            creation = target.session_service.path_resolver.catalog_store.get_creation_record(key)
            assert creation.state == "preparing" and creation.abort_reason is None
            capture = target.session_service.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=key)
            assert capture is not None and capture.state == "preparing"
            snapshot_root = transfer.capture_directory(target_root, capture)
            assert not snapshot_root.exists() and not snapshot_root.is_symlink()
            assert {item.session_id for item in (await target.session_service.list()).items} == visible
        finally:
            release.set()
            await asyncio.gather(operation, *([contender] if contender is not None else []), return_exceptions=True)
            loop.set_default_executor(previous_pool)
            if occupied is not None:
                occupied.result(timeout=3)
            if pool is not None:
                pool.shutdown(wait=True)
            # 即便断言失败，也经原产品入口结清同key操作和原60秒watchdog。
            monkeypatch.setattr(transport, "fork_transfer", dispatch)
            response = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))

        # 取消保留preparing；显式同key恢复必须复用原创建身份，不能另造目标。
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        sid, tid = result["session"]["session_id"], result["thread_id"]
        published = target.session_service.path_resolver.catalog_store.get_creation_record(key)
        assert published.state == "published" and published.abort_reason is None
        assert replace(published, state=creation.state, record_updated_at=creation.record_updated_at) == creation
        assert (sid, tid) == (creation.session_id, creation.main_thread_id)
        assert {item.session_id for item in (await target.session_service.list()).items} == visible | {sid}
        repeated = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
        assert repeated.status_code == 200 and repeated.json()["data"] == result
        with RolloutCheckpointSaver(target_root) as cold:
            with session_thread_owner_scope(MutationIntentOwner(sid, tid), langgraph_thread_id=tid):
                restored = cold.get_tuple(cast(RunnableConfig, build_checkpoint_config(tid, checkpoint_id=checkpoint.checkpoint["id"])))
            assert restored is not None and restored.checkpoint == checkpoint.checkpoint
            assert restored.metadata == checkpoint.metadata and restored.pending_writes == checkpoint.pending_writes
            assert (cold._storage.storage.fork_paths.thread_node(sid, tid) / "rollout/rollout.jsonl").read_bytes() == original
            for assembly in assemblies:
                copied = cold._storage.assemblies.get_context_assembly(sid, tid, assembly_id=assembly.assembly_id)
                assert copied.to_dict() == assembly.to_dict() and copied.request_hash == assembly.request_hash
        assert canonical.read_bytes() == original
