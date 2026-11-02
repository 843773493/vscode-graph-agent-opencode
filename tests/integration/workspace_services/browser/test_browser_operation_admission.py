"""正式 Browser 动作入口的准入到期与真实执行保留同一资源归属。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.services.business.browser_resources import owner as owner_module
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.browser.test_browser_owner_revision_drain import (
    blocked_action as blocked_action,  # noqa: PLC0414
)
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


@pytest.mark.parametrize("stage", ["lock", "run"])
async def test_http_action_preserves_admission_and_execution_boundaries(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
    owner_mount: MountedWorkspace, blocked_action: tuple[str, asyncio.Event, asyncio.Event],
    monkeypatch: pytest.MonkeyPatch, resource_api, stage: str,
) -> None:
    _client, root, run = browser_client
    url, arrived, release = blocked_action
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context):
        owner = container.browser_resource_owner
        tid = container.session_catalog.resolver.main_thread_id(sid)
        async with resource_api(container, "browser") as client:
            resources = []
            for number in range(2):
                created = await client.post("/api/v1/resources/browsers", json={
                    "session_id": sid, "retention_scope": "thread", "title": f"admission-{number}",
                    "url": "data:text/html,<input id=field value=initial>",
                })
                assert created.status_code == 200, created.text
                resources.append(created.json()["data"]["resource_id"])
            rid, peer = resources
            original = await owner._transport.get_resource(rid)
            lock = owner._resource_lock(rid)
            entered, locked = asyncio.Event(), asyncio.Event()
            recover, perform = owner.recover, owner._transport.perform
            deadlines: list[asyncio.Timeout] = []
            dispatched: list[str] = []

            def selected() -> bool:
                task = asyncio.current_task()
                return task is not None and task.get_name() == "http-action-under-test"

            def capture(seconds: float) -> asyncio.Timeout:
                assert seconds == 30
                context = asyncio.timeout(seconds)
                if selected():
                    deadlines.append(context)
                return context

            async def recovering(resource_ids=None) -> None:
                await recover(resource_ids)
                if selected():
                    entered.set()
                    if stage == "lock":
                        await locked.wait()

            async def performing(resource_id, action, payload, *, actor):
                dispatched.append(resource_id)
                return await perform(resource_id, action, payload, actor=actor)

            body = {"expected_revision": 1, "thread": {"session_id": sid, "thread_id": tid}}
            code = f"await fetch({json.dumps(url)}); await page.locator('#field').fill('completed'); return 'completed';"
            with monkeypatch.context() as patch:
                patch.setattr(owner_module, "timeout", capture, raising=False)
                patch.setattr(owner, "recover", recovering)
                patch.setattr(owner._transport, "perform", performing)
                pending = asyncio.create_task(client.post(f"/api/v1/resources/{rid}/browser-operation", json={
                    **body, "action": "run", "payload": {"code": code, "timeoutMs": 60000},
                }, headers={"X-Request-ID": "browser-admission-under-test"}), name="http-action-under-test")
                try:
                    await asyncio.wait_for(entered.wait() if stage == "lock" else arrived.wait(), 10)
                    if stage == "lock":
                        # 冷恢复也会取得原资源锁；完成后由本测试任务持锁制造真实竞争。
                        await lock.acquire()
                        locked.set()
                    independent = await client.post(f"/api/v1/resources/{peer}/browser-operation", json={
                        **body, "action": "read", "payload": {},
                    })
                    assert independent.status_code == 200, independent.text
                    before = owner._store.path.read_bytes()
                    ledger_path = owner._leases._state_path
                    assert ledger_path is not None
                    ledger = ledger_path.read_bytes()
                    if stage == "lock":
                        async with asyncio.timeout(2):
                            while not lock._waiters:
                                await asyncio.sleep(0)
                        if deadlines:
                            deadline = deadlines[0].when()
                            assert deadline is not None
                            deadlines[0].reschedule(deadline - 31)
                        response = await asyncio.wait_for(asyncio.shield(pending), 1)
                        assert response.status_code == 503, response.text
                        assert "TimeoutError" in response.text
                        assert response.headers["X-Request-ID"] == "browser-admission-under-test"
                        assert len(deadlines) == 1 and deadlines[0].expired()
                        assert owner._store.path.read_bytes() == before
                        assert ledger_path.read_bytes() == ledger
                        assert not owner._leases.leases_for_resource(rid)
                        assert dispatched == [peer] and lock.locked()
                        lock.release()
                    else:
                        lease, = owner._leases.leases_for_resource(rid)
                        assert lease.status == "dispatching" and lock.locked()
                        assert not pending.done() and len(deadlines) == 1 and not deadlines[0].expired()
                        release.set()
                        response = await asyncio.wait_for(pending, 10)
                        assert response.status_code == 200, response.text
                        assert response.json()["data"]["browser"]["reconciliation_status"] == "verified"
                        released = owner._leases.get_lease(lease.lease_id)
                        assert released is not None and released.status == "released"
                finally:
                    release.set()
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                    if stage == "lock" and lock.locked():
                        lock.release()
            current = await owner._transport.get_resource(rid)
            assert current["active_page_id"] == original["active_page_id"]
            assert current["transport_owner_revision"] == original["transport_owner_revision"] == 1
            # 通过同一公开动作入口继续操作，验证取消的准入没有迟到派发或遗留锁。
            retried = await client.post(f"/api/v1/resources/{rid}/browser-operation", json={
                **body, "action": "run", "payload": {"code": "return await page.locator('#field').inputValue();", "timeoutMs": 60000},
            })
            assert retried.status_code == 200, retried.text
            assert retried.json()["request_id"] == retried.headers["X-Request-ID"]
            assert retried.json()["data"]["result"] == ("completed" if stage == "run" else "initial")
            assert not owner._leases.leases_for_resource(rid)
            (run.artifacts_dir / f"management-{stage}.json").write_text(json.dumps({
                "response": response.json(), "retry": retried.json(), "external": current,
                "dispatched": dispatched,
                "scope": "真实Session与ASGI资源API、Node/Chromium/HTTP；仅推进本次锁准入截止，长run越过准入预算另由单测时钟核对，不代Gateway/Web或Windows",
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
