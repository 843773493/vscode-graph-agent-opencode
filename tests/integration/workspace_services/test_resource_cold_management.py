"""冷恢复取消后，经原管理API关闭仍在等待context的真实脚本。"""

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from pathlib import Path

import pytest

from app.container import WorkspaceContainerRegistry
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.path_utils import catalog_provider_scope
from app.services.business.workspace_resources.recovery import RecoveryVersion
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
async def test_cold_peer_recovers_while_first_real_listing_receipt_is_pending(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext], owner_mount: MountedWorkspace,
    resource_api, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    _client, root, run = browser_client
    async with (
        workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context),
        resource_api(container, kind) as api,
    ):
        created = []
        for index in range(2):
            response = await api.post(f"/api/v1/resources/{kind}s", json={
                "session_id": sid, "retention_scope": "workspace", "title": f"cold-peer-{index}",
            })
            assert response.status_code == 200, response.text
            created.append(response.json()["data"])
    # 经原服务图退出后冷建owner；关键资源均由正式创建入口生成。
    with (
        WorkspaceContainerRegistry(
            mounted_workspaces=MountedWorkspaceRegistry([owner_mount]), project_root=Path.cwd(),
        ) as registry,
        catalog_provider_scope(registry),
        ExitStack() as cleanup,
    ):
        container = registry.get(owner_mount.workspace_id)
        cleanup.callback(container.workspace_activity_service.store.close)
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        rid, peer = (resource["resource_id"] for resource in created)
        originals = [await owner._transport.get_resource(resource_id) for resource_id in (rid, peer)]
        frozen: dict[str, int] = {}
        for resource_id, original in zip((rid, peer), originals, strict=True):
            revision = original["transport_owner_revision"]
            assert type(revision) is int
            record = owner._store.get(resource_id)
            assert record is not None and record.owner_revision == revision
            frozen[resource_id] = revision + 1
        entered, release = asyncio.Event(), asyncio.Event()
        listing, sync = owner._transport.list_resources, owner._transport.sync_owner_revision
        revisions: list[tuple[str, int]] = []

        async def receiving():
            snapshot = await listing()
            task = asyncio.current_task()
            assert task is not None
            if task.get_name() == "slow-initial-resource-listing":
                entered.set()
                await release.wait()
            return snapshot

        async def syncing(resource_id: str, revision: int, *, for_stop: bool) -> None:
            revisions.append((resource_id, revision))
            await sync(resource_id, revision, for_stop=for_stop)

        with monkeypatch.context() as patch:
            patch.setattr(owner._transport, "list_resources", receiving)
            patch.setattr(owner._transport, "sync_owner_revision", syncing)
            async with resource_api(container, kind) as api:
                reading = asyncio.create_task(api.get(f"/api/v1/resources/{rid}", headers={
                    "X-Request-ID": "slow-initial-resource-listing",
                }), name="slow-initial-resource-listing")
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    independent = await asyncio.wait_for(api.get(f"/api/v1/resources/{peer}", headers={
                        "X-Request-ID": "independent-cold-peer",
                    }), 3)
                    assert not reading.done()
                    assert independent.status_code == 200, independent.text
                    assert independent.headers["X-Request-ID"] == "independent-cold-peer"
                    assert independent.json()["request_id"] == "independent-cold-peer"
                    assert independent.json()["data"]["resource_id"] == peer
                    assert independent.json()["data"]["reconciliation_status"] == "verified"
                    assert independent.json()["data"]["owner_revision"] == frozen[peer]
                    assert owner._recovery._pending == {rid: RecoveryVersion(frozen[rid])}
                    peer_record = owner._store.get(peer)
                    release.set()
                    response = await asyncio.wait_for(reading, 5)
                    assert response.status_code == 200, response.text
                    assert response.headers["X-Request-ID"] == "slow-initial-resource-listing"
                    assert response.json()["request_id"] == "slow-initial-resource-listing"
                    assert response.json()["data"]["resource_id"] == rid
                    assert response.json()["data"]["owner_revision"] == frozen[rid]
                    assert response.json()["data"]["reconciliation_status"] == "verified"
                    assert owner._store.get(peer) == peer_record
                    assert revisions == [(peer, frozen[peer]), (rid, frozen[rid])]
                    assert owner._recovery._pending == {}
                    for resource_id, original in zip((rid, peer), originals, strict=True):
                        runtime = await owner._transport.get_resource(resource_id)
                        assert runtime["transport_owner_revision"] == frozen[resource_id]
                        assert runtime["status"] == original["status"]
                        identity_key = "os_pid" if kind == "terminal" else "active_page_id"
                        assert runtime[identity_key] == original[identity_key]
                        assert runtime[identity_key] is not None
                        assert not ExternalResourceLeaseLedger(
                            state_path=root / ".boxteam/resources.json",
                        ).leases_for_resource(resource_id)
                    run.write_result("passed", {
                        "kind": kind, "revisions": revisions,
                        "scope": "正式Session/资源API、冷owner及真实Node/PTY或Chromium/HTTP；仅暂停首次真实清单回执",
                    })
                finally:
                    release.set()
                    reading.cancel()
                    await asyncio.gather(reading, return_exceptions=True)


@pytest.mark.parametrize("action", ["stop", "delete"])
async def test_cold_management_can_close_context_after_cancelled_recovery(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext], owner_mount: MountedWorkspace,
    resource_api, action: str,
) -> None:
    client, root, run = browser_client
    started_marker, stopped_marker = "cold-script-started", "cold-script-context-closed"

    def script_events() -> str:
        return "\n".join(path.read_text() for path in run.artifacts_dir.glob("browser-backend-*.stderr.log"))

    async with (
        workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context),
        resource_api(container, "browser") as api,
    ):
        resources = []
        for _ in range(2):
            created = await api.post("/api/v1/resources/browsers", json={
                "session_id": sid, "retention_scope": "workspace", "url": "about:blank",
            })
            assert created.status_code == 200, created.text
            resources.append(created.json()["data"])
        first, peer = resources
        rid = first["resource_id"]
        detached = await api.post(f"/api/v1/resources/{rid}/detach", json={
            "expected_revision": first["owner_revision"], "thread": first["source_thread"],
        })
        assert detached.status_code == 200, detached.text
        operation = await api.post(f"/api/v1/resources/{rid}/browser-operation", json={
            "expected_revision": detached.json()["data"]["owner_revision"],
            "action": "run", "payload": {
                "code": (
                    f"console.error({json.dumps(started_marker)}); "
                    "await new Promise(resolve => context.once('close', resolve)); "
                    f"console.error({json.dumps(stopped_marker)}); return 'closed';"
                ),
                "timeoutMs": 1000,
            },
        })
        assert operation.status_code == 408, operation.text
        assert "BrowserRequestError" in operation.text
        original = await client.get_resource(rid)
        assert script_events().count(started_marker) == 1
        assert script_events().count(stopped_marker) == 0
    # 原服务图关闭后冷建owner，不启动周期worker或手填资源/队列状态。
    with (
        WorkspaceContainerRegistry(
            mounted_workspaces=MountedWorkspaceRegistry([owner_mount]), project_root=Path.cwd(),
        ) as registry,
        catalog_provider_scope(registry),
        ExitStack() as cleanup,
    ):
        container = registry.get(owner_mount.workspace_id)
        cleanup.callback(container.workspace_activity_service.store.close)
        owner = container.browser_resource_owner
        original_revision = original["transport_owner_revision"]
        assert isinstance(original_revision, (int, float))
        revision = int(original_revision) + 1
        async with resource_api(container, "browser") as api:
            reading = asyncio.create_task(api.get(f"/api/v1/resources/{rid}"))
            try:
                async with asyncio.timeout(4):
                    while True:
                        runtime = await client.get_resource(rid)
                        if runtime["transport_owner_revision"] == revision:
                            break
                        await asyncio.sleep(0.02)
                assert runtime["status"] == "running"
                assert script_events().count(stopped_marker) == 0
                assert not reading.done()
                reading.cancel()
                await asyncio.gather(reading, return_exceptions=True)
                assert reading.cancelled()
                assert owner._recovery._pending is not None and owner._recovery._pending[rid] == RecoveryVersion(revision)
                unknown = owner._store.get(rid)
                assert unknown is not None and unknown.reconciliation_status == "unknown"
                independent = await api.get(f"/api/v1/resources/{peer['resource_id']}")
                assert independent.status_code == 200, independent.text
                assert independent.json()["data"]["reconciliation_status"] == "verified"
                stopped = await api.post(f"/api/v1/resources/{rid}/{action}", json={
                    "expected_revision": revision,
                }, headers={"X-Request-ID": "cold-management-stop"})
                assert stopped.status_code == 200, stopped.text
                assert stopped.headers["X-Request-ID"] == "cold-management-stop"
                assert stopped.json()["request_id"] == "cold-management-stop"
                result = stopped.json()["data"]
                assert result["resource_id"] == rid
                assert result["status"] == ("closed" if action == "stop" else "deleted")
                assert result["reconciliation_status"] == "verified"
                assert result["owner_revision"] == revision + 1
                assert rid not in owner._recovery._pending
                verified = await client.get_resource(rid)
                assert verified["status"] == result["status"]
                assert verified["transport_owner_revision"] == result["owner_revision"]
                assert verified["active_operation_count"] == 0
                assert verified["reconciliation_status"] == "verified"
                assert script_events().count(started_marker) == 1
                assert script_events().count(stopped_marker) == 1
                assert not owner._leases.leases_for_resource(rid)
                stored = json.loads(owner._store.path.read_text())
                assert str(root) not in json.dumps(stored)
                run.write_result("passed", {
                    "action": action, "resource_id": rid, "frozen_revision": revision,
                    "final_revision": result["owner_revision"],
                    "scope": "正式Session/ASGI创建、执行、冷owner与管理入口，真实Node/Chromium/HTTP；不代表Backend硬重启、Gateway/Web或任意脚本强制终止",
                })
            finally:
                reading.cancel()
                await asyncio.gather(reading, return_exceptions=True)
                # 失败时仍由原外部管理关闭放行脚本，再让原Python owner核实。
                await client.stop_resource(rid)
                for resource_id in (rid, peer["resource_id"]):
                    current = owner._store.get(resource_id)
                    assert current is not None
                    if current.status != "deleted":
                        await owner.command(
                            resource_id, action="delete", expected_revision=current.owner_revision,
                            holder_id=f"request:cleanup:{resource_id}", operation_id="cleanup",
                        )
