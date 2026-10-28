"""正式资源入口遇到 owner 落盘失败时不发布候选或真实位置。"""

from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
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
from tests.support.workspaces import render_test_workspace_config


@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_resource_creation_disk_failure_keeps_old_owner_and_allows_explicit_retry(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
    owner_mount: MountedWorkspace, monkeypatch: pytest.MonkeyPatch, resource_api, kind: str,
) -> None:
    _browser, root, run = browser_client
    # 此测试不调用模型，仅覆盖资源入口；隔离配置不能依赖个人 Provider 凭据。
    config_path = root / ".boxteam/workspace.jsonc"
    config = json.loads(render_test_workspace_config(config_path.read_text(encoding="utf-8")))
    for provider in config["llm"]["providers"]:
        provider["api_key"] = "resource-record-unused-key"
        provider["endpoint"] = "http://127.0.0.1:1/v1"
        provider.pop("auth", None)
    config_path.write_text(json.dumps(config, ensure_ascii=False) + "\n", encoding="utf-8")
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        async with resource_api(container, kind) as client:
            endpoint = f"/api/v1/resources/{kind}s"
            response = await client.post(endpoint, json={"session_id": sid, "title": "已有真实资源"})
            assert response.status_code == 200, response.text
            existing = response.json()["data"]["resource_id"]
            before = owner._store.path.read_bytes()
            records = owner._store.all()
            ledger_path = owner._leases._state_path
            assert ledger_path is not None
            leases = ledger_path.read_bytes()
            external = await owner._transport.get_resource(existing)
            pending = owner._store.path.with_suffix(".tmp")
            native_error = PermissionError(errno.EACCES, "owner 发布失败", str(pending))
            replace = Path.replace

            def fail_publication(path: Path, target: Path) -> Path:
                if path == pending:
                    raise native_error
                return replace(path, target)

            request_id = f"resource-record-{kind}-failure"
            with monkeypatch.context() as patch:
                patch.setattr(Path, "replace", fail_publication)
                failed = await client.post(endpoint, json={"session_id": sid, "title": "未发布的资源"},
                                           headers={"X-Request-ID": request_id})
            assert failed.status_code == 500, failed.text
            assert failed.headers["X-Request-ID"] == failed.json()["request_id"] == request_id
            assert "PermissionError" in failed.text and "stage=write" in failed.text and f"kind={kind}" in failed.text
            assert str(root) not in failed.text and str(pending) not in failed.text
            assert owner._store.path.read_bytes() == before and owner._store.all() == records
            assert ledger_path.read_bytes() == leases
            assert [item[f"{kind}_id"] for item in await owner._transport.list_resources()] == [existing]
            identity = "os_pid" if kind == "terminal" else "active_page_id"
            assert (await owner._transport.get_resource(existing))[identity] == external[identity]
            retried = await client.post(endpoint, json={"session_id": sid, "title": "显式重试"})
            assert retried.status_code == 200, retried.text
            created = retried.json()["data"]["resource_id"]
            assert created != existing and set(owner._store.ids()) == {existing, created}
            assert str(root) not in owner._store.path.read_text()
            (run.artifacts_dir / f"record-errors-{kind}.json").write_text(json.dumps({
                "failure": failed.json(), "retry": retried.json(), "existing_resource_id": existing,
                "scope": "正式Session和ASGI资源API、真实Node/PTY或Chromium/HTTP；未验证Gateway/Web或Backend进程重启",
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
