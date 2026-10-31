"""正式创建及真实辅助服务验证工具准入失败不派发、不丢失原占用。"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import psutil
import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.core.resource_uri import resource_display_uri
from app.services.business.terminal_resources.operations import TerminalOperations
from app.services.business.workspace_resources import operations as operations_module
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
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


@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_real_admission_keeps_original_resource(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext], owner_mount: MountedWorkspace,
    monkeypatch: pytest.MonkeyPatch, resource_api, kind: str,
) -> None:
    _client, root, run = browser_client
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        ledger_path = owner._leases._state_path
        assert ledger_path is not None
        tid = container.session_catalog.resolver.main_thread_id(sid)
        async with resource_api(container, kind) as client:
            resources = []
            for number in range(2):
                response = await client.post(f"/api/v1/resources/{kind}s", json={
                    "session_id": sid, "retention_scope": "thread", "title": f"admit-{number}",
                })
                assert response.status_code == 200, response.text
                resources.append(response.json()["data"]["resource_id"])
        rid, peer = resources
        external = await owner._transport.get_resource(rid)
        deadlines: list[asyncio.Timeout] = []
        calls: list[str] = []
        recovered, continue_admission = asyncio.Event(), asyncio.Event()
        recover = owner.recover
        method = "read_terminal" if kind == "terminal" else "perform"
        dispatch = getattr(owner._transport, method)

        def capture(seconds: float) -> asyncio.Timeout:
            assert seconds == 30
            context = asyncio.timeout(seconds)
            task = asyncio.current_task()
            assert task is not None
            if task.get_name() == "admit-under-test":
                deadlines.append(context)
            return context

        async def recovering(resource_ids=None) -> None:
            await recover(resource_ids)
            task = asyncio.current_task()
            assert task is not None
            if task.get_name() == "admit-under-test":
                recovered.set()
                await continue_admission.wait()

        async def observed(resource_id: str, *args, **kwargs):
            calls.append(resource_id)
            return await dispatch(resource_id, *args, **kwargs)

        async with owner.operation(session_id=sid, thread_id=tid,
                                   holder_id="request:admit", operation_id="admit") as operation:
            async def read(resource_id: str):
                if isinstance(operation, TerminalOperations):
                    return await operation.read_terminal(resource_id)
                return await operation.perform(resource_id, "read", {})

            await read(rid)
            active, = owner._leases.leases_for_resource(rid)
            assert active.status == "active"
            lock = owner._resource_lock(rid)
            with monkeypatch.context() as patch:
                patch.setattr(operations_module, "timeout", capture, raising=False)
                patch.setattr(owner._transport, method, observed)
                patch.setattr(owner, "recover", recovering)
                pending = asyncio.create_task(read(rid), name="admit-under-test")
                try:
                    # 真实恢复完成后，由测试持原锁，明确阻塞工具准入而非恢复的子任务。
                    await asyncio.wait_for(recovered.wait(), 5)
                    await lock.acquire()
                    continue_admission.set()
                    await asyncio.sleep(0)
                    assert not pending.done()
                    await asyncio.wait_for(read(peer), 5)
                    before, ledger = owner._store.path.read_bytes(), ledger_path.read_bytes()
                    # typeshed未公开Task的等待者，仅测试读取CPython原future以核精确取消。
                    admission_waiter = getattr(pending, "_fut_waiter")  # noqa: B009
                    assert admission_waiter is not None and admission_waiter in (lock._waiters or ())
                    if deadlines:
                        deadline = deadlines[0].when()
                        assert deadline is not None
                        deadlines[0].reschedule(deadline - 31)
                    with pytest.raises(TimeoutError, match="operation 准入等待超过 30 秒") as caught:
                        await asyncio.wait_for(asyncio.shield(pending), 0.5)
                    assert len(deadlines) == 1 and deadlines[0].expired()
                    assert rid in str(caught.value) and str(root) not in str(caught.value)
                    assert lock.locked()
                    assert admission_waiter.cancelled() and admission_waiter not in (lock._waiters or ())
                    assert calls == [peer]
                    assert owner._store.path.read_bytes() == before
                    assert ledger_path.read_bytes() == ledger
                    lock.release()
                    await asyncio.sleep(0)
                    assert calls == [peer]
                    cold = ExternalResourceLeaseLedger(state_path=ledger_path)
                    assert cold.get_lease(active.lease_id) == active
                    current = await owner._transport.get_resource(rid)
                    identity = "os_pid" if kind == "terminal" else "active_page_id"
                    assert current[identity] == external[identity]
                    assert current["transport_owner_revision"] == external["transport_owner_revision"] == 1
                    assert current["status"] == "running"
                    await read(rid)
                    assert calls == [peer, rid]
                    assert owner._leases.get_lease(active.lease_id) == active
                    record = owner._store.get(rid)
                    assert record is not None and record.reconciliation_status == "verified"
                    (run.artifacts_dir / f"admit-{kind}.json").write_text(json.dumps({
                        "error": str(caught.value), "external": current, "lease_id": active.lease_id,
                        "calls": calls, "scope": "正式Session/ASGI创建与原operation；真实PTY或Chromium/HTTP；推进本次截止，不代表真实等待30秒、Gateway/Web或Backend重启",
                    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                finally:
                    continue_admission.set()
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                    if lock.locked():
                        lock.release()
        assert not owner._leases.leases_for_resource(rid)


async def test_terminal_cwd_resolution_does_not_block_peer_http_or_change_native_target(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext], owner_mount: MountedWorkspace,
    monkeypatch: pytest.MonkeyPatch, resource_api,
) -> None:
    _client, root, run = browser_client
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context):
        owner = container.terminal_resource_owner
        target = root / "http-waiting-cwd"
        target.mkdir()
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        expired: list[bool] = []
        resolve = Path.resolve

        def waiting(path: Path, strict: bool = False) -> Path:
            if path == target:
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    expired.append(True)
                    raise TimeoutError("原生 cwd 解析阻塞了同一服务的 HTTP 查询")
            return resolve(path, strict=strict)

        proofs = []
        gateway_id = "gateway_00000000000040008000000000000099"
        uri = resource_display_uri(gateway_authority=gateway_id, scope="workspace", scope_id=container.workspace_id,
                                   kind="files", tail_segments=("root", target.name))
        async with resource_api(container, "terminal") as client:
            response = await client.post("/api/v1/resources/terminals", json={"session_id": sid, "retention_scope": "thread"})
            assert response.status_code == 200, response.text
            peer = response.json()["data"]["resource_id"]
            original = await owner._transport.get_resource(peer)
            for cwd in (target.name, str(target), uri):
                entered.clear()
                release.clear()
                before = tuple(owner._store.ids())
                with monkeypatch.context() as patch:
                    patch.setattr(Path, "resolve", waiting)
                    creating = asyncio.create_task(client.post("/api/v1/resources/terminals", json={
                        "session_id": sid, "retention_scope": "thread", "cwd_relative": cwd,
                    }, headers={"X-BoxTeam-Gateway-Id": gateway_id}))
                    try:
                        await asyncio.wait_for(entered.wait(), 7)
                        assert not expired and not creating.done()
                        peer_reply = await asyncio.wait_for(client.get(f"/api/v1/resources/{peer}"), 2)
                        assert peer_reply.status_code == 200, peer_reply.text
                        assert peer_reply.headers["X-Request-ID"] == peer_reply.json()["request_id"]
                        assert peer_reply.json()["data"]["status"] == "running"
                        assert tuple(owner._store.ids()) == before
                        assert (await owner._transport.get_resource(peer))["os_pid"] == original["os_pid"]
                        async with owner.deletion_holder.lock_for(sid, wait=False) as probe:
                            assert not probe.acquired
                        release.set()
                        response = await asyncio.wait_for(creating, 10)
                        assert response.status_code == 200, response.text
                    finally:
                        release.set()
                        await asyncio.gather(creating, return_exceptions=True)
                created = response.json()["data"]
                runtime = await owner._transport.get_resource(created["resource_id"])
                # 现有 Proto Struct 数值解为 float；PID 必须仍是精确正整数并指向真实进程。
                pid = runtime["os_pid"]
                assert isinstance(pid, (int, float)) and not isinstance(pid, bool)
                assert pid > 0 and pid == int(pid)
                assert Path(psutil.Process(int(pid)).cwd()).resolve() == target
                assert runtime["cwd_relative"] == created["metadata"]["cwd_relative"] == target.name
                assert created["source_thread"]["session_id"] == sid
                assert not owner._leases.leases_for_resource(created["resource_id"])
                assert str(root) not in owner._store.path.read_text(encoding="utf-8")
                proofs.append({"input": cwd, "resource": created, "runtime": runtime})
                # 用完经正式入口结清，本场景始终只需 peer 和当前 PTY。
                # TODO(5.4)：另验四个运行 PTY 并发 Session 清理的原 10 秒 HTTP 超时，失败原件已保留。
                detached = await client.post(f"/api/v1/resources/{created['resource_id']}/detach", json={
                    "expected_revision": created["owner_revision"], "thread": created["source_thread"],
                })
                assert detached.status_code == 200, detached.text
                detached_resource = detached.json()["data"]
                assert detached_resource["thread_associations"] == []
                deleted = await client.post(f"/api/v1/resources/{created['resource_id']}/delete", json={
                    "expected_revision": detached_resource["owner_revision"],
                })
                assert deleted.status_code == 200, deleted.text
                assert deleted.json()["data"]["resource_id"] == created["resource_id"]
                assert deleted.json()["data"]["status"] == "deleted"
                assert deleted.json()["data"]["reconciliation_status"] == "verified"
        (run.artifacts_dir / "cwd-worker.json").write_text(json.dumps({
            "peer": original, "created": proofs,
            "scope": "ASGI Session资源入口、真实Node HTTP及PTY；慢解析时同服务详情可读，三种输入均在同一原生目录执行；非Gateway/Web或进程重启验收",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
