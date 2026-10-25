"""真实资源整组释放保留全量检查，独立推进并沿原身份核实丢失回执。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.api.deps import (
    get_browser_resource_owner,
    get_terminal_resource_owner,
    get_workspace_resource_owner,
    verify_local_token,
)
from app.api.resources import router
from app.core.mounted_workspaces import MountedWorkspace
from app.core.trace_middleware import TraceMiddleware
from app.services.business.workspace_resources import owner as owner_module
from app.services.business.workspace_resources.owner import ResourceConflict
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
from tests.support.workspace_tool_runtime import workspace_tool_runtime


@pytest.fixture
def resource_api():
    def client(container, kind: str) -> httpx.AsyncClient:
        app = FastAPI()
        app.add_middleware(TraceMiddleware)
        app.include_router(router, prefix="/api/v1")
        app.dependency_overrides[verify_local_token] = lambda: "authenticated-test"
        app.dependency_overrides[get_terminal_resource_owner] = lambda: container.terminal_resource_owner
        app.dependency_overrides[get_browser_resource_owner] = lambda: container.browser_resource_owner
        app.dependency_overrides[get_workspace_resource_owner] = lambda: (
            container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        )
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://workspace.test")
    return client


@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_real_release_preflights_then_unlocks_peer_and_reconciles_original_delete(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
    owner_mount: MountedWorkspace, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    _browser_client, root, run = browser_client
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, session_id, _context):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        resources = []
        for number in range(2):
            arguments = {
                "session_id": session_id, "retention_scope": "thread",
                "holder_id": f"request:release-create:{number}", "operation_id": f"release-create:{number}",
            }
            if kind == "terminal":
                resources.append(await owner.create_terminal(**arguments))
            else:
                resources.append(await owner.create_browser(**arguments, url="about:blank"))
        rid, peer = (str(item["resource_id"]) for item in resources)
        thread_id = container.session_catalog.resolver.main_thread_id(session_id)

        async def releasing() -> int:
            return await owner.cleanup_session(session_id)

        async with owner.operation(session_id=session_id, thread_id=thread_id,
                                   holder_id="request:preflight-holder", operation_id="preflight-holder") as operation:
            if kind == "terminal":
                await operation.read_terminal(peer)
            else:
                await operation.perform(peer, "navigate", {"url": "about:blank"})
            before = [owner._store.get(resource_id) for resource_id in (rid, peer)]
            ledger = owner._leases._state_path.read_bytes()
            with pytest.raises(ResourceConflict, match="未结清"):
                await releasing()
            assert [owner._store.get(resource_id) for resource_id in (rid, peer)] == before
            assert owner._leases._state_path.read_bytes() == ledger
        revision_entered, revision_release = asyncio.Event(), asyncio.Event()
        delete_entered, delete_release, peer_deleted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_sync, original_delete = owner._transport.sync_owner_revision, owner._transport.delete_resource
        deadlines: list[asyncio.Timeout] = []
        deletes: list[str] = []

        def capture(seconds: float) -> asyncio.Timeout:
            assert seconds == 30
            context = asyncio.timeout(seconds)
            if asyncio.current_task().get_name() == "release-sweep-under-test":
                deadlines.append(context)
            return context

        async def syncing(resource_id: str, revision: int, *, for_stop: bool) -> None:
            await original_sync(resource_id, revision, for_stop=for_stop)
            if resource_id == rid and for_stop:
                revision_entered.set()
                await revision_release.wait()

        async def deleting(resource_id: str) -> dict[str, object]:
            deletes.append(resource_id)
            receipt = await original_delete(resource_id)
            if resource_id == rid:
                delete_entered.set()
                await delete_release.wait()
            else:
                peer_deleted.set()
            return receipt

        monkeypatch.setattr(owner_module, "timeout", capture, raising=False)
        monkeypatch.setattr(owner._transport, "sync_owner_revision", syncing)
        monkeypatch.setattr(owner._transport, "delete_resource", deleting)
        sweeping = asyncio.create_task(releasing(), name="release-sweep-under-test")
        try:
            await asyncio.wait_for(revision_entered.wait(), 5)
            await asyncio.wait_for(peer_deleted.wait(), 5)
            assert not sweeping.done()
            current = await owner.get_resource(peer)
            assert current["status"] == "deleted" and current["reconciliation_status"] == "verified"
            assert len(deadlines) == 1
            deadline = deadlines[0].when()
            revision_release.set()
            await asyncio.wait_for(delete_entered.wait(), 5)
            assert len(deadlines) == 1 and deadlines[0].when() == deadline
            pending, = owner._leases.leases_for_resource(rid)
            assert pending.status == "active"
            deadlines[0].reschedule(asyncio.get_running_loop().time())
            with pytest.raises(TimeoutError, match="关联释放等待超过 30 秒"):
                await asyncio.wait_for(sweeping, 5)
            saved, = owner._leases.leases_for_resource(rid)
            record = owner._store.get(rid)
            assert saved.lease_id == pending.lease_id and saved.status == "reconcile_required"
            assert record.status == "deleting" and record.reconciliation_status == "unknown"
            assert record.owner_revision == 2
            external = await owner._transport.get_resource(rid)
            assert external["status"] == "deleted" and external["reconciliation_status"] == "verified"
            assert external["transport_owner_revision"] == 2
            stored = json.loads((root / f".boxteam/{kind}-manager/{kind}s.json").read_text())
            persisted = {item[f"{kind}_id"]: item for item in stored[f"{kind}s"]}
            assert set(persisted) == {rid, peer}
            assert all(item["status"] == "deleted" and item["reconciliation_status"] == "verified"
                       and item["transport_owner_revision"] == 2 for item in persisted.values())
            assert deletes.count(rid) == deletes.count(peer) == 1
            delete_release.set()
            await releasing()
            reconciled = await owner.get_resource(rid)
            assert reconciled["status"] == "deleted" and reconciled["owner_revision"] == 2
            assert owner._leases.get_lease(pending.lease_id).status == "settled"
            assert not owner._leases.leases_for_resource(rid)
            assert deletes.count(rid) == deletes.count(peer) == 1
            run.write_result("passed", {
                "kind": kind, "resource_id": rid, "peer_id": peer, "owner_revision": 2, "entry": "session-resource-drain",
                "lease_id": pending.lease_id, "delete_calls": deletes, "budget_seconds": 30,
                "scope": "正式Session/创建/operation及原Session关联释放重试，真实Node/PTY或Chromium/HTTP；外置推进原截止并暂停真实回执，不代表不合作进程终止、Gateway/Web/Windows或完整shutdown",
            })
        finally:
            revision_release.set()
            delete_release.set()
            await asyncio.gather(sweeping, return_exceptions=True)


@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_real_operation_close_deadline_preserves_live_resource_and_original_lease_for_retry(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
    owner_mount: MountedWorkspace, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    _client, root, run = browser_client
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, context):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        resources = []
        for index in range(2):
            options = {"session_id": sid, "retention_scope": "thread",
                       "holder_id": f"request:close-budget-create:{index}", "operation_id": f"close-budget-create:{index}"}
            resource = (await owner.create_terminal(**options) if kind == "terminal"
                        else await owner.create_browser(**options, url="about:blank"))
            resources.append(str(resource["resource_id"]))
        rid, peer = resources
        async with owner.operation(session_id=sid, thread_id=context.thread_binding.thread_id,
                                   holder_id="request:real-close-budget", operation_id="real-close-budget") as operation:
            for resource_id in resources:
                if kind == "terminal":
                    await operation.read_terminal(resource_id)
                else:
                    await operation.perform(resource_id, "navigate", {"url": "about:blank"})
            leases = {resource_id: owner._leases.leases_for_resource(resource_id)[0] for resource_id in resources}
            records = {resource_id: owner._store.get(resource_id) for resource_id in resources}
            external = {resource_id: await owner._transport.get_resource(resource_id) for resource_id in resources}
            loop = asyncio.get_running_loop()
            original_time = loop.time
            offset = 0
            async with owner._resource_lock(rid):
                with monkeypatch.context() as patch:
                    patch.setattr(loop, "time", lambda: original_time() + offset)
                    closing = asyncio.create_task(operation.close())
                    try:
                        async with asyncio.timeout(5):
                            while owner._leases.get_lease(leases[peer].lease_id).status != "released":
                                await asyncio.sleep(0)
                        assert not closing.done()
                        offset = 31
                        with pytest.raises(TimeoutError, match="总截止时间") as caught:
                            await asyncio.wait_for(asyncio.shield(closing), 5)
                        assert "operation_id=real-close-budget" in str(caught.value)
                        assert rid in str(caught.value) and str(root) not in str(caught.value)
                        assert operation._lease_refs == {rid: leases[rid].lease_id}
                        cold = ExternalResourceLeaseLedger(state_path=owner._leases._state_path)
                        assert cold.get_lease(leases[rid].lease_id).status == "active"
                        assert cold.get_lease(leases[peer].lease_id).status == "released"
                    finally:
                        closing.cancel()
                        await asyncio.gather(closing, return_exceptions=True)
            # 到期只结束本次锁等待，不停止真实PTY/页面，也不在解锁后偷偷释放。
            await asyncio.sleep(0)
            assert operation._lease_refs == {rid: leases[rid].lease_id}
            for resource_id in resources:
                assert owner._store.get(resource_id) == records[resource_id]
                current = await owner._transport.get_resource(resource_id)
                assert current["status"] == external[resource_id]["status"] == "running"
                assert current["transport_owner_revision"] == external[resource_id]["transport_owner_revision"]
                identity_field = "os_pid" if kind == "terminal" else "active_page_id"
                assert current[identity_field] == external[resource_id][identity_field]
            await operation.close()
            await operation.close()
            assert operation._lease_refs == {}
            assert all(owner._leases.get_lease(lease.lease_id).status == "released" for lease in leases.values())
            run.write_result("passed", {
                "entry": "resource-operation-close", "kind": kind, "resource_id": rid, "peer_id": peer,
                "original_lease_id": leases[rid].lease_id, "budget_seconds": 30,
                "scope": "正式Session/资源/operation，真实Node/PTY或Chromium/HTTP；仅推进本进程单调时间和占用原锁，不代表外部终止或完整shutdown验收",
            })


@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("stage", ["lock", "receipt"])
async def test_real_management_command_total_deadline_preserves_facts_and_reconciles_without_replay(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext], owner_mount: MountedWorkspace,
    monkeypatch: pytest.MonkeyPatch, resource_api, kind: str, stage: str,
) -> None:
    _client, root, run = browser_client
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        async with resource_api(container, kind) as client:
            created = []
            for index in range(2):
                response = await client.post(f"/api/v1/resources/{kind}s", json={
                    "session_id": sid, "retention_scope": "thread", "title": f"command-{index}",
                })
                assert response.status_code == 200, response.text
                assert response.json()["request_id"] == response.headers["X-Request-ID"]
                created.append(response.json()["data"]["resource_id"])
            rid, peer = created
            original_record = owner._store.get(rid)
            original_external = await owner._transport.get_resource(rid)
            entered, release = asyncio.Event(), asyncio.Event()
            recover = owner.recover
            sync = owner._transport.sync_owner_revision
            stop = owner._transport.stop_resource
            lock = owner._resource_lock(rid)
            stops = []
            deadlines: list[asyncio.Timeout] = []
            sync_deadlines = []
            acquired = False

            def is_command() -> bool:
                return asyncio.current_task().get_name() == "management-command-under-test"

            def capture(seconds: float) -> asyncio.Timeout:
                assert seconds == 30
                context = asyncio.timeout(seconds)
                if is_command():
                    deadlines.append(context)
                return context

            async def recover_then_lock(resource_ids=None):
                nonlocal acquired
                await recover(resource_ids)
                if is_command() and resource_ids == (rid,):
                    await lock.acquire()
                    acquired = True
                    entered.set()

            async def syncing(resource_id: str, revision: int, *, for_stop: bool) -> None:
                if is_command():
                    sync_deadlines.append(deadlines[0].when())
                await sync(resource_id, revision, for_stop=for_stop)

            async def stopping(resource_id: str, *, reason: str | None = None):
                stops.append(resource_id)
                receipt = await stop(resource_id, reason=reason)
                if resource_id == rid and stage == "receipt":
                    entered.set()
                    await release.wait()
                return receipt

            monkeypatch.setattr(owner._transport, "stop_resource", stopping)
            with monkeypatch.context() as patch:
                patch.setattr(owner_module, "timeout", capture)
                if stage == "lock":
                    patch.setattr(owner, "recover", recover_then_lock)
                else:
                    patch.setattr(owner._transport, "sync_owner_revision", syncing)
                pending = asyncio.create_task(client.post(
                    f"/api/v1/resources/{rid}/stop", json={"expected_revision": 1},
                    headers={"X-Request-ID": "command-under-test"},
                ), name="management-command-under-test")
                try:
                    await asyncio.wait_for(entered.wait(), 10)
                    healthy = await client.get(f"/api/v1/resources/{peer}")
                    assert healthy.status_code == 200 and healthy.json()["data"]["status"] == "running"
                    if stage == "receipt":
                        lease, = owner._leases.leases_for_resource(rid)
                        assert (lease.turn_stream_id, lease.operation_id) == ("request:command-under-test", "command-under-test")
                    assert len(deadlines) == 1
                    if stage == "receipt":
                        assert sync_deadlines == [deadlines[0].when()]
                    deadlines[0].reschedule(asyncio.get_running_loop().time())
                    failed = await asyncio.wait_for(asyncio.shield(pending), 1)
                    assert failed.status_code == 503, failed.text
                    assert failed.headers["X-Request-ID"] == "command-under-test"
                    assert "TimeoutError" in failed.text and str(root) not in failed.text
                finally:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                    release.set()
                    if acquired:
                        lock.release()
            await asyncio.sleep(0)
            current = owner._store.get(rid)
            external = await owner._transport.get_resource(rid)
            cold = ExternalResourceLeaseLedger(state_path=owner._leases._state_path)
            if stage == "lock":
                assert current == original_record and not stops
                assert not cold.leases_for_resource(rid)
                identity = "os_pid" if kind == "terminal" else "active_page_id"
                assert external[identity] == original_external[identity]
                assert external["status"] == "running" and external["transport_owner_revision"] == 1
            else:
                assert current.status == "stopping" and current.reconciliation_status == "unknown"
                assert current.owner_revision == external["transport_owner_revision"] == 2
                assert external["status"] in {"closed", "terminated"}
                assert external["reconciliation_status"] == "verified"
                assert cold.get_lease(lease.lease_id).status == "reconcile_required"
                assert stops == [rid]
            retried = await client.post(f"/api/v1/resources/{rid}/stop", json={
                "expected_revision": current.owner_revision,
            })
            assert retried.status_code == 200, retried.text
            assert retried.json()["request_id"] == retried.headers["X-Request-ID"]
            assert retried.json()["data"]["reconciliation_status"] == "verified"
            assert not owner._leases.leases_for_resource(rid)
            if stage == "receipt":
                assert stops == [rid] and owner._leases.get_lease(lease.lease_id).status == "settled"
            else:
                assert stops == [rid]
            (run.artifacts_dir / f"command-deadline-{kind}-{stage}.json").write_text(json.dumps({
                "failure": failed.json(), "retry": retried.json(), "resource_id": rid,
                "original_external": original_external, "after_timeout_external": external,
                "scope": "真实Session及ASGI资源创建/管理入口，真实Node/PTY或Chromium/HTTP；暂停原回执并推进本次截止，不代表Gateway/Web或任意不合作执行的终止",
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_real_list_total_deadline_preserves_live_resources_and_peer_access(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext], owner_mount: MountedWorkspace,
    monkeypatch: pytest.MonkeyPatch, resource_api, kind: str,
) -> None:
    _client, root, run = browser_client
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        async with resource_api(container, kind) as client:
            created = []
            for index in range(2):
                response = await client.post(f"/api/v1/resources/{kind}s", json={
                    "session_id": sid, "retention_scope": "thread", "title": f"list-{index}",
                })
                assert response.status_code == 200, response.text
                created.append(response.json()["data"]["resource_id"])
            rid, peer = created
            external = await owner._transport.get_resource(rid)
            entered, release = asyncio.Event(), asyncio.Event()
            transport_list = owner._transport.list_resources
            deadlines: list[asyncio.Timeout] = []
            calls = 0

            def is_listing() -> bool:
                return asyncio.current_task().get_name() == "resource-list-under-test"

            def capture(seconds: float) -> asyncio.Timeout:
                assert seconds == 30
                context = asyncio.timeout(seconds)
                if is_listing():
                    deadlines.append(context)
                return context

            async def receiving():
                nonlocal calls
                result = await transport_list()
                if is_listing():
                    calls += 1
                    entered.set()
                    await release.wait()
                return result

            with monkeypatch.context() as patch:
                patch.setattr(owner_module, "timeout", capture)
                patch.setattr(owner._transport, "list_resources", receiving)
                pending = asyncio.create_task(client.get(
                    f"/api/v1/resources?kind={kind}",
                    headers={"X-Request-ID": "list-under-test"},
                ), name="resource-list-under-test")
                try:
                    await asyncio.wait_for(entered.wait(), 10)
                    healthy = await client.get(f"/api/v1/resources/{peer}")
                    assert healthy.status_code == 200 and healthy.json()["data"]["status"] == "running"
                    before, ledger = owner._store.path.read_bytes(), owner._leases._state_path.read_bytes()
                    assert len(deadlines) == calls == 1
                    deadlines[0].reschedule(asyncio.get_running_loop().time())
                    failed = await asyncio.wait_for(asyncio.shield(pending), 1)
                    assert failed.status_code == 503, failed.text
                    assert failed.headers["X-Request-ID"] == "list-under-test"
                    assert "TimeoutError" in failed.text and str(root) not in failed.text
                    assert owner._store.path.read_bytes() == before
                    assert owner._leases._state_path.read_bytes() == ledger
                finally:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                    release.set()
            await asyncio.sleep(0)
            assert calls == 1
            current = await owner._transport.get_resource(rid)
            identity = "os_pid" if kind == "terminal" else "active_page_id"
            assert current[identity] == external[identity]
            assert current["transport_owner_revision"] == external["transport_owner_revision"] == 1
            assert current["status"] == external["status"] == "running"
            retried = await client.get(f"/api/v1/resources?kind={kind}")
            assert retried.status_code == 200, retried.text
            assert retried.json()["request_id"] == retried.headers["X-Request-ID"]
            assert {item["resource_id"] for item in retried.json()["data"]["items"]} == set(created)
            (run.artifacts_dir / f"list-deadline-{kind}.json").write_text(json.dumps({
                "failure": failed.json(), "retry": retried.json(), "external": current,
                "scope": "正式Session及ASGI资源创建/列表，真实Node/PTY或Chromium/HTTP；暂停已返回清单并推进本次截止，不代表Gateway/Web或整个聚合API总期限",
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
