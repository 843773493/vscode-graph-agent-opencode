"""真实Browser清单等待的锁隔离与迟到观察冲突。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.services.business.workspace_resources.owner import ResourceConflict
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.browser.test_browser_owner_revision_drain import (
    browser_client as browser_client,  # noqa: PLC0414
)
from tests.integration.workspace_services.browser.test_browser_owner_revision_drain import (
    owner_mount as owner_mount,  # noqa: PLC0414
)
from tests.support.workspace_tool_runtime import workspace_tool_runtime


@pytest.mark.parametrize("transition", ["stop", "external_stop"])
async def test_slow_browser_list_keeps_peer_usable_and_rejects_stale_observation(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
    owner_mount: MountedWorkspace, monkeypatch: pytest.MonkeyPatch, transition: str,
) -> None:
    client, root, run = browser_client
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, session_id, _context):
        owner = container.browser_resource_owner
        resources = [await owner.create_browser(
            session_id=session_id, retention_scope="workspace", url="about:blank",
            holder_id=f"request:list-create:{number}", operation_id=f"list-create:{number}",
        ) for number in range(2)]
        rid, peer = (str(item["resource_id"]) for item in resources)
        entered, release = asyncio.Event(), asyncio.Event()
        original = owner._transport.list_resources
        held = False
        receipts = 0

        async def list_receipt() -> list[dict[str, object]]:
            nonlocal held, receipts
            receipt = await original()
            receipts += 1
            if not held:
                held = True
                entered.set()
                await release.wait()
            return receipt

        monkeypatch.setattr(owner._transport, "list_resources", list_receipt)
        listing = asyncio.create_task(owner.list_resources())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            # 正式owner查询和工具都必须在慢清单尚未返回时完成。
            current = await asyncio.wait_for(owner.get_resource(peer), 5)
            assert current["owner_revision"] == resources[1]["owner_revision"]
            read = await asyncio.wait_for(owner.perform(
                peer, action="run", payload={"code": "return 41 + 1;"},
                expected_revision=int(current["owner_revision"]),
                holder_id="request:list-peer", operation_id="list-peer",
            ), 5)
            assert read["result"] == 42 and not listing.done()
            if transition == "external_stop":
                # 真实Node关闭不推进Python owner版本，由正式查询观察并发布。
                await asyncio.wait_for(client.stop_resource(rid), 5)
                committed = await asyncio.wait_for(owner.get_resource(rid), 5)
                assert committed["owner_revision"] == resources[0]["owner_revision"]
            else:
                committed = await asyncio.wait_for(owner.command(
                    rid, action="stop", expected_revision=int(resources[0]["owner_revision"]),
                    holder_id="request:list-stop", operation_id="list-stop",
                ), 5)
                assert committed["owner_revision"] == int(resources[0]["owner_revision"]) + 1
            assert committed["status"] == "closed" and committed["reconciliation_status"] == "verified"
            before = owner._store.path.read_bytes()
            ledger = owner._leases._state_path.read_bytes()
            release.set()
            with pytest.raises(ResourceConflict, match="列表读取期间状态已改变"):
                await asyncio.wait_for(listing, 5)
            assert owner._store.path.read_bytes() == before
            assert owner._leases._state_path.read_bytes() == ledger
            assert owner._leases.leases_for_resource(rid) == []
            runtime = await client.get_resource(rid)
            assert runtime["status"] == "closed"
            assert runtime["transport_owner_revision"] == committed["owner_revision"]
            calls = receipts
            refreshed = await owner.list_resources()
            assert receipts == calls + 1
            assert {item["resource_id"]: item["status"] for item in refreshed} == {rid: "closed", peer: "running"}
            run.write_result("passed", {
                "transition": transition, "resource_id": rid, "peer_id": peer,
                "owner_revision": committed["owner_revision"], "peer_result": read["result"],
                "scope": "正式Session及Python资源owner、真实Node/Chromium/HTTP；暂停已取得的清单回执，不代表Gateway/主Web、网络永久挂起或完整shutdown期限",
            })
        finally:
            release.set()
            await asyncio.gather(listing, return_exceptions=True)
            for resource_id in (rid, peer):
                for thread in owner._store.get(resource_id).thread_associations:
                    await owner.command(
                        resource_id, action="detach", expected_revision=owner._store.get(resource_id).owner_revision,
                        thread=thread, holder_id=f"request:list-detach:{resource_id}", operation_id="list-detach",
                    )
                await owner.command(
                    resource_id, action="delete", expected_revision=owner._store.get(resource_id).owner_revision,
                    holder_id=f"request:list-cleanup:{resource_id}", operation_id="list-cleanup",
                )
