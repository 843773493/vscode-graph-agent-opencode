"""真实资源的记录发布在 worker 结算，取消不提前释放原管理锁或占用。"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.core.trace_middleware import get_current_request_id
from app.services.business.workspace_resources.records import ResourceRecordStore
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
from tests.integration.workspace_services.test_resource_record_errors import (
    resource_workspace as resource_workspace,  # noqa: PLC0414
)
from tests.integration.workspace_services.test_resource_release import (
    resource_api as resource_api,  # noqa: PLC0414
)
from tests.support.workspace_tool_runtime import workspace_tool_runtime


@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("cancel", [False, True], ids=["complete", "cancel"])
async def test_native_record_write_preserves_real_identity_and_original_request_lifetime(
    resource_workspace: tuple[BrowserManagerClient, Path, TestRunContext],
    owner_mount: MountedWorkspace, monkeypatch: pytest.MonkeyPatch, resource_api, kind: str, cancel: bool,
) -> None:
    _browser, root, run = resource_workspace
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        async with resource_api(container, kind) as client:
            resources = []
            for number in range(2):
                response = await client.post(f"/api/v1/resources/{kind}s", json={"session_id": sid, "title": f"原资源{number}"})
                assert response.status_code == 200, response.text
                resources.append(response.json()["data"])
            rid, peer = (item["resource_id"] for item in resources)
            identity = "os_pid" if kind == "terminal" else "active_page_id"
            external = [await owner._transport.get_resource(item["resource_id"]) for item in resources]
            pending = owner._store.path.with_suffix(".tmp")
            entered, release = asyncio.Event(), threading.Event()
            loop, event_thread = asyncio.get_running_loop(), threading.get_ident()
            writers: list[int] = []
            replace = Path.replace
            request_id = f"record-write-{kind}-{cancel}"

            def publishing(path: Path, target: Path) -> Path:
                # worker 继承正式请求上下文，后台观察不能冒充本次管理写入。
                if path == pending and get_current_request_id() == request_id:
                    writers.append(threading.get_ident())
                    if len(writers) == 1:
                        loop.call_soon_threadsafe(entered.set)
                        if not release.wait(5):
                            raise TimeoutError("测试原生发布屏障未释放")
                return replace(path, target)

            with monkeypatch.context() as patch:
                patch.setattr(Path, "replace", publishing)
                command = asyncio.create_task(client.post(f"/api/v1/resources/{rid}/promote", json={"expected_revision": 1},
                                                         headers={"X-Request-ID": request_id}))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    assert not command.done() and owner._resource_lock(rid).locked()
                    assert writers == [writers[0]] and writers[0] != event_thread
                    original = owner._store.path.read_bytes()
                    record = owner._store.get(rid)
                    assert record is not None and record.owner_revision == 1 and record.retention_scope == "thread"
                    # 无关资源的正式拒绝入口仍能响应，不伪造健康接口或后台业务状态。
                    refused = await asyncio.wait_for(client.post(f"/api/v1/resources/{peer}/promote",
                                                                json={"expected_revision": 999}), 2)
                    assert refused.status_code == 409, refused.text
                    assert refused.headers["X-Request-ID"] == refused.json()["request_id"]
                    assert owner._store.path.read_bytes() == original
                    lease, = owner._leases.leases_for_resource(rid)
                    assert lease.status == "active"
                    for index, item in enumerate(resources):
                        current = await owner._transport.get_resource(item["resource_id"])
                        assert current[identity] == external[index][identity]
                        assert current["transport_owner_revision"] == 1 and current["status"] == external[index]["status"]
                    if cancel:
                        command.cancel("原生写入期间取消HTTP请求")
                        await asyncio.sleep(0)
                        assert not command.done() and owner._resource_lock(rid).locked()
                        assert owner._leases.get_lease(lease.lease_id) == lease
                    release.set()
                    if cancel:
                        with pytest.raises(asyncio.CancelledError):
                            await asyncio.wait_for(command, 5)
                        record = owner._store.get(rid)
                        assert record is not None and record.owner_revision == 2 and record.reconciliation_status == "unknown"
                        cold_ledger = ExternalResourceLeaseLedger(state_path=owner._leases._state_path)
                        saved = cold_ledger.get_lease(lease.lease_id)
                        assert saved is not None and saved.status == "reconcile_required"
                    else:
                        result = await asyncio.wait_for(command, 5)
                        assert result.status_code == 200, result.text
                        assert result.headers["X-Request-ID"] == result.json()["request_id"] == request_id
                        assert result.json()["data"]["owner_revision"] == 2
                        assert result.json()["data"]["retention_scope"] == "workspace"
                        saved = owner._leases.get_lease(lease.lease_id)
                        assert saved is not None and saved.status == "released"
                finally:
                    release.set()
                    await asyncio.gather(command, return_exceptions=True)
            assert not owner._resource_lock(rid).locked()
            for item, revision in zip(resources, (2, 1), strict=True):
                stopped = await client.post(f"/api/v1/resources/{item['resource_id']}/stop", json={"expected_revision": revision})
                assert stopped.status_code == 200, stopped.text
                state = stopped.json()["data"]
                assert state["reconciliation_status"] == "verified" and state["status"] in {"terminated", "closed"}
                actual = await owner._transport.get_resource(item["resource_id"])
                assert actual["status"] == state["status"] and actual["reconciliation_status"] == "verified"
                assert actual["transport_owner_revision"] == revision + 1
            if cancel:
                saved = owner._leases.get_lease(lease.lease_id)
                assert saved is not None and saved.status == "settled"
            payload = json.loads(owner._store.path.read_bytes())
            assert {record["resource_id"] for record in payload["records"]} == {rid, peer}
            run.write_result("passed", {"kind": kind, "cancelled": cancel, "resource_id": rid,
                                       "peer_resource_id": peer, "original_lease_id": lease.lease_id,
                                       "writer_threads": writers, "event_loop_thread": event_thread,
                                       "scope": "Session/ASGI资源入口、真实Node与PTY/Chromium；非Gateway/Web、OS重启或原生I/O抢占"})


@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_cancelled_initial_intent_is_closed_without_dispatching_a_real_resource(
    resource_workspace: tuple[BrowserManagerClient, Path, TestRunContext],
    owner_mount: MountedWorkspace, monkeypatch: pytest.MonkeyPatch, resource_api, kind: str,
) -> None:
    _browser, root, run = resource_workspace
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        async with resource_api(container, kind) as client:
            endpoint = f"/api/v1/resources/{kind}s"
            created = await client.post(endpoint, json={"session_id": sid, "title": "原生健康资源"})
            assert created.status_code == 200, created.text
            peer = created.json()["data"]["resource_id"]
            external = await owner._transport.get_resource(peer)
            pending = owner._store.path.with_suffix(".tmp")
            entered, release = asyncio.Event(), threading.Event()
            loop = asyncio.get_running_loop()
            replace = Path.replace
            request_id = f"unstarted-creation-{kind}"
            writes = 0

            def publishing(path: Path, target: Path) -> Path:
                nonlocal writes
                if path == pending and get_current_request_id() == request_id:
                    writes += 1
                    if writes == 1:
                        loop.call_soon_threadsafe(entered.set)
                        if not release.wait(5):
                            raise TimeoutError("测试首条 intent 写入屏障未释放")
                return replace(path, target)

            with monkeypatch.context() as patch:
                patch.setattr(Path, "replace", publishing)
                creating = asyncio.create_task(client.post(endpoint, json={"session_id": sid, "title": "不派发的创建"},
                                                          headers={"X-Request-ID": request_id}))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    rid, = set(owner._resource_locks) - {peer}
                    assert owner._resource_locks[rid].locked() and owner._store.get(rid) is None
                    creating.cancel("首条原生 intent 取消")
                    await asyncio.sleep(0)
                    assert not creating.done() and owner._resource_locks[rid].locked()
                    assert owner._leases.get(rid) is None
                    release.set()
                    with pytest.raises(asyncio.CancelledError, match="首条原生 intent 取消"):
                        await asyncio.wait_for(creating, 5)
                finally:
                    release.set()
                    await asyncio.gather(creating, return_exceptions=True)
            record = owner._store.get(rid)
            assert record is not None and record.status == "deleted" and record.reconciliation_status == "verified"
            assert record.owner_revision == 1 and not record.thread_associations
            assert not owner._resource_lock(rid).locked() and not owner._leases.leases_for_resource(rid)
            listing = await client.get("/api/v1/resources", params={"kind": kind})
            assert listing.status_code == 200, listing.text
            assert {row["resource_id"] for row in listing.json()["data"]["items"]} == {peer}
            detail = await client.get(f"/api/v1/resources/{rid}")
            assert detail.status_code == 200, detail.text
            assert detail.headers["X-Request-ID"] == detail.json()["request_id"]
            assert detail.json()["data"]["status"] == "deleted"
            assert detail.json()["data"]["reconciliation_status"] == "verified"
            assert [item[f"{kind}_id"] for item in await owner._transport.list_resources()] == [peer]
            identity = "os_pid" if kind == "terminal" else "active_page_id"
            current = await owner._transport.get_resource(peer)
            assert current[identity] == external[identity] and current["transport_owner_revision"] == 1
            cold_store = ResourceRecordStore(workspace_root=root, workspace_id=owner.workspace_id, kind=owner.kind)
            assert cold_store.get(rid) == record
            cold_ledger = ExternalResourceLeaseLedger(state_path=owner._leases._state_path)
            registered = cold_ledger.get(rid)
            assert registered is not None and registered.status == "stopped"
            assert not cold_ledger.leases_for_resource(rid)
            stopped = await client.post(f"/api/v1/resources/{peer}/stop", json={"expected_revision": 1})
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["data"]["status"] in {"terminated", "closed"}
            run.write_result("passed", {"kind": kind, "unstarted_resource_id": rid, "peer_resource_id": peer,
                                       "writes": writes, "scope": "正式Session/ASGI、真实Node/PTY/Chromium与存储冷读；非Backend重启、Gateway/Web或旧intent接管"})
