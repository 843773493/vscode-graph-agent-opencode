"""通过 Gateway 与 Workspace owner 验证正式 Terminal 插件的核心生命周期。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import commentjson
import httpx
import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from tests.harness.python.run_context import TestRunContext
from tests.support.paths import chromium_temporary_environment
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)

PACKAGE_ID = "boxteam.developer-tools"
PACKAGE_VERSION = "0.1.0"
PLUGIN_ID = "terminal"


def _json(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise TypeError(f"Gateway 响应必须是 JSON 对象: {response.request.url}")
    return value


def _build_package(project_root: Path, output: Path) -> None:
    subprocess.run(
        [
            "bun",
            "scripts/plugins/pack-developer-tools.mjs",
            "--out",
            str(output.resolve()),
        ],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _cas_workspace_binding(
    client: httpx.Client,
    *,
    gateway_id: str,
    workspace_id: str,
    enabled: bool,
    idempotency_key: str,
) -> dict[str, Any]:
    source_path = f"/api/v1/workspaces/{workspace_id}/config/sources/workspace_root_mutable_override"
    headers = {"X-BoxTeam-Workspace-Id": workspace_id}
    document = _json(client.get(source_path, headers=headers))["data"]
    if not isinstance(document, dict):
        raise TypeError("Workspace 配置来源没有 data 对象")
    source = commentjson.loads(str(document["jsonc"]))
    source.setdefault("plugins", {})["runtime"] = {
        "entries": [{
            "package_id": PACKAGE_ID,
            "package_version": PACKAGE_VERSION,
            "plugin_id": PLUGIN_ID,
            "enabled": enabled,
        }],
    }
    response = _json(client.put(source_path, headers=headers, json={
        "gateway_id": gateway_id,
        "workspace_id": workspace_id,
        "source_key": "workspace_root_mutable_override",
        "jsonc": json.dumps(source, ensure_ascii=False, indent=2),
        "base_file_digest": document["file_digest"],
        "base_layer_revision": document["layer_revision"],
        "base_layer_digest": document["layer_digest"],
        "expected_active_revision": document["active_revision"],
        "expected_active_digest": document["active_digest"],
        "idempotency_key": idempotency_key,
    }))
    assert response["data"]["edit"]["state"] == "active"
    return response


def _wait_for_plugin(
    client: httpx.Client,
    *,
    gateway_id: str,
    workspace_id: str,
    state: str,
    timeout_seconds: float = 90,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        payload = _json(client.post("/api/gateway/service-plugins/status", json={"gateway_id": gateway_id}))["data"]
        if not isinstance(payload, dict):
            raise TypeError("插件状态缺少 data 对象")
        last = next((item for item in payload.get("plugins", [])
            if item.get("binding", {}).get("plugin_id") == PLUGIN_ID
            and item.get("binding", {}).get("workspace_id") == workspace_id), None)
        if last is not None and last.get("state") == state:
            return last
        time.sleep(0.2)
    raise TimeoutError(f"Terminal 插件状态未收敛: expected={state}, last={last}")


def _receive_output(socket, marker: str, *, timeout_seconds: float = 15) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    observed: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        message = json.loads(socket.recv(timeout=max(0.1, deadline - time.monotonic())))
        observed.append(message)
        if message.get("type") == "output" and marker in str(message.get("data", "")):
            return message
    raise TimeoutError(f"Terminal 没有输出命令标记 {marker!r}: {observed}")


def _wait_for_close(socket, *, timeout_seconds: float = 10) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            socket.recv(timeout=max(0.1, deadline - time.monotonic()))
        except ConnectionClosed:
            return
    raise TimeoutError("Terminal WebSocket 未在期限内观察到 owner 版本撤权")


def _run_terminal_page(
    *,
    project_root: Path,
    context: TestRunContext,
    gateway_url: str,
    gateway_id: str,
    workspace_id: str,
    resource_id: str,
    instance_id: str,
    cookies: list[dict[str, Any]],
) -> dict[str, Any]:
    executable = (
        os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
    )
    if executable is None:
        raise FileNotFoundError("Terminal 页面验收需要可用的 Chromium")
    artifacts = context.artifacts_dir / "terminal-plugin-page"
    artifacts.mkdir(parents=True, exist_ok=True)
    input_path = artifacts / "input.json"
    input_path.write_text(json.dumps({
        "gatewayUrl": gateway_url,
        "gatewayId": gateway_id,
        "workspaceId": workspace_id,
        "resourceId": resource_id,
        "instanceId": instance_id,
        "artifactsDir": str(artifacts.resolve()),
        "chromiumExecutable": str(Path(executable).resolve()),
        "cookies": cookies,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    command = ["bun", "tests/integration/gateway/terminal_plugin_page.mjs", str(input_path.resolve())]
    protected = ["timeout", "180", "bash", "-c", 'ulimit -d 4194304; exec "$@"', "bash", *command]
    with chromium_temporary_environment(artifacts / "chromium-tmp") as temporary:
        result = subprocess.run(
            protected,
            cwd=project_root,
            env={
                **os.environ,
                **temporary,
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": str(Path(executable).resolve()),
            },
            check=False,
            capture_output=True,
            text=True,
            timeout=195,
        )
    (artifacts / "browser.stdout.raw").write_text(result.stdout, encoding="utf-8")
    (artifacts / "browser.stderr.raw").write_text(result.stderr, encoding="utf-8")
    (artifacts / "browser-process.json").write_text(json.dumps({
        "command": command,
        "protected_command": protected,
        "exit_code": result.returncode,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads((artifacts / "result.json").read_text(encoding="utf-8"))
    assert observed["gatewayId"] == gateway_id
    assert observed["workspaceId"] == workspace_id
    assert observed["resourceId"] == resource_id
    assert observed["instanceId"] == instance_id
    assert observed["pageErrors"] == []
    return observed


def _wait_for_owner_revision(
    client: httpx.Client,
    *,
    workspace_id: str,
    resource_id: str,
    previous_revision: int,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    headers = {"X-BoxTeam-Workspace-Id": workspace_id}
    while time.monotonic() < deadline:
        resource = _json(client.get(f"/api/v1/resources/{resource_id}", headers=headers))["data"]
        if resource["owner_revision"] > previous_revision:
            return resource
        time.sleep(0.1)
    raise TimeoutError(f"Workspace owner 未递增 Terminal owner_revision: resource_id={resource_id}")


def test_terminal_package_runs_through_gateway_owner_and_exact_instance_routes(
    request: pytest.FixtureRequest,
    unused_tcp_port: int,
) -> None:
    project_root = Path.cwd().resolve()
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    context.artifacts_dir.mkdir(parents=True, exist_ok=True)
    template = project_root / "tests/fixtures/workspaces/default_test_workspace"
    workspace_config = project_root / "configs/tests/workspace/default.jsonc"
    workspace_schema = project_root / "configs/workspace_schema.jsonc"
    gateway_workspace = prepare_test_workspace(
        workspace_root=context.workspace_root / "gateway-default",
        template_root=template,
        template_items=(),
    )
    workspace = prepare_test_workspace(
        workspace_root=context.workspace_root / "terminal-workspace",
        template_root=template,
        template_items=(),
    )
    for root in (gateway_workspace, workspace):
        install_test_workspace_config(
            workspace_root=root,
            config_path=workspace_config,
            schema_path=workspace_schema,
        )
        config_path = root / ".boxteam/workspace.jsonc"
        config = commentjson.loads(config_path.read_text(encoding="utf-8"))
        config.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})[
            "url"
        ] = f"http://127.0.0.1:{unused_tcp_port}"
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    gateway = start_gateway_process(
        workspace_root=gateway_workspace,
        default_backend_url=None,
        port=unused_tcp_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid),
        artifacts_dir=context.artifacts_dir,
    )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
            trust_env=False,
        ) as client:
            _json(client.post("/api/gateway/users/guest", json={"tracking": {"test": True}}))
            gateway_id = str(_json(client.get("/api/gateway/health"))["data"]["gateway_id"])
            managed = _json(client.post("/api/gateway/managed-workspaces", json={
                "root_path": str(workspace),
                "name": "Terminal plugin lifecycle",
                "create_directory": False,
            }, timeout=75))["data"]
            workspace_id = next(
                item["workspace_id"] for item in managed["items"]
                if item["gateway_id"] == gateway_id and item["name"] == "Terminal plugin lifecycle"
            )
            archive = context.artifacts_dir / "boxteam.developer-tools-0.1.0.zip"
            _build_package(project_root, archive)
            install = _json(client.post("/api/gateway/service-plugins/install", json={
                "gateway_id": gateway_id,
                "path": str(archive),
                "expected_digest": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }))
            assert install["data"]["manifest"]["package_id"] == PACKAGE_ID

            _cas_workspace_binding(client, gateway_id=gateway_id, workspace_id=workspace_id,
                enabled=True, idempotency_key="terminal-plugin-lifecycle-enable")
            ready = _wait_for_plugin(client, gateway_id=gateway_id, workspace_id=workspace_id, state="ready")
            old_instance = ready["instance"]
            assert old_instance["services"] == [
                {"service_id": "http", "service_type": "terminal", "protocol": "http"},
                {"service_id": "socket", "service_type": "terminal", "protocol": "websocket"},
            ]

            # 旧 Terminal Manager 代理已下线；产品只能经 Gateway 管理的正式插件实例访问。
            legacy_http = client.get(
                f"/api/gateway/workspaces/{workspace_id}/terminal-manager/health",
            )
            assert legacy_http.status_code == 404, legacy_http.text
            legacy_socket_url = (
                f"ws://127.0.0.1:{gateway.port}/api/gateway/workspaces/"
                f"{workspace_id}/terminal-manager/terminal"
            )
            with pytest.raises(InvalidStatus) as legacy_socket:
                connect(legacy_socket_url, open_timeout=5, close_timeout=1)
            # 已删除的 WebSocket 路由沿 Starlette 的未接受连接合同返回 403。
            assert legacy_socket.value.response.status_code == 403

            plugin_http_root = (
                f"/api/gateway/service-plugins/{PACKAGE_ID}/{PLUGIN_ID}/instances/"
                f"{old_instance['instance_id']}/services/http"
            )
            plugin_query = {"package_version": PACKAGE_VERSION, "workspace_id": workspace_id}
            missing_owner = client.get(
                f"{plugin_http_root}/api/terminals", params=plugin_query,
                headers={"X-BoxTeam-Workspace-Id": workspace_id},
            )
            assert missing_owner.status_code == 403, missing_owner.text

            workspace_headers = {"X-BoxTeam-Workspace-Id": workspace_id}
            session = _json(client.post("/api/v1/sessions", headers=workspace_headers,
                json={"title": "Gateway Terminal plugin lifecycle"}))["data"]
            resource = _json(client.post("/api/v1/resources/terminals", headers=workspace_headers, json={
                "session_id": session["session_id"],
                "thread_id": session["thread_id"],
                "retention_scope": "workspace",
                "title": "Gateway plugin PTY",
                "cwd_relative": ".",
                "cols": 100,
                "rows": 30,
            }, timeout=45))["data"]
            assert resource["status"] == "running"
            assert resource["retention_scope"] == "workspace"
            grant = _json(client.post(
                f"/api/v1/resources/{resource['resource_id']}/transport-grant",
                headers=workspace_headers,
                json={"expected_revision": resource["owner_revision"]},
            ))["data"]
            service = grant["service"]
            unused_pre_restart_grant = _json(client.post(
                f"/api/v1/resources/{resource['resource_id']}/transport-grant",
                headers=workspace_headers,
                json={"expected_revision": resource["owner_revision"]},
            ))["data"]
            assert service == {
                "gateway_id": gateway_id,
                "package_id": PACKAGE_ID,
                "package_version": PACKAGE_VERSION,
                "plugin_id": PLUGIN_ID,
                "instance_id": old_instance["instance_id"],
                "service_id": "socket",
                "workspace_id": workspace_id,
            }
            query = urlencode({
                "workspace_id": service["workspace_id"],
                "package_version": service["package_version"],
                "token": client.headers["X-Local-Token"],
                "target_gateway_id": service["gateway_id"],
            })
            old_socket_url = (
                f"ws://127.0.0.1:{gateway.port}/api/gateway/service-plugins/{PACKAGE_ID}/{PLUGIN_ID}"
                f"/instances/{service['instance_id']}/services/socket/terminal?{query}"
            )
            socket = connect(old_socket_url, open_timeout=10, close_timeout=3)
            try:
                socket.send(json.dumps({
                    "type": "attach",
                    "terminalId": resource["resource_id"],
                    "grantToken": grant["grant_token"],
                    "cols": 100,
                    "rows": 30,
                }))
                attached = json.loads(socket.recv(timeout=10))
                assert attached["type"] == "attached"
                socket.send(json.dumps({"type": "resize", "cols": 120, "rows": 45}))
                resize_deadline = time.monotonic() + 5
                while True:
                    resized = _json(client.get(
                        f"/api/v1/resources/{resource['resource_id']}", headers=workspace_headers,
                    ))["data"]
                    if (resized["metadata"]["cols"], resized["metadata"]["rows"]) == (120, 45):
                        break
                    assert time.monotonic() < resize_deadline, resized
                    time.sleep(0.05)
                marker = f"gateway-plugin-{time.time_ns()}"
                socket.send(json.dumps({"type": "input", "data": f"printf '{marker}\\n'\n"}))
                output = _receive_output(socket, marker)
                assert output["terminalId"] == resource["resource_id"]
                page = _run_terminal_page(
                    project_root=project_root,
                    context=context,
                    gateway_url=f"http://127.0.0.1:{gateway.port}",
                    gateway_id=gateway_id,
                    workspace_id=workspace_id,
                    resource_id=resource["resource_id"],
                    instance_id=old_instance["instance_id"],
                    cookies=[{"name": cookie.name, "value": cookie.value,
                              "url": f"http://127.0.0.1:{gateway.port}"} for cookie in client.cookies.jar],
                )
                assert str(page["marker"]).startswith("terminal-page-")

                client.post(
                    f"/api/gateway/workspaces/{workspace_id}/runtime/restart-safe",
                    timeout=150,
                ).raise_for_status()
                after_backend_restart = _wait_for_plugin(
                    client, gateway_id=gateway_id, workspace_id=workspace_id, state="ready",
                )
                assert after_backend_restart["instance"]["instance_id"] == old_instance["instance_id"]
                assert after_backend_restart["instance"]["process_id"] == old_instance["process_id"]
                recovered_resource = _wait_for_owner_revision(
                    client, workspace_id=workspace_id, resource_id=resource["resource_id"],
                    previous_revision=grant["owner_revision"],
                )
                assert recovered_resource["resource_id"] == resource["resource_id"]
                assert recovered_resource["workspace_id"] == workspace_id
                assert recovered_resource["metadata"]["os_pid"] == resource["metadata"]["os_pid"]
                _wait_for_close(socket)

                socket.close()
                stale_socket = connect(old_socket_url, open_timeout=10, close_timeout=3)
                stale_socket.send(json.dumps({
                    "type": "attach",
                    "terminalId": resource["resource_id"],
                    "grantToken": unused_pre_restart_grant["grant_token"],
                }))
                stale_grant = json.loads(stale_socket.recv(timeout=5))
                assert stale_grant["type"] == "error", stale_grant
                stale_socket.close()

                recovered_grant = _json(client.post(
                    f"/api/v1/resources/{resource['resource_id']}/transport-grant",
                    headers=workspace_headers,
                    json={"expected_revision": recovered_resource["owner_revision"]},
                ))["data"]
                assert recovered_grant["owner_revision"] == recovered_resource["owner_revision"]
                assert recovered_grant["service"] == service
                socket = connect(old_socket_url, open_timeout=10, close_timeout=3)
                socket.send(json.dumps({
                    "type": "attach",
                    "terminalId": resource["resource_id"],
                    "grantToken": recovered_grant["grant_token"],
                    "cols": 100,
                    "rows": 30,
                }))
                reattached = json.loads(socket.recv(timeout=10))
                assert reattached["type"] == "attached", reattached
                marker_after_restart = f"gateway-backend-restart-{time.time_ns()}"
                socket.send(json.dumps({"type": "input", "data": f"printf '{marker_after_restart}\\n'\n"}))
                _receive_output(socket, marker_after_restart)

                closed = _json(client.post(
                    f"/api/v1/resources/{resource['resource_id']}/stop", headers=workspace_headers,
                    json={"expected_revision": recovered_resource["owner_revision"]},
                ))["data"]
                assert closed["status"] == "terminated", closed
                assert closed["reconciliation_status"] == "verified", closed
                _wait_for_close(socket)

                restarted = _json(client.post("/api/gateway/service-plugins/restart", json={
                    "gateway_id": gateway_id,
                    "workspace_id": workspace_id,
                    "package_id": PACKAGE_ID,
                    "package_version": PACKAGE_VERSION,
                    "plugin_id": PLUGIN_ID,
                    "revision": after_backend_restart["revision"],
                }))["data"]
                new_instance = restarted["instance"]
                assert new_instance["instance_id"] != old_instance["instance_id"]
                _wait_for_close(socket)
                with pytest.raises(InvalidStatus) as old_route:
                    connect(old_socket_url, open_timeout=5, close_timeout=1)
                assert old_route.value.response.status_code == 409
                old_http_route = (
                    f"/api/gateway/service-plugins/{PACKAGE_ID}/{PLUGIN_ID}/instances/"
                    f"{old_instance['instance_id']}/services/http/api/terminals"
                    f"?package_version={PACKAGE_VERSION}&workspace_id={workspace_id}"
                )
                assert client.get(old_http_route, headers={
                    "X-BoxTeam-Resource-Owner": "workspace-terminal",
                }).status_code == 409

                _cas_workspace_binding(client, gateway_id=gateway_id, workspace_id=workspace_id,
                    enabled=False, idempotency_key="terminal-plugin-lifecycle-disable")
                stopped = _wait_for_plugin(client, gateway_id=gateway_id, workspace_id=workspace_id, state="stopped")
                assert stopped["instance"] is None
                blocked = client.post("/api/gateway/service-plugins/uninstall", json={
                    "gateway_id": gateway_id,
                    "package_id": PACKAGE_ID,
                    "package_version": PACKAGE_VERSION,
                })
                assert blocked.status_code == 409, blocked.text
            finally:
                socket.close()
    finally:
        close_gateway_process(gateway)
