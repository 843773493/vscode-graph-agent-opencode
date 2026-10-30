"""真实资源短Session准入、并行工具与正式导航删除的holder排空。"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.core import session_subtree_delete as deletion
from app.core.identifier import create_prefixed_id
from app.core.mounted_workspaces import MountedWorkspace
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.schemas.internal_v2.session import SessionCreateRequest
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationMutationEnqueueRequest,
    NavigationMutationIntentDTO,
)
from app.services.business.session_navigation.operations_service import (
    local_navigation_scope,
)
from app.services.business.terminal_resources.operations import TerminalOperations
from app.services.business.workspace_resources import owner as owner_module
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.browser.test_browser_owner_revision_drain import (
    browser_client as browser_client,  # noqa: PLC0414
)
from tests.integration.workspace_services.browser.test_browser_owner_revision_drain import (
    owner_mount as owner_mount,  # noqa: PLC0414
)
from tests.integration.workspace_services.test_resource_release import (
    resource_api as resource_api,  # noqa: PLC0414
)
from tests.support.workspace_tool_runtime import workspace_tool_runtime
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    NativeStep,
    _entered,
    _turns,
)


@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_real_thread_admission_and_delete(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext], owner_mount: MountedWorkspace,
    monkeypatch: pytest.MonkeyPatch, resource_api, kind: str,
) -> None:
    _client, root, run = browser_client
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, _sid, _context):
        session = await container.session_service.create(SessionCreateRequest(title="独立资源删除"))
        sid = session.session_id
        tid = container.session_catalog.resolver.main_thread_id(sid)
        source = container.session_catalog.resolver.resolve_session_node(sid)
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        ledger_path = owner._leases._state_path
        assert ledger_path is not None
        deadlines: list[asyncio.Timeout] = []
        entered = asyncio.Event()
        timeout = asyncio.timeout
        dispatched: list[str] = []
        method = "create_terminal" if kind == "terminal" else "create_browser"
        create = getattr(owner._transport, method)

        def capture(seconds: float) -> asyncio.Timeout:
            context = timeout(seconds)
            task = asyncio.current_task()
            assert task is not None
            if task.get_name() == "thread-admit-under-test":
                assert seconds == 30
                deadlines.append(context)
                entered.set()
            return context

        async def creating(**kwargs):
            dispatched.append(kwargs["terminal_id" if kind == "terminal" else "browser_id"])
            return await create(**kwargs)

        async with resource_api(container, kind) as client:
            resources: list[str] = []
            for number in range(2):
                response = await client.post(f"/api/v1/resources/{kind}s", json={
                    "session_id": sid, "retention_scope": "thread", "title": f"thread-{number}",
                })
                assert response.status_code == 200, response.text
                resources.append(response.json()["data"]["resource_id"])
            with monkeypatch.context() as patch:
                patch.setattr(asyncio, "timeout", capture)
                patch.setattr(owner._transport, method, creating)
                async with container.session_service._session_lifecycle_gate.exclusive(sid) as held:
                    before, ledger = owner._store.path.read_bytes(), ledger_path.read_bytes()
                    pending = asyncio.create_task(client.post(f"/api/v1/resources/{kind}s", json={
                        "session_id": sid, "retention_scope": "thread", "title": "explicit-retry",
                    }, headers={"X-Request-ID": "thread-admission-under-test"}), name="thread-admit-under-test")
                    try:
                        await asyncio.wait_for(entered.wait(), 5)
                        deadline = deadlines[0].when()
                        assert deadline is not None
                        deadlines[0].reschedule(deadline - 31)
                        failed = await asyncio.wait_for(asyncio.shield(pending), 1)
                        assert failed.status_code == 503, failed.text
                        assert failed.headers["X-Request-ID"] == "thread-admission-under-test"
                        assert "TimeoutError" in failed.text and str(root) not in failed.text
                        assert len(deadlines) == 1 and deadlines[0].expired()
                        assert held.acquired and dispatched == []
                        assert owner._store.path.read_bytes() == before and ledger_path.read_bytes() == ledger
                    finally:
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                await asyncio.sleep(0)
                assert dispatched == []
                async with owner.deletion_holder.lock_for(sid, wait=False) as proof:
                    assert proof.acquired
                retried = await client.post(f"/api/v1/resources/{kind}s", json={
                    "session_id": sid, "retention_scope": "thread", "title": "explicit-retry",
                })
                assert retried.status_code == 200, retried.text
                assert dispatched == [retried.json()["data"]["resource_id"]]
                resources.append(dispatched[0])

        rid, peer, _retry = resources
        reached = asyncio.Event()
        observe = deletion.observe_phase
        operation_id = create_prefixed_id("op")

        async def observed(key: str, phase: str, **kwargs) -> None:
            await observe(key, phase, **kwargs)
            if key == operation_id and phase == f"deletion.drain_waiting/{sid}":
                reached.set()

        async with owner.operation(session_id=sid, thread_id=tid,
                                   holder_id="request:long-resource", operation_id="long-resource") as operation:
            if isinstance(operation, TerminalOperations):
                await operation.read_terminal(rid)
            else:
                await operation.perform(rid, "read", {})
            original, = owner._leases.leases_for_resource(rid)
            assert original.status == "active"

            async def independent() -> None:
                async with owner.operation(session_id=sid, thread_id=tid,
                                           holder_id="request:peer-resource", operation_id="peer-resource") as peer_operation:
                    if isinstance(peer_operation, TerminalOperations):
                        await peer_operation.read_terminal(peer)
                    else:
                        await peer_operation.perform(peer, "read", {})
            await asyncio.wait_for(independent(), 5)

            async def read_session() -> None:
                async with container.session_service.session_read_admission(sid):
                    assert source.is_dir()
            await asyncio.wait_for(read_session(), 5)
            catalog = container.session_catalog_service
            scope = local_navigation_scope(container.workspace_id)
            with monkeypatch.context() as patch:
                patch.setattr(deletion, "observe_phase", observed)
                accepted = await catalog.operations.enqueue(NavigationMutationEnqueueRequest(intents=[
                    NavigationMutationIntentDTO(
                        client_operation_id=operation_id, client_sequence=1,
                        kind="delete_session", target_node_id=sid,
                        base_catalog_revision=catalog.path_resolver.revision,
                    ),
                ]), scope)
                assert accepted.accepted_count == 1
                await asyncio.wait_for(reached.wait(), 5)
            receipt, = catalog.operations.status([operation_id], scope).items
            assert receipt.state == "committed" and receipt.settlement == "pending"
            assert source.is_dir() and owner._leases.get_lease(original.lease_id) == original
            async with owner.deletion_holder.lock_for(sid, wait=False) as proof:
                assert not proof.acquired
            with pytest.raises(SessionDeletionPendingError):
                async with owner.admit_thread(sid, tid):
                    pytest.fail("deleting Session不能接纳新资源操作")
            assert (await owner._transport.get_resource(rid))["status"] == "running"

        async with asyncio.timeout(10):
            while True:
                receipt, = catalog.operations.status([operation_id], scope).items
                if receipt.settlement == "settled":
                    break
                assert receipt.settlement == "pending", receipt
                await asyncio.sleep(0.02)
        assert not source.exists()
        released = owner._leases.get_lease(original.lease_id)
        assert released is not None and released.status == "released"
        final = [await owner._transport.get_resource(resource_id) for resource_id in resources]
        assert all(record["status"] == "deleted" for record in final)
        assert all(not owner._leases.leases_for_resource(resource_id) for resource_id in resources)
        (run.artifacts_dir / f"thread-{kind}.json").write_text(json.dumps({
            "failure": failed.json(), "retry": retried.json(), "deletion": receipt.model_dump(mode="json"),
            "lease_id": original.lease_id, "external": final,
            "scope": "正式Session/ASGI资源创建/工具operation/导航enqueue及唯一worker；真实PTY或Chromium/HTTP；不代表Gateway/Web、独立Backend重启或整个operation终止期限",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_real_admission_io_settles_before_cancellation_releases_gates(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext], owner_mount: MountedWorkspace,
    monkeypatch: pytest.MonkeyPatch, resource_api, kind: str,
) -> None:
    _client, root, run = browser_client
    evidence = []
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, _sid, _context):
        sessions = container.session_service
        session = await sessions.create(SessionCreateRequest(title="同步准入原Session"))
        sid, tid = session.session_id, session.thread_id
        peer = await sessions.create(SessionCreateRequest(title="同步准入peer"))
        resolver = container.session_catalog.resolver
        source = resolver.resolve_session_node(sid)
        store = resolver.catalog_store
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        loop, loop_thread = asyncio.get_running_loop(), threading.get_ident()
        async with resource_api(container, kind) as client:
            baseline = await client.post(f"/api/v1/resources/{kind}s", json={"session_id": sid})
            assert baseline.status_code == 200, baseline.text
            ledger_path = owner._leases._state_path
            assert ledger_path is not None
            async def check_stage(stage: str) -> None:
                native, pool_step = NativeStep(), NativeStep()
                executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="product-thread-admission")
                tasks: list[Awaitable[object]] = []
                budget = asyncio.timeout(None)
                gate_entered = asyncio.Event()
                read_bodies = []
                dispatched = []
                request_id = f"{kind}-{stage}-io"
                before, ledger = owner._store.path.read_bytes(), ledger_path.read_bytes()
                method = "create_terminal" if kind == "terminal" else "create_browser"
                create = getattr(owner._transport, method)
                get_node = resolver.get_node
                main_thread = resolver.main_thread_id
                thread_node = resolver.resolve_thread_node
                is_dir = Path.is_dir
                reads = 0

                def pause() -> None:
                    if not native.calls:
                        native.block()

                def catalog_read(session_id: str):
                    nonlocal reads
                    if session_id == sid:
                        reads += 1
                        if (stage in {"write-active", "read-active"} and reads == 1) or (stage == "session-read" and reads == 2):
                            pause()
                    return get_node(session_id)

                def directory(path: Path) -> bool:
                    if stage == "session-directory" and path == source:
                        pause()
                    return is_dir(path)

                def main(session_id: str) -> str:
                    if stage == "main" and session_id == sid:
                        pause()
                    return main_thread(session_id)

                def thread(session_id: str, thread_id: str) -> Path:
                    if stage in {"thread", "pool"} and session_id == sid:
                        pause()
                    return thread_node(session_id, thread_id)

                async def creating(**kwargs):
                    dispatched.append(kwargs["terminal_id" if kind == "terminal" else "browser_id"])
                    return await create(**kwargs)

                async def request():
                    async with budget:
                        if stage == "read-active":
                            async with sessions.session_read_admission(sid):
                                read_bodies.append(sid)
                                return await sessions.get(sid)
                        return await client.post(f"/api/v1/resources/{kind}s", json={
                            "session_id": sid, "thread_id": None if stage == "main" else tid,
                        }, headers={"X-Request-ID": request_id})

                async def contend() -> None:
                    async with sessions._session_lifecycle_gate.exclusive(sid):
                        gate_entered.set()

                def hold_catalog() -> None:
                    with store._connection_lock:
                        native.block()

                worker = owner_module.run_sync_shielded if stage == "pool" else None
                queued = asyncio.Event()

                async def queue(operation, *args, **kwargs):
                    tasks.append(loop.run_in_executor(None, pool_step.block))
                    await _entered(pool_step)
                    queued.set()
                    assert worker is not None
                    return await worker(operation, *args, **kwargs)

                with monkeypatch.context() as patch:
                    patch.setattr(resolver, "get_node", catalog_read)
                    patch.setattr(resolver, "main_thread_id", main)
                    patch.setattr(resolver, "resolve_thread_node", thread)
                    patch.setattr(Path, "is_dir", directory)
                    patch.setattr(owner._transport, method, creating)
                    if stage == "catalog-lock":
                        tasks.append(loop.run_in_executor(None, hold_catalog))
                        await _entered(native)
                    if stage == "pool":
                        patch.setattr(loop, "_default_executor", executor)
                        patch.setattr(owner_module, "run_sync_shielded", queue)
                    pending = asyncio.create_task(request())
                    tasks.append(pending)
                    try:
                        if stage == "pool":
                            await asyncio.wait_for(queued.wait(), 2)
                            assert not native.calls
                        elif stage != "catalog-lock":
                            await _entered(native)
                        else:
                            async with asyncio.timeout(2):
                                while reads == 0:
                                    await asyncio.sleep(0.001)
                        tasks.append(asyncio.create_task(contend()))
                        if stage in {"session-read", "main", "read-active", "catalog-lock"}:
                            pending.cancel("产品Thread同步准入取消")
                        else:
                            budget.reschedule(loop.time())
                        await _turns()
                        assert not pending.done() and not gate_entered.is_set()
                        assert dispatched == [] and read_bodies == []
                        # 原catalog锁/线程池可阻塞读取；辅助服务HTTP和事件循环仍须可调度。
                        external = await asyncio.wait_for(owner._transport.list_resources(), 2)
                        assert external
                        if stage not in {"catalog-lock", "pool"}:
                            assert (await asyncio.wait_for(sessions.get(peer.session_id), 2)).session_id == peer.session_id
                        if stage != "read-active":
                            async with owner.deletion_holder.lock_for(sid, wait=False) as proof:
                                assert not proof.acquired
                        if stage == "pool":
                            pool_step.release.set()
                            await _entered(native)
                        native.release.set()
                        expected = asyncio.CancelledError if stage in {"session-read", "main", "read-active", "catalog-lock"} else TimeoutError
                        with pytest.raises(expected) as caught:
                            await pending
                        assert str(root) not in str(caught.value)
                        await asyncio.gather(*tasks, return_exceptions=True)
                        assert gate_entered.is_set() and dispatched == [] and read_bodies == []
                        assert owner._store.path.read_bytes() == before and ledger_path.read_bytes() == ledger
                        assert len(native.calls) == 1 and native.calls[0] != loop_thread
                        evidence.append({"stage": stage, "error": type(caught.value).__name__, "worker_thread": native.calls[0], "loop_thread": loop_thread})
                    finally:
                        native.release.set()
                        pool_step.release.set()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        executor.shutdown(wait=True)
                async with owner.deletion_holder.lock_for(sid, wait=False) as proof:
                    assert proof.acquired
                retried = await client.post(f"/api/v1/resources/{kind}s", json={"session_id": sid, "thread_id": tid})
                assert retried.status_code == 200, retried.text
                assert retried.headers["X-Request-ID"] == retried.json()["request_id"]
                current = retried.json()["data"]
                assert current["source_thread"] == {"session_id": sid, "thread_id": tid}
                assert current["workspace_id"] == container.workspace_id and current["owner_revision"] == 1

            for stage in ("write-active", "session-read", "session-directory", "main", "thread", "read-active", "catalog-lock", "pool"):
                await check_stage(stage)
        (run.artifacts_dir / f"thread-io-{kind}.json").write_text(json.dumps({
            "stages": evidence,
            "scope": "正式Session、ASGI资源创建、真实PTY/Chromium与HTTP；不代表Gateway/Web、Windows、Backend硬重启或原生线程可终止",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
