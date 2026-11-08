"""真实辅助服务的 JSON 解析失败不回显输入，也不创建资源。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.gateway.auth import get_gateway_local_token
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.browser.test_browser_owner_revision_drain import (
    browser_client as browser_client,  # noqa: PLC0414
)
from tests.integration.workspace_services.browser.test_browser_owner_revision_drain import (
    owner_mount as owner_mount,  # noqa: PLC0414
)


@pytest.fixture
async def json_terminal_client(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
    owner_mount: MountedWorkspace,
) -> AsyncIterator[TerminalManagerClient]:
    browser, root, _run = browser_client
    client = TerminalManagerClient(workspace_id=browser._workspace_id,
                                   config_service=ConfigService(workspace_root=root))
    try:
        yield client
    finally:
        await client.aclose()


@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_invalid_json_keeps_native_input_in_diagnostics_and_allows_retry(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
    owner_mount: MountedWorkspace, kind: str, request: pytest.FixtureRequest,
    json_terminal_client: TerminalManagerClient,
) -> None:
    browser, root, run = browser_client
    config = ConfigService(workspace_root=root)
    terminal = json_terminal_client
    transport = terminal if kind == "terminal" else browser
    route = f"/api/{kind}s"
    headers = {"X-BoxTeam-Workspace-Id": owner_mount.workspace_id,
               "X-BoxTeam-Resource-Owner": f"workspace-{kind}"}
    evidence = []
    if kind == "terminal":
        headers["X-Local-Token"] = get_gateway_local_token()
        binding = terminal._current_binding()
        base_url = config.get_gateway_connection_url()
        route = (f"/api/gateway/service-plugins/{binding.package_id}/{binding.plugin_id}"
                 f"/services/http{route}")
        params = {"package_version": binding.package_version, "workspace_id": owner_mount.workspace_id}
    else:
        base_url, params = browser.backend_url, {}
    async with httpx.AsyncClient(base_url=base_url, headers=headers,
                                 timeout=15, trust_env=False) as client:
        before = await transport.list_resources()
        for native_path in ("/parse-secret", r"C:\parse-secret", r"\\host\parse-secret"):
            for body in (native_path, '{"path":' + native_path + "}"):
                response = await client.post(route, content=body, params=params, headers={"Content-Type": "application/json"})
                assert response.status_code == 500, response.text
                assert "parse-secret" not in response.text
                assert response.json()["error"] == "HTTP JSON 请求解析失败: stage=request_json, error=SyntaxError"
                assert await transport.list_resources() == before
                evidence.append(response.json())

        # 同一服务接受下一次正常请求；普通正文和 Browser 页面 URL 不经过字符串脱敏。
        resource_id = f"{'term' if kind == 'terminal' else kind}_{uuid4().hex}"
        title = "/parse-secret 是用户标题"
        if kind == "terminal":
            created = await terminal.create_terminal(terminal_id=resource_id, owner_revision=1,
                                                       title=title, cwd=str(root))
        else:
            page = root / "parse-secret.html"
            page.write_text(f"<title>{title}</title><p>JSON 恢复验证</p>", encoding="utf-8")
            created = await browser.create_browser(browser_id=resource_id, owner_revision=1,
                                                     title=title, url=page.as_uri())
            assert created["url"] == page.as_uri()
        assert created[f"{kind}_id"] == resource_id and created["title"] == title
        assert [item[f"{kind}_id"] for item in await transport.list_resources()] == [resource_id]
        await transport.delete_resource(resource_id)
        tombstone, = await transport.list_resources()
        assert tombstone[f"{kind}_id"] == resource_id and tombstone["status"] == "deleted"

    logs = (list((run.boxteam_home_for_node(request.node.nodeid) / "state/logs").rglob("*.log"))
            if kind == "terminal" else [path for path in run.artifacts_dir.glob("*.stderr.log") if kind in path.name])
    assert logs and any("parse-secret" in path.read_text(encoding="utf-8") for path in logs)
    (run.artifacts_dir / f"http-json-errors-{kind}.json").write_text(json.dumps({
        "failures": evidence, "resource_id": resource_id,
        "scope": "真实 Node HTTP、PTY/Chromium；不代表 Python owner、Gateway、Backend 重启或 Web 验收",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
