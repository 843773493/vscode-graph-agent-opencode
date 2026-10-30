"""真实资源冷恢复被取消或到期，保留此前失败及原未知占用。"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from pathlib import Path

import pytest

from app.container import WorkspaceContainerRegistry
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.path_utils import catalog_provider_scope
from app.services.business.workspace_resources import recovery
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.test_resource_cold_sync_retry import (
    browser_client as browser_client,  # noqa: PLC0414
)
from tests.integration.workspace_services.test_resource_cold_sync_retry import (
    owner_mount as owner_mount,  # noqa: PLC0414
)
from tests.integration.workspace_services.test_resource_cold_sync_retry import (
    resource_api as resource_api,  # noqa: PLC0414
)
from tests.support.workspace_tool_runtime import workspace_tool_runtime


@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("ending", ["deadline", "cancel"])
async def test_real_recovery_preserves_completed_failure_and_original_unknown_lease(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext], owner_mount: MountedWorkspace,
    resource_api, monkeypatch: pytest.MonkeyPatch, kind: str, ending: str,
) -> None:
    _client, root, run = browser_client
    async with (
        workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context),
        resource_api(container, kind) as api,
    ):
        ids: list[str] = []
        for index in range(3):
            created = await api.post(f"/api/v1/resources/{kind}s", json={
                "session_id": sid, "retention_scope": "workspace", "title": f"recovery-errors-{index}",
            })
            assert created.status_code == 200, created.text
            assert created.json()["request_id"] == created.headers["X-Request-ID"]
            ids.append(created.json()["data"]["resource_id"])
    failed, blocked, healthy = ids
    mounts = MountedWorkspaceRegistry([owner_mount])
    with (
        WorkspaceContainerRegistry(mounted_workspaces=mounts, project_root=Path.cwd()) as registry,
        catalog_provider_scope(registry), ExitStack() as cleanup,
    ):
        container = registry.get(owner_mount.workspace_id)
        cleanup.callback(container.workspace_activity_service.store.close)
        cleanup.callback(container.session_catalog.close)
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        await owner.recover(ids)
        grant = owner._transport.issue_transport_grant

        async def lost_grant(resource_id: str, *, owner_revision: int, association: str | None):
            await grant(resource_id, owner_revision=owner_revision, association=association)
            raise ConnectionError("真实授权已签发，原回执未交付")

        record = owner._store.get(failed)
        assert record is not None
        async with resource_api(container, kind) as api:
            with monkeypatch.context() as patch:
                patch.setattr(owner._transport, "issue_transport_grant", lost_grant)
                response = await api.post(f"/api/v1/resources/{failed}/transport-grant", json={
                    "expected_revision": record.owner_revision,
                }, headers={"X-Request-ID": "unknown-before-recovery-errors"})
                assert response.status_code == 503, response.text
        lease, = owner._leases.leases_for_resource(failed)
        assert lease.status == "reconcile_required" and lease.operation_id == "unknown-before-recovery-errors"

    with (
        WorkspaceContainerRegistry(mounted_workspaces=mounts, project_root=Path.cwd()) as registry,
        catalog_provider_scope(registry), ExitStack() as cleanup,
    ):
        container = registry.get(owner_mount.workspace_id)
        cleanup.callback(container.workspace_activity_service.store.close)
        cleanup.callback(container.session_catalog.close)
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        before = {rid: await owner._transport.get_resource(rid) for rid in ids}
        identity_key = "os_pid" if kind == "terminal" else "active_page_id"
        assert all(before[rid][identity_key] is not None for rid in ids)
        frozen: dict[str, int] = {}
        for rid in ids:
            revision = before[rid]["transport_owner_revision"]
            assert isinstance(revision, int)
            frozen[rid] = revision + 1
        sync = owner._transport.sync_owner_revision
        arrived, peer_done, did_fail, release = (asyncio.Event() for _ in range(4))
        original = ConnectionError("原同步已提交，Python未取得回执")
        calls: list[tuple[str, int, bool]] = []
        deadlines: list[asyncio.Timeout] = []

        def capture(seconds: float) -> asyncio.Timeout:
            assert seconds == 30
            budget = asyncio.timeout(seconds)
            if asyncio.current_task() is waiting:
                deadlines.append(budget)
            return budget

        async def syncing(resource_id: str, revision: int, *, for_stop: bool) -> None:
            calls.append((resource_id, revision, for_stop))
            await sync(resource_id, revision, for_stop=for_stop)
            if resource_id == failed:
                did_fail.set()
                raise original
            if resource_id == blocked:
                arrived.set()
                await release.wait()
            else:
                peer_done.set()

        waiting: asyncio.Task[None] | None = None
        async with resource_api(container, kind) as api:
            try:
                with monkeypatch.context() as patch:
                    patch.setattr(owner._transport, "sync_owner_revision", syncing)
                    patch.setattr(recovery, "timeout", capture)
                    waiting = asyncio.create_task(owner.recover(ids))
                    try:
                        for event in (did_fail, arrived, peer_done):
                            await asyncio.wait_for(event.wait(), 15)
                        assert len(deadlines) == 1
                        if ending == "deadline":
                            deadlines[0].reschedule(asyncio.get_running_loop().time())
                            with pytest.raises(ExceptionGroup) as caught:
                                await asyncio.wait_for(waiting, 5)
                            first_error, expiry = caught.value.exceptions
                            assert first_error is original
                            assert isinstance(expiry, TimeoutError) and "冷恢复等待超过 30 秒" in str(expiry)
                        else:
                            waiting.cancel("真实批次调用者取消")
                            with pytest.raises(asyncio.CancelledError) as cancelled:
                                await waiting
                            assert cancelled.value.args == ("真实批次调用者取消",)
                            cause = cancelled.value.__cause__
                            assert isinstance(cause, ExceptionGroup) and cause.exceptions == (original,)
                        assert not any(owner._resource_lock(rid).locked() for rid in ids)
                        pending = owner._recovery._pending
                        assert pending is not None and set(pending) == {failed, blocked}
                        assert pending[failed].revision == frozen[failed] and pending[failed].management_only
                        assert pending[blocked].revision == frozen[blocked] and not pending[blocked].management_only
                        cold = ExternalResourceLeaseLedger(state_path=root / ".boxteam/resources.json")
                        assert cold.leases_for_resource(failed) == [lease]
                        assert not cold.leases_for_resource(blocked) and not cold.leases_for_resource(healthy)
                        for rid in ids:
                            runtime = await owner._transport.get_resource(rid)
                            assert runtime[identity_key] == before[rid][identity_key]
                            assert runtime["status"] == "running" and runtime["transport_owner_revision"] == frozen[rid]
                        unknown = await api.get(f"/api/v1/resources/{failed}")
                        peer = await api.get(f"/api/v1/resources/{healthy}")
                        assert unknown.status_code == peer.status_code == 200
                        assert unknown.json()["data"]["reconciliation_status"] == "unknown"
                        assert peer.json()["data"]["reconciliation_status"] == "verified"
                        assert calls == [(rid, frozen[rid], False) for rid in ids]
                    finally:
                        release.set()
                        if not waiting.done():
                            waiting.cancel()
                        await asyncio.gather(waiting, return_exceptions=True)
                stopped = await api.post(f"/api/v1/resources/{failed}/stop", json={"expected_revision": frozen[failed]})
                assert stopped.status_code == 200, stopped.text
                assert stopped.json()["data"]["status"] == ("terminated" if kind == "terminal" else "closed")
                settled = ExternalResourceLeaseLedger(state_path=root / ".boxteam/resources.json").get_lease(lease.lease_id)
                assert settled is not None and settled.status == "settled"
                run.write_result("passed", {"kind": kind, "ending": ending, "resource_ids": ids,
                                           "lease_id": lease.lease_id, "calls": calls,
                                           "scope": "正式Session/ASGI资源与授权、冷Python owner及真实PTY/Chromium/HTTP；只暂停已提交同步的回执，不代表Backend进程重启、Gateway/Web、Windows或强制终止"})
            finally:
                for rid in ids:
                    record = owner._store.get(rid)
                    assert record is not None
                    if record.status != "deleted":
                        await owner.command(rid, action="delete", expected_revision=record.owner_revision,
                                            holder_id=f"request:cleanup:{rid}", operation_id="cleanup")
