"""验证卸载工作区作用域包时明确报告不可达的外部配置 owner。"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from app.core.workspace_identity import read_workspace_id
from tests.support.plugin_package import install_plugin

pytest_plugins = ("tests.integration.gateway.test_plugin_install_operations",)


def test_workspace_package_uninstall_reports_external_owner_and_keeps_package(
    source_gateway, integration_workspace_root_path: str, request: pytest.FixtureRequest,
) -> None:
    client, context, _restart = source_gateway
    evidence: list[dict[str, object]] = []

    def exchange(method: str, path: str, body: dict[str, object] | None = None) -> httpx.Response:
        request_id = f"workspace-uninstall-{len(evidence)}"
        response = client.request(method, path, json=body, headers={"X-Request-ID": request_id})
        payload = response.json()
        assert response.headers["X-Request-ID"] == payload["request_id"] == request_id
        evidence.append({"method": method, "path": path, "status": response.status_code, "body": payload})
        return response

    gateway_id = exchange("GET", "/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]
    workspace_id = read_workspace_id(Path(integration_workspace_root_path))

    manifest = {
        "manifest_version": 1,
        "package_id": "workspace.declaration.proof",
        "version": "1.0.0",
        "name": "工作区声明卸载探针",
        "extension_contract_version": 1,
        "plugins": [{
            "id": "declaration",
            "name": "声明探针",
            "instance_scope": "workspace",
            "requires": [{"extension_point": "configuration", "version": 1}],
            "contributes": {"configuration": {"type": "object", "properties": {}}},
        }],
    }
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("plugin.jsonc", json.dumps(manifest))
    package_path = context.artifacts_dir / "workspace-declaration.zip"
    package_path.write_bytes(archive.getvalue())

    install_plugin(
        lambda path, body: exchange("POST", path, body),
        gateway_id=gateway_id,
        path=str(package_path),
    )
    package_root = context.boxteam_home_for_node(request.node.nodeid) / (
        "plugins/packages/workspace.declaration.proof/1.0.0"
    )
    uninstall = exchange("POST", "/api/gateway/service-plugins/uninstall", {
        "gateway_id": gateway_id,
        "package_id": "workspace.declaration.proof",
        "package_version": "1.0.0",
    })

    assert uninstall.status_code == 409 and workspace_id in uninstall.json()["detail"], uninstall.text
    inventory = exchange("POST", "/api/gateway/service-plugins/inventory", {"gateway_id": gateway_id})
    assert any(
        item["manifest"]["package_id"] == "workspace.declaration.proof"
        and item["manifest"]["version"] == "1.0.0"
        for item in inventory.json()["data"]["packages"]
    )
    assert package_root.is_dir()
    after = exchange("PATCH", f"/api/gateway/workspaces/{workspace_id}", {"name": "外部插件卸载探针"})
    assert after.status_code == 200, after.text
    workspaces = after.json()["data"]["items"]
    target, = (item for item in workspaces if item["workspace_id"] == workspace_id)
    assert target["managed"] is False and target["runtime_action"] == "probe_external_backend"
    (context.artifacts_dir / "workspace-plugin-uninstall-http.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
