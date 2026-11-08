"""真实 Gateway／Workspace 插件配置交接与独立实例生命周期。"""

from __future__ import annotations

import hashlib
import json
import time
import zipfile
from pathlib import Path

import commentjson
import httpx
import psutil
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.integration.gateway.test_service_plugins import PYTHON_PLUGIN
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)


@pytest.fixture
def workspace_plugin_case(request: pytest.FixtureRequest, unused_tcp_port: int):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    template = Path.cwd() / "tests/fixtures/workspaces/default_test_workspace"
    config = Path.cwd() / "configs/tests/workspace/default.jsonc"
    schema = Path.cwd() / "configs/workspace_schema.jsonc"
    gateway_workspace = prepare_test_workspace(workspace_root=context.workspace_root / "gateway-default",
        template_root=template, template_items=())
    install_test_workspace_config(workspace_root=gateway_workspace, config_path=config, schema_path=schema)
    workspaces = [
        prepare_test_workspace(workspace_root=context.workspace_root / name,
            template_root=template, template_items=())
        for name in ("workspace-a", "workspace-b")
    ]
    for workspace in workspaces:
        install_test_workspace_config(workspace_root=workspace, config_path=config, schema_path=schema)

    gateway_url = f"http://127.0.0.1:{unused_tcp_port}"
    for workspace in [gateway_workspace, *workspaces]:
        config_path = workspace / ".boxteam/workspace.jsonc"
        payload = commentjson.loads(config_path.read_text(encoding="utf-8"))
        payload.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = gateway_url
        config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    gateway = start_gateway_process(workspace_root=gateway_workspace, default_backend_url=None,
        port=unused_tcp_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS,
            timeout=30, trust_env=False) as client:
            yield client, context, workspaces
    finally:
        close_gateway_process(gateway)


def _package(path: Path, *, package_id: str, mode: str = "") -> Path:
    manifest = {
        "manifest_version": 1,
        "package_id": package_id,
        "version": "1.0.0",
        "name": "Workspace 生命周期探针",
        "extension_contract_version": 1,
        "plugins": [{
            "id": "probe",
            "name": "Workspace 进程探针",
            "instance_scope": "workspace",
            "requires": [{"extension_point": "services", "version": 1}],
            "contributes": {"services": [{"service_id": "echo", "service_type": "terminal", "protocol": "http"}]},
            "runtime": "python",
            "entrypoint": ["nested/entry.py", mode] if mode else ["nested/entry.py"],
            "management_protocol_version": 1,
        }],
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest))
        archive.writestr("nested/entry.py", PYTHON_PLUGIN)
        archive.writestr("nested/local_module.py", "VALUE = 1\n")
    return path


def test_workspace_plugins_follow_owner_configuration_and_independent_lifecycle(
    workspace_plugin_case,
) -> None:
    client, context, workspaces = workspace_plugin_case
    evidence: list[dict[str, object]] = []
    request_index = 0

    def exchange(method: str, path: str, body: dict[str, object] | None = None,
        *, headers: dict[str, str] | None = None, timeout: float | None = None) -> httpx.Response:
        nonlocal request_index
        request_index += 1
        request_id = f"workspace-plugin-{request_index}"
        request_options = {"timeout": timeout} if timeout is not None else {}
        response = client.request(method, path, json=body,
            headers={"X-Request-ID": request_id, **(headers or {})}, **request_options)
        evidence.append({"method": method, "path": path, "status": response.status_code,
            "request_id": request_id, "body": response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text})
        assert response.headers.get("X-Request-ID") == request_id
        if response.headers.get("content-type", "").startswith("application/json"):
            payload = response.json()
            if "request_id" in payload:
                assert payload["request_id"] == request_id
        return response

    def gateway_id() -> str:
        return str(exchange("GET", "/api/gateway/health").raise_for_status().json()["data"]["gateway_id"])

    def workspace_identity(workspace_id: str) -> str:
        response = exchange("GET", f"/api/v1/workspaces/{workspace_id}/workspace").raise_for_status()
        return str(response.json()["data"]["workspace_id"])

    def _wait_for(predicate, *, timeout: float = 60) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        last: dict[str, object] = {}
        while time.monotonic() < deadline:
            response = exchange("POST", "/api/gateway/service-plugins/status", {"gateway_id": gateway_id()})
            response.raise_for_status()
            last = response.json()["data"]
            if predicate(last):
                return last
            time.sleep(0.2)
        raise AssertionError(f"Workspace 插件状态未在期限内收敛: {last}")

    def status_for(data: dict[str, object], workspace_id: str) -> dict[str, object] | None:
        return next((item for item in data["plugins"]
            if item["binding"].get("workspace_id") == workspace_id and item["binding"]["plugin_id"] == "probe"), None)

    def binding_for(package_id: str, *, enabled: bool = True) -> dict[str, object]:
        return {"package_id": package_id, "package_version": "1.0.0", "plugin_id": "probe", "enabled": enabled}

    def write_binding(workspace_id: str, entries: list[dict[str, object]], key: str) -> None:
        path = f"/api/v1/workspaces/{workspace_id}/config/sources/workspace_root_mutable_override"
        headers = {"X-BoxTeam-Workspace-Id": workspace_id}
        document = exchange("GET", path, headers=headers).raise_for_status().json()["data"]
        source = commentjson.loads(document["jsonc"])
        source.setdefault("plugins", {})["runtime"] = {"entries": entries}
        response = exchange("PUT", path, {
            "gateway_id": gateway_id(),
            "workspace_id": workspace_id,
            "source_key": "workspace_root_mutable_override",
            "jsonc": json.dumps(source, ensure_ascii=False, indent=2),
            "base_file_digest": document["file_digest"],
            "base_layer_revision": document["layer_revision"],
            "base_layer_digest": document["layer_digest"],
            "expected_active_revision": document["active_revision"],
            "expected_active_digest": document["active_digest"],
            "idempotency_key": key,
        }, headers=headers)
        response.raise_for_status()
        assert response.json()["data"]["edit"]["state"] == "active"

    def wait_binding(workspace_id: str, state: str) -> dict[str, object]:
        data = _wait_for(lambda value: (item := status_for(value, workspace_id)) is not None and item["state"] == state)
        item = status_for(data, workspace_id)
        assert item is not None
        return item

    def wait_workspace_state(workspace_id: str, state: str) -> dict[str, object]:
        data = _wait_for(lambda value: any(item["workspace_id"] == workspace_id and item["state"] == state
            for item in value.get("workspace_configurations", [])))
        return next(item for item in data["workspace_configurations"] if item["workspace_id"] == workspace_id)

    def assert_process_exited(process_id: int) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                process = psutil.Process(process_id)
            except psutil.NoSuchProcess:
                return
            if process.status() == psutil.STATUS_ZOMBIE:
                return
            time.sleep(0.05)
        pytest.fail(f"Workspace 插件进程没有退出: pid={process_id}")

    def call(item: dict[str, object]) -> dict[str, object]:
        binding, instance = item["binding"], item["instance"]
        path = (f"/api/gateway/service-plugins/{binding['package_id']}/probe/instances/"
            f"{instance['instance_id']}/services/echo/value")
        return exchange("GET", path + "?package_version=1.0.0&workspace_id=" + binding["workspace_id"]).raise_for_status().json()

    def call_response(item: dict[str, object]) -> httpx.Response:
        binding, instance = item["binding"], item["instance"]
        path = (f"/api/gateway/service-plugins/{binding['package_id']}/probe/instances/"
            f"{instance['instance_id']}/services/echo/value")
        return exchange("GET", path + "?package_version=1.0.0&workspace_id=" + binding["workspace_id"])

    try:
        # 两个 Backend 都由真实 Gateway 管理入口创建并启动。
        target_gateway = gateway_id()
        workspace_ids: list[str] = []
        for workspace, name in zip(workspaces, ("Workspace A", "Workspace B"), strict=True):
            created = exchange("POST", "/api/gateway/managed-workspaces", {
                "root_path": str(workspace), "name": name, "create_directory": False,
            }, timeout=75).raise_for_status().json()["data"]
            workspace_ids.append(str(next(item["workspace_id"] for item in created["items"]
                if item["gateway_id"] == target_gateway and item["name"] == name)))
        workspace_a_id, workspace_b_id = workspace_ids
        assert workspace_identity(workspace_a_id) == workspace_a_id
        assert workspace_identity(workspace_b_id) == workspace_b_id

        for package_id, mode in (("probe.tools", ""), ("probe.bad", "bad-ready")):
            archive = _package(context.artifacts_dir / f"{package_id}.zip", package_id=package_id, mode=mode)
            exchange("POST", "/api/gateway/service-plugins/install", {
                "gateway_id": target_gateway, "path": str(archive),
                "expected_digest": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }).raise_for_status()

        write_binding(workspace_a_id, [binding_for("probe.tools")], "workspace-a-plugin-enable")
        write_binding(workspace_b_id, [binding_for("probe.tools")], "workspace-b-plugin-enable")
        ready_a, ready_b = wait_binding(workspace_a_id, "ready"), wait_binding(workspace_b_id, "ready")
        assert ready_a["instance"]["process_id"] != ready_b["instance"]["process_id"]
        assert ready_a["instance"]["instance_id"] != ready_b["instance"]["instance_id"]
        assert call(ready_a)["pid"] == ready_a["instance"]["process_id"]
        assert call(ready_b)["pid"] == ready_b["instance"]["process_id"]

        restarted = exchange("POST", f"/api/gateway/workspaces/{workspace_a_id}/runtime/restart-safe",
            timeout=150).raise_for_status()
        assert restarted.status_code == 200
        same_a = wait_binding(workspace_a_id, "ready")
        assert same_a["instance"]["instance_id"] == ready_a["instance"]["instance_id"]
        assert wait_binding(workspace_b_id, "ready")["instance"]["instance_id"] == ready_b["instance"]["instance_id"]

        restarted_plugin = exchange("POST", "/api/gateway/service-plugins/restart", {
            "gateway_id": target_gateway,
            "workspace_id": workspace_a_id,
            "package_id": "probe.tools",
            "package_version": "1.0.0",
            "plugin_id": "probe",
            "revision": same_a["revision"],
        }).raise_for_status().json()["data"]
        assert restarted_plugin["instance"]["instance_id"] != same_a["instance"]["instance_id"]
        assert_process_exited(same_a["instance"]["process_id"])
        assert call_response(same_a).status_code == 409, "插件重启后旧 instance_id 必须失效，不能改投新实例"
        ready_a = restarted_plugin
        assert wait_binding(workspace_b_id, "ready")["instance"]["instance_id"] == ready_b["instance"]["instance_id"]

        write_binding(workspace_a_id, [binding_for("probe.tools", enabled=False)], "workspace-a-plugin-disable")
        assert wait_binding(workspace_a_id, "stopped")["instance"] is None
        assert_process_exited(ready_a["instance"]["process_id"])
        still_b = wait_binding(workspace_b_id, "ready")
        assert still_b["instance"]["instance_id"] == ready_b["instance"]["instance_id"]
        assert call(still_b)["pid"] == ready_b["instance"]["process_id"]

        write_binding(workspace_a_id, [binding_for("probe.tools")], "workspace-a-plugin-reenable")
        enabled_a = wait_binding(workspace_a_id, "ready")
        blocked = exchange("POST", "/api/gateway/service-plugins/uninstall", {
            "gateway_id": target_gateway, "package_id": "probe.tools", "package_version": "1.0.0",
        })
        assert blocked.status_code == 409

        exchange("POST", f"/api/gateway/workspaces/{workspace_a_id}/runtime/stop", timeout=150).raise_for_status()
        assert wait_workspace_state(workspace_a_id, "stopped")["state"] == "stopped"
        assert_process_exited(enabled_a["instance"]["process_id"])
        assert wait_binding(workspace_b_id, "ready")["instance"]["instance_id"] == ready_b["instance"]["instance_id"]
        exchange("POST", f"/api/gateway/workspaces/{workspace_a_id}/runtime/start", timeout=75).raise_for_status()
        restored_a = wait_binding(workspace_a_id, "ready")
        assert restored_a["instance"]["instance_id"] != enabled_a["instance"]["instance_id"]

        # 坏 ready 只使 A 的新绑定失败；B 的已有进程和调用继续可用。
        write_binding(workspace_a_id, [binding_for("probe.bad")], "workspace-a-bad-ready")
        failed_a = wait_binding(workspace_a_id, "failed")
        assert failed_a["binding"]["package_id"] == "probe.bad"
        final_b = wait_binding(workspace_b_id, "ready")
        assert final_b["instance"]["instance_id"] == ready_b["instance"]["instance_id"]
        assert call(final_b)["pid"] == ready_b["instance"]["process_id"]
    finally:
        (context.artifacts_dir / "workspace-plugin-flow.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
