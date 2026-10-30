"""真实资源同步失败后，经原管理API重试冻结版本并核实终态。"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

import pytest

from app.container import WorkspaceContainerRegistry
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.path_utils import catalog_provider_scope
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
@pytest.mark.parametrize("accepted", [False, True])
async def test_real_failed_cold_sync_keeps_unknown_discoverable_and_management_retryable(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext], owner_mount: MountedWorkspace,
    resource_api, monkeypatch: pytest.MonkeyPatch, kind: str, accepted: bool,
) -> None:
    _client, root, run = browser_client
    async with (
        workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context),
        resource_api(container, kind) as api,
    ):
        resources = []
        for index in range(2):
            created = await api.post(f"/api/v1/resources/{kind}s", json={
                "session_id": sid, "retention_scope": "workspace", "title": f"sync-retry-{index}",
            })
            assert created.status_code == 200, created.text
            resources.append(created.json()["data"])
    rid, peer = (resource["resource_id"] for resource in resources)
    # 原生命周期已解除workspace资源关联；冷owner通过真实授权入口生成未知占用。
    with (
        WorkspaceContainerRegistry(mounted_workspaces=MountedWorkspaceRegistry([owner_mount]), project_root=Path.cwd()) as registry,
        catalog_provider_scope(registry), ExitStack() as cleanup,
    ):
        container = registry.get(owner_mount.workspace_id)
        cleanup.callback(container.workspace_activity_service.store.close)
        cleanup.callback(container.session_catalog.close)
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        grant = owner._transport.issue_transport_grant

        async def lost_grant(resource_id: str, *, owner_revision: int, association: str | None):
            await grant(resource_id, owner_revision=owner_revision, association=association)
            raise ConnectionError("真实授权已签发，Python未取得回执")

        async with resource_api(container, kind) as api:
            current = await api.get(f"/api/v1/resources/{rid}")
            assert current.status_code == 200, current.text
            with monkeypatch.context() as patch:
                patch.setattr(owner._transport, "issue_transport_grant", lost_grant)
                response = await api.post(f"/api/v1/resources/{rid}/transport-grant", json={
                    "expected_revision": current.json()["data"]["owner_revision"],
                }, headers={"X-Request-ID": "unknown-before-cold-sync"})
                assert response.status_code == 503, response.text
        lease, = owner._leases.leases_for_resource(rid)
        assert lease.status == "reconcile_required"
        assert lease.operation_id == "unknown-before-cold-sync"

    with (
        WorkspaceContainerRegistry(mounted_workspaces=MountedWorkspaceRegistry([owner_mount]), project_root=Path.cwd()) as registry,
        catalog_provider_scope(registry), ExitStack() as cleanup,
    ):
        container = registry.get(owner_mount.workspace_id)
        cleanup.callback(container.workspace_activity_service.store.close)
        cleanup.callback(container.session_catalog.close)
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        before = owner._store.get(rid)
        assert before is not None
        frozen = before.owner_revision + 1
        original = await owner._transport.get_resource(rid)
        identity_key = "os_pid" if kind == "terminal" else "active_page_id"
        assert original[identity_key] is not None
        sync, stop, delete = owner._transport.sync_owner_revision, owner._transport.stop_resource, owner._transport.delete_resource
        calls: list[tuple[str, int, bool]] = []
        effects: list[tuple[str, str]] = []
        failures = 2

        async def syncing(resource_id: str, version: int, *, for_stop: bool) -> None:
            nonlocal failures
            calls.append((resource_id, version, for_stop))
            if resource_id == rid and failures:
                failures -= 1
                if accepted:
                    await sync(resource_id, version, for_stop=for_stop)
                raise ConnectionError("冷恢复同步回执未确认")
            await sync(resource_id, version, for_stop=for_stop)

        async def stopping(resource_id: str, *, reason: str | None = None):
            effects.append((resource_id, "stop"))
            return await stop(resource_id, reason=reason)

        async def deleting(resource_id: str):
            effects.append((resource_id, "delete"))
            return await delete(resource_id)

        async with resource_api(container, kind) as api:
            try:
                with monkeypatch.context() as patch:
                    patch.setattr(owner._transport, "sync_owner_revision", syncing)
                    patch.setattr(owner._transport, "stop_resource", stopping)
                    patch.setattr(owner._transport, "delete_resource", deleting)
                    failed = await api.get(f"/api/v1/resources/{rid}", headers={"X-Request-ID": "failed-cold-sync"})
                    assert failed.status_code == 503, failed.text
                    assert failed.headers["X-Request-ID"] == "failed-cold-sync"
                    unknown = await api.get(f"/api/v1/resources/{rid}")
                    assert unknown.status_code == 200, unknown.text
                    assert unknown.json()["data"]["reconciliation_status"] == "unknown"
                    assert unknown.json()["data"]["owner_revision"] == frozen
                    healthy = await api.get(f"/api/v1/resources/{peer}")
                    assert healthy.status_code == 200, healthy.text
                    assert healthy.json()["data"]["reconciliation_status"] == "verified"
                    initial_calls = list(calls)
                    assert [call for call in calls if call[0] == rid] == [(rid, frozen, False)]
                    runtime = await owner._transport.get_resource(rid)
                    assert runtime[identity_key] == original[identity_key]
                    assert runtime["transport_owner_revision"] == (frozen if accepted else frozen - 1)

                    action = "delete" if accepted else "stop"
                    first = await api.post(f"/api/v1/resources/{rid}/{action}", json={
                        "expected_revision": frozen,
                    }, headers={"X-Request-ID": "first-explicit-sync-retry"})
                    assert first.status_code == 503, first.text
                    assert first.headers["X-Request-ID"] == "first-explicit-sync-retry"
                    assert calls == initial_calls + [(rid, frozen, True)]
                    assert effects == []
                    cold = ExternalResourceLeaseLedger(state_path=root / ".boxteam/resources.json")
                    assert cold.leases_for_resource(rid) == [lease]
                    result = await api.post(f"/api/v1/resources/{rid}/{action}", json={
                        "expected_revision": frozen,
                    }, headers={"X-Request-ID": "verified-explicit-sync-retry"})
                    assert result.status_code == 200, result.text
                    assert result.headers["X-Request-ID"] == "verified-explicit-sync-retry"
                    assert result.json()["request_id"] == "verified-explicit-sync-retry"
                    final = result.json()["data"]
                    assert final["owner_revision"] == frozen + 1
                    assert final["reconciliation_status"] == "verified"
                    assert final["status"] == ("deleted" if accepted else "terminated" if kind == "terminal" else "closed")
                    assert calls == initial_calls + [(rid, frozen, True), (rid, frozen, True), (rid, frozen + 1, True)]
                    assert effects == [(rid, action)]
                    verified = await owner._transport.get_resource(rid)
                    assert verified["status"] == final["status"]
                    assert verified["reconciliation_status"] == "verified"
                    assert verified["transport_owner_revision"] == frozen + 1
                    settled = ExternalResourceLeaseLedger(state_path=root / ".boxteam/resources.json").get_lease(lease.lease_id)
                    assert settled is not None and settled.status == "settled"
                    assert not owner._leases.leases_for_resource(rid)
                    assert (await api.get(f"/api/v1/resources/{peer}")).json()["data"] == healthy.json()["data"]
                    run.write_result("passed", {"kind": kind, "accepted": accepted, "calls": calls, "effects": effects,
                                               "lease_id": lease.lease_id, "final": final})
            finally:
                for resource_id in (rid, peer):
                    record = owner._store.get(resource_id)
                    assert record is not None
                    if record.status != "deleted":
                        await owner.command(resource_id, action="delete", expected_revision=record.owner_revision,
                                            holder_id=f"request:cleanup:{resource_id}", operation_id="cleanup")
