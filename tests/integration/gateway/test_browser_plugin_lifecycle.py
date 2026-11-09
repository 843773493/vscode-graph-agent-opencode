from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import commentjson
import httpx
import pytest
from websockets.sync.client import connect

from tests.harness.python.run_context import TestRunContext
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.terminal_plugin import copy_prepared_developer_tools_package
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)

PACKAGE_ID = "boxteam.developer-tools"
PACKAGE_VERSION = "0.1.0"
PLUGIN_ID = "browser"


def _json(response: httpx.Response) -> dict[str, Any]:
    assert response.is_success, f"Gateway 请求失败: status={response.status_code}, body={response.text}"
    value = response.json()
    if not isinstance(value, dict):
        raise TypeError(f"Gateway 响应必须是 JSON 对象: {response.request.url}")
    return value


def _enable_browser(client: httpx.Client, *, gateway_id: str, workspace_id: str, idempotency_key: str) -> None:
    path = f"/api/v1/workspaces/{workspace_id}/config/sources/workspace_root_mutable_override"
    headers = {"X-BoxTeam-Workspace-Id": workspace_id}
    document = _json(client.get(path, headers=headers))["data"]
    config = commentjson.loads(str(document["jsonc"]))
    config.setdefault("plugins", {}).setdefault("runtime", {})["entries"] = [{
        "package_id": PACKAGE_ID,
        "package_version": PACKAGE_VERSION,
        "plugin_id": PLUGIN_ID,
        "enabled": True,
    }]
    result = _json(client.put(path, headers=headers, json={
        "gateway_id": gateway_id,
        "workspace_id": workspace_id,
        "source_key": "workspace_root_mutable_override",
        "jsonc": json.dumps(config, ensure_ascii=False, indent=2),
        "base_file_digest": document["file_digest"],
        "base_layer_revision": document["layer_revision"],
        "base_layer_digest": document["layer_digest"],
        "expected_active_revision": document["active_revision"],
        "expected_active_digest": document["active_digest"],
        "idempotency_key": idempotency_key,
    }))
    assert result["data"]["edit"]["state"] == "active", result


def _wait_for_browser_plugin(client: httpx.Client, *, gateway_id: str, workspace_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 90
    last: Any = None
    while time.monotonic() < deadline:
        data = _json(client.post("/api/gateway/service-plugins/status", json={"gateway_id": gateway_id}))["data"]
        last = next((plugin for plugin in data["plugins"]
            if plugin["binding"]["package_id"] == PACKAGE_ID
            and plugin["binding"]["plugin_id"] == PLUGIN_ID
            and plugin["binding"]["workspace_id"] == workspace_id), None)
        if last is not None and last["state"] in {"ready", "failed"}:
            break
        time.sleep(0.2)
    assert last is not None and last["state"] == "ready", f"Browser 插件未就绪: {last}"
    return last


def _receive_message(socket, expected: str, timeout: float = 20) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = socket.recv(timeout=max(0.1, deadline - time.monotonic()))
        if isinstance(message, bytes):
            continue
        value = json.loads(message)
        if value.get("type") == expected:
            return value
    raise TimeoutError(f"Browser WebSocket 未收到 {expected}")


def test_browser_plugin_runs_through_workspace_owner_and_exact_gateway_routes(
    request: pytest.FixtureRequest,
    unused_tcp_port: int,
    prepared_developer_tools_package: Path,
) -> None:
    project_root = Path.cwd().resolve()
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    template = project_root / "tests/fixtures/workspaces/default_test_workspace"
    gateway_workspace = prepare_test_workspace(
        workspace_root=context.workspace_root / "gateway-default", template_root=template, template_items=(),
    )
    workspace = prepare_test_workspace(
        workspace_root=context.workspace_root / "browser-workspace", template_root=template, template_items=(),
    )
    for root in (gateway_workspace, workspace):
        install_test_workspace_config(
            workspace_root=root,
            config_path=project_root / "configs/tests/workspace/default.jsonc",
            schema_path=project_root / "configs/workspace_schema.jsonc",
        )
        config_path = root / ".boxteam/workspace.jsonc"
        config = commentjson.loads(config_path.read_text(encoding="utf-8"))
        config.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})[
            "url"
        ] = f"http://127.0.0.1:{unused_tcp_port}"
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    gateway_home = context.boxteam_home_for_node(request.node.nodeid)
    copy_prepared_developer_tools_package(prepared_developer_tools_package, gateway_home)
    gateway = start_gateway_process(
        workspace_root=gateway_workspace,
        default_backend_url=None,
        port=unused_tcp_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid),
        artifacts_dir=context.artifacts_dir,
        extra_env={name: "browser-plugin-test-key" for name in (
            "CCTQ_API_KEY", "KILO_API_KEY", "OPENCODE_ZEN_API_KEY", "OPENROUTER_API_KEY",
        )},
    )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS,
            timeout=45, trust_env=False,
        ) as client:
            gateway_id = _json(client.get("/api/gateway/health"))["data"]["gateway_id"]
            client.post(
                "/api/gateway/users/guest",
                json={"tracking": {"test": True}},
            ).raise_for_status()
            managed = _json(client.post("/api/gateway/managed-workspaces", json={
                "root_path": str(workspace), "name": "Browser plugin lifecycle", "create_directory": False,
            }, timeout=75))["data"]
            workspace_id = next(item["workspace_id"] for item in managed["items"]
                if item["gateway_id"] == gateway_id and item["name"] == "Browser plugin lifecycle")

            # 包已由 session fixture 准备并复制到本场景隔离 Home；此用例证明 Browser 业务路由。
            _enable_browser(client, gateway_id=gateway_id, workspace_id=workspace_id,
                idempotency_key="browser-plugin-lifecycle-enable")
            plugin = _wait_for_browser_plugin(client, gateway_id=gateway_id, workspace_id=workspace_id)
            instance = plugin["instance"]
            assert instance["services"] == [
                {"service_id": "http", "service_type": "browser", "protocol": "http"},
                {"service_id": "socket", "service_type": "browser", "protocol": "websocket"},
            ]

            # Gateway 发布原 Browser 页面与模块，不启动第二个前端服务。
            page = client.get("/api/gateway/attach/browser/index.html")
            assert page.status_code == 200 and 'src="./main.js"' in page.text
            assert client.get("/api/gateway/attach/browser/config.js").status_code == 404

            workspace_headers = {"X-BoxTeam-Workspace-Id": workspace_id}
            session = _json(client.post("/api/v1/sessions", headers=workspace_headers,
                json={"title": "Browser plugin lifecycle"}))["data"]
            resource = _json(client.post("/api/v1/resources/browsers", headers=workspace_headers, json={
                "session_id": session["session_id"], "thread_id": session["thread_id"],
                "retention_scope": "workspace", "title": "Gateway Browser plugin",
                "url": "about:blank",
            })) ["data"]
            assert resource["kind"] == "browser" and resource["status"] == "running"

            grant = _json(client.post(f"/api/v1/resources/{resource['resource_id']}/transport-grant",
                headers=workspace_headers,
                json={"expected_revision": resource["owner_revision"]})) ["data"]
            service = grant["service"]
            assert service == {
                "gateway_id": gateway_id, "package_id": PACKAGE_ID, "package_version": PACKAGE_VERSION,
                "plugin_id": PLUGIN_ID, "instance_id": instance["instance_id"],
                "service_id": "socket", "workspace_id": workspace_id,
            }
            query = urlencode({
                "workspace_id": workspace_id, "package_version": PACKAGE_VERSION,
                "target_gateway_id": gateway_id, "token": client.headers["X-Local-Token"],
            })
            socket_url = (
                f"ws://127.0.0.1:{gateway.port}/api/gateway/service-plugins/{PACKAGE_ID}/{PLUGIN_ID}"
                f"/instances/{instance['instance_id']}/services/socket/browser?{query}"
            )
            with connect(socket_url, open_timeout=10, close_timeout=3) as socket:
                socket.send(json.dumps({
                    "type": "attach", "browserId": resource["resource_id"],
                    "grantToken": grant["grant_token"],
                }))
                attached = _receive_message(socket, "attached")
                assert attached["browserId"] == resource["resource_id"]

                operated = _json(client.post(
                    f"/api/v1/resources/{resource['resource_id']}/browser-operation",
                    headers=workspace_headers,
                    json={
                        "expected_revision": resource["owner_revision"],
                        "thread": resource["source_thread"],
                        "action": "navigate",
                        "payload": {"type": "url", "url": "data:text/html,<title>plugin-route</title>"},
                    },
                ))["data"]["browser"]
                assert operated["metadata"]["pages"][0]["title"] == "plugin-route", operated

                service_root = (
                    f"/api/gateway/service-plugins/{PACKAGE_ID}/{PLUGIN_ID}"
                    f"/instances/{instance['instance_id']}/services/http"
                )
                service_params = {"workspace_id": workspace_id, "package_version": PACKAGE_VERSION}
                owner_headers = {
                    "X-BoxTeam-Workspace-Id": workspace_id,
                    "X-BoxTeam-Target-Gateway-Id": gateway_id,
                    "X-BoxTeam-Resource-Owner": "workspace-browser",
                }
                locked = _json(client.patch(
                    f"{service_root}/api/browsers/{resource['resource_id']}/agent-lock",
                    params=service_params, headers=owner_headers,
                    json={"locked": True, "owner_id": "user_browser_plugin_lifecycle"},
                ))["data"]
                assert locked["browser"]["agent_access_locked"] is True
                blocked = client.post(
                    f"{service_root}/api/browsers/{resource['resource_id']}/navigate",
                    params=service_params,
                    headers={**owner_headers, "X-BoxTeam-Actor": "agent"},
                    json={"type": "url", "url": "data:text/html,<title>must-stay-locked</title>"},
                )
                assert blocked.status_code == 423, blocked.text
                missing_owner_control = client.post(
                    f"{service_root}/api/browsers/{resource['resource_id']}/navigate",
                    params=service_params,
                    headers={"X-BoxTeam-Workspace-Id": workspace_id,
                             "X-BoxTeam-Target-Gateway-Id": gateway_id,
                             "X-BoxTeam-Actor": "user:http"},
                    json={"type": "url", "url": "data:text/html,<title>must-not-run</title>"},
                )
                assert missing_owner_control.status_code == 403, missing_owner_control.text
                unlocked = _json(client.patch(
                    f"{service_root}/api/browsers/{resource['resource_id']}/agent-lock",
                    params=service_params, headers=owner_headers,
                    json={"locked": False, "owner_id": "user_browser_plugin_lifecycle"},
                ))["data"]
                assert unlocked["browser"]["agent_access_locked"] is False
                navigated = _json(client.post(
                    f"{service_root}/api/browsers/{resource['resource_id']}/navigate",
                    params=service_params,
                    headers={**owner_headers, "X-BoxTeam-Actor": "user:http"},
                    json={"type": "url", "url": "data:text/html,<title>artifact-read</title>"},
                ))["data"]
                screenshot = _json(client.post(
                    f"{service_root}/api/browsers/{resource['resource_id']}/screenshot",
                    params=service_params, headers=owner_headers, json={"selector": "body"},
                ))["data"]
                screenshot_id = screenshot["screenshot_id"]
                artifact = client.get(
                    f"{service_root}/api/browsers/{resource['resource_id']}/screenshots/{screenshot_id}",
                    params=service_params,
                    headers={"X-BoxTeam-Workspace-Id": workspace_id,
                             "X-BoxTeam-Target-Gateway-Id": gateway_id},
                )
                assert artifact.status_code == 200, artifact.text
                assert artifact.content.startswith(b"\x89PNG\r\n\x1a\n")
                missing_artifact = client.get(
                    f"{service_root}/api/browsers/{resource['resource_id']}/screenshots/screenshot_missing",
                    params=service_params,
                    headers={"X-BoxTeam-Workspace-Id": workspace_id,
                             "X-BoxTeam-Target-Gateway-Id": gateway_id},
                )
                assert missing_artifact.status_code == 404, missing_artifact.text
                assert navigated["browser"]["pages"][0]["title"] == "artifact-read"

                stopped = _json(client.post(f"/api/v1/resources/{resource['resource_id']}/stop",
                    headers=workspace_headers,
                    json={"expected_revision": operated["owner_revision"]})) ["data"]
                assert stopped["status"] == "closed", stopped
                socket.close()

            _json(client.post("/api/gateway/service-plugins/restart", json={
                "gateway_id": gateway_id, "workspace_id": workspace_id, "package_id": PACKAGE_ID,
                "package_version": PACKAGE_VERSION, "plugin_id": PLUGIN_ID, "revision": plugin["revision"],
            }))
            _enable_browser(client, gateway_id=gateway_id, workspace_id=workspace_id,
                idempotency_key="browser-plugin-lifecycle-enable-after-restart")
            restarted = _wait_for_browser_plugin(client, gateway_id=gateway_id, workspace_id=workspace_id)
            assert restarted["instance"]["instance_id"] != instance["instance_id"]
    finally:
        close_gateway_process(gateway)
