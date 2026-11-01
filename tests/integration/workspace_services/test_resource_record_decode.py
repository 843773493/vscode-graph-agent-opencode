"""正式创建的真实资源遇到损坏 owner 记录，冷读拒绝且不重建外部实例。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.services.business.workspace_resources.records import ResourceRecordStore
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
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
async def test_real_resource_record_decode_rejects_original_without_redirecting_or_recreating(
    resource_workspace: tuple[BrowserManagerClient, Path, TestRunContext],
    owner_mount: MountedWorkspace, resource_api, kind: str, caplog: pytest.LogCaptureFixture,
) -> None:
    _browser, root, run = resource_workspace
    async with (
        workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context),
        resource_api(container, kind) as api,
    ):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        created = await api.post(f"/api/v1/resources/{kind}s", json={"session_id": sid, "title": "冷读原资源"})
        assert created.status_code == 200, created.text
        rid = created.json()["data"]["resource_id"]
        path = owner._store.path
        original = path.read_bytes()
        records = owner._store.all()
        native = await owner._transport.get_resource(rid)
        identity = "os_pid" if kind == "terminal" else "active_page_id"
        ledger_path = owner._leases._state_path
        assert ledger_path is not None
        ledger = ledger_path.read_bytes()
        rejected = []
        try:
            for field, value in (("created_at", "/private/cold-record-input"), ("owner_revision", "1")):
                payload = deepcopy(json.loads(original))
                payload["records"][0][field] = value
                damaged = json.dumps(payload, ensure_ascii=False).encode()
                path.write_bytes(damaged)
                with pytest.raises(ValueError) as caught:
                    ResourceRecordStore(workspace_root=root, workspace_id=owner.workspace_id, kind=owner.kind)
                error = caught.value
                assert str(root) not in str(error)
                if field == "created_at":
                    assert value not in str(error)
                assert "stage=validate" in str(error) and f"kind={kind}" in str(error)
                assert error.__cause__ is not None
                assert any(record.exc_info is not None and record.exc_info[1] is error.__cause__
                           for record in caplog.records)
                assert path.read_bytes() == damaged and owner._store.all() == records
                assert ledger_path.read_bytes() == ledger
                current = await owner._transport.get_resource(rid)
                assert current[identity] == native[identity] and current["transport_owner_revision"] == 1
                assert [item[f"{kind}_id"] for item in await owner._transport.list_resources()] == [rid]
                rejected.append({"field": field, "error": str(error)})
        finally:
            # 恢复测试注入的损坏原件，生命周期仍由原 owner 收尾。
            path.write_bytes(original)
        cold = ResourceRecordStore(workspace_root=root, workspace_id=owner.workspace_id, kind=owner.kind)
        assert cold.all() == records and path.read_bytes() == original
        found = await api.get(f"/api/v1/resources/{rid}")
        assert found.status_code == 200, found.text
        assert found.json()["data"]["resource_id"] == rid
        stopped = await api.post(f"/api/v1/resources/{rid}/stop", json={"expected_revision": 1},
                                 headers={"X-Request-ID": f"decode-retry-{kind}"})
        assert stopped.status_code == 200, stopped.text
        assert stopped.headers["X-Request-ID"] == stopped.json()["request_id"] == f"decode-retry-{kind}"
        final = stopped.json()["data"]
        assert final["status"] in {"terminated", "closed"} and final["reconciliation_status"] == "verified"
        assert final["owner_revision"] == 2
        assert (await owner._transport.get_resource(rid))["status"] == final["status"]
        run.write_result("passed", {"kind": kind, "resource_id": rid, "rejected": rejected,
            "scope": "正式Session/ASGI创建、真实PTY/Chromium/HTTP与记录store冷读；非Backend重启、Gateway/Web或冷owner恢复"})
