"""真实动作失败后的回执与提交故障不得释放原 operation 占用。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.abstractions.browser_resources import BrowserRequestError
from app.core.mounted_workspaces import MountedWorkspace
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


@pytest.mark.parametrize("entry", ["tool", "http"])
@pytest.mark.parametrize("fault", ["revision", "record"])
async def test_failed_real_action_keeps_its_lease_until_explicit_verified_stop(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext], owner_mount: MountedWorkspace,
    resource_api, monkeypatch: pytest.MonkeyPatch, entry: str, fault: str,
) -> None:
    transport, root, run = browser_client
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context):
        owner = container.browser_resource_owner
        tid = container.session_catalog.resolver.main_thread_id(sid)
        async with resource_api(container, "browser") as client:
            created = await client.post("/api/v1/resources/browsers", json={
                "session_id": sid, "retention_scope": "workspace", "url": "about:blank",
            })
            assert created.status_code == 200, created.text
            rid = created.json()["data"]["resource_id"]
            promoted = await client.post(f"/api/v1/resources/{rid}/promote", json={"expected_revision": 1})
            assert promoted.status_code == 200, promoted.text
            assert promoted.json()["data"]["owner_revision"] == 2
            before = await transport.get_resource(rid)
            perform, save = owner._transport.perform, owner._store.save
            originals, leases, responses = [], [], []
            armed = False
            payload = {"code": (
                "await page.evaluate(() => { document.body.dataset.receipts = "
                "String(Number(document.body.dataset.receipts || 0) + 1); }); "
                "throw new Error('deliberate receipt failure');"
            )}

            async def returned(resource_id, action, values, *, actor):
                lease, = owner._leases.leases_for_resource(resource_id)
                leases.append(lease)
                try:
                    return await perform(resource_id, action, values, actor=actor)
                except BrowserRequestError as error:
                    assert error.payload["operation_outcome"] == "failed"
                    assert error.payload["browser"]["transport_owner_revision"] == 2
                    originals.append(error)
                    if armed and fault == "revision":
                        # 故障只改真实HTTP回执的版本，页面和Node持久状态保持原值。
                        altered = {**error.payload, "browser": {
                            **error.payload["browser"], "transport_owner_revision": 1,
                        }}
                        raise BrowserRequestError(
                            method=error.method, path=error.path, status=error.status, payload=altered,
                        ) from error
                    raise

            def committing(*records):
                if armed and fault == "record":
                    # 只拒绝回执的首次提交，后续unknown仍经原持久化入口。
                    patch.setattr(owner._store, "save", save)
                    raise ConnectionError("receipt commit fault")
                return save(*records)

            async def invoke():
                if entry == "http":
                    response = await client.post(f"/api/v1/resources/{rid}/browser-operation", json={
                        "action": "run", "payload": payload, "expected_revision": 2,
                    }, headers={"X-Request-ID": f"receipt-{fault}-{'bad' if armed else 'valid'}"})
                    responses.append({"status": response.status_code, "body": response.json()})
                    assert response.headers["X-Request-ID"] == f"receipt-{fault}-{'bad' if armed else 'valid'}"
                    assert str(root) not in response.text
                    assert response.status_code == ((400 if fault == "revision" else 503) if armed else 500), response.text
                    return
                async with owner.operation(
                    session_id=sid, thread_id=tid, holder_id="request:receipt-tool", operation_id="receipt-tool",
                ) as operation:
                    await operation.perform(rid, "run", payload)

            with monkeypatch.context() as patch:
                patch.setattr(owner._transport, "perform", returned)
                patch.setattr(owner._store, "save", committing)
                if entry == "tool":
                    with pytest.raises(BrowserRequestError) as valid:
                        await invoke()
                    assert valid.value is originals[0]
                else:
                    await invoke()
                first, = leases
                assert owner._leases.get_lease(first.lease_id).status == "released"
                assert not owner._leases.leases_for_resource(rid)
                assert owner._store.get(rid).reconciliation_status == "verified"
                armed = True
                if entry == "tool":
                    with pytest.raises(ValueError if fault == "revision" else ConnectionError) as invalid:
                        await invoke()
                    assert isinstance(invalid.value.__cause__, BrowserRequestError)
                else:
                    await invoke()
                assert len(leases) == len(originals) == 2
                original = leases[-1]
                pending, = owner._leases.leases_for_resource(rid)
                assert pending.lease_id == original.lease_id
                assert pending.status == "reconcile_required"
                record = owner._store.get(rid)
                assert record.reconciliation_status == "unknown"
                assert record.owner_revision == 2 and record.status == "running"
                cold = ExternalResourceLeaseLedger(state_path=owner._leases._state_path)
                assert cold.get_lease(original.lease_id) == pending
            external = await transport.get_resource(rid)
            assert external["active_page_id"] == before["active_page_id"]
            assert external["transport_owner_revision"] == 2
            effects = await transport.perform(rid, "run", {
                "code": "return await page.evaluate(() => document.body.dataset.receipts);",
            }, actor="user:http")
            assert effects["result"] == "2"
            current = await client.get(f"/api/v1/resources/{rid}")
            assert current.status_code == 200, current.text
            assert current.json()["data"]["reconciliation_status"] == "unknown"
            assert owner._leases.get_lease(original.lease_id).status == "reconcile_required"
            stopped = await client.post(f"/api/v1/resources/{rid}/stop", json={"expected_revision": 2}, headers={
                "X-Request-ID": "receipt-explicit-stop",
            })
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["data"]["status"] == "closed"
            assert stopped.json()["data"]["owner_revision"] == 3
            assert owner._leases.get_lease(original.lease_id).status == "settled"
            assert owner._leases.get_lease(first.lease_id).status == "released"
            assert len(originals) == 2
            assert (await transport.get_resource(rid))["status"] == "closed"
            (run.artifacts_dir / f"receipt-{entry}-{fault}.json").write_text(json.dumps({
                "entry": entry, "fault": fault, "original_errors": [error.payload for error in originals],
                "responses": responses, "stop": stopped.json(), "original_lease_id": original.lease_id,
                "scope": "正式Session/ASGI创建与管理、原工具operation、真实Node/Chromium/HTTP；回执版本和首次提交为故障注入，不代表Gateway/Web或独立Backend重启",
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
