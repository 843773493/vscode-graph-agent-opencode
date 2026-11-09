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
        for provider in payload["llm"]["providers"]:
            if "api_key" in provider:
                provider["api_key"] = "plugin-workspace-unused-key"
        payload.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = gateway_url
        config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    gateway = start_gateway_process(workspace_root=gateway_workspace, default_backend_url=None,
        port=unused_tcp_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS,
            timeout=30, trust_env=False) as client:
            # 当前提交的 Gateway 业务代理要求正式游客会话；沿公开首载入口取得 Cookie。
            client.get("/api/gateway/users/current").raise_for_status()
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
            "requires": [{"extension_point": "services", "version": 1}, {"extension_point": "configuration", "version": 1}],
            "contributes": {"services": [{"service_id": "echo", "service_type": "terminal", "protocol": "http"}],
                "configuration": {"type": "object", "additionalProperties": False,
                    "properties": {"label": {"type": "string"}, "credentialValue": {"type": "string", "writeOnly": True}}}},
            "runtime": "python",
            "entrypoint": ["nested/entry.py", mode] if mode else ["nested/entry.py"],
            "management_protocol_version": 1,
        }],
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest))
        archive.writestr("nested/entry.py", PYTHON_PLUGIN.replace(
            "'pid': os.getpid(), 'instance_id': identity['instance_id'],",
            "'parameters': json.loads(os.environ['BOXTEAM_PLUGIN_PARAMETERS']), 'pid': os.getpid(), 'instance_id': identity['instance_id'],"))
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

    def write_binding(workspace_id: str, entries: list[dict[str, object]], key: str,
                      parameters: dict[str, object] | None = None, expected_status: int = 200) -> None:
        path = f"/api/v1/workspaces/{workspace_id}/config/sources/workspace_root_mutable_override"
        headers = {"X-BoxTeam-Workspace-Id": workspace_id}
        document = exchange("GET", path, headers=headers).raise_for_status().json()["data"]
        source = commentjson.loads(document["jsonc"])
        source.setdefault("plugins", {})["runtime"] = {"entries": entries}
        if parameters is not None:
            source["plugins"]["configuration"] = {"values": {"probe.tools": {"probe": parameters}}}
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
        assert response.status_code == expected_status, response.text
        if expected_status != 200:
            after = exchange("GET", path, headers=headers).raise_for_status().json()["data"]
            assert after["jsonc"] == document["jsonc"]
            assert after["effective"] == document["effective"]
            return
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

        write_binding(workspace_a_id, [binding_for("probe.tools")], "workspace-a-plugin-enable", {"label": "A"})
        write_binding(workspace_b_id, [binding_for("probe.tools")], "workspace-b-plugin-enable", {"label": "B"})
        ready_a, ready_b = wait_binding(workspace_a_id, "ready"), wait_binding(workspace_b_id, "ready")
        assert ready_a["instance"]["process_id"] != ready_b["instance"]["process_id"]
        assert ready_a["instance"]["instance_id"] != ready_b["instance"]["instance_id"]
        assert call(ready_a)["pid"] == ready_a["instance"]["process_id"]
        assert call(ready_b)["pid"] == ready_b["instance"]["process_id"]
        assert call(ready_a)["parameters"] == {"label": "A"}
        assert call(ready_b)["parameters"] == {"label": "B"}

        write_binding(workspace_a_id, [binding_for("probe.tools")], "workspace-a-private-parameters",
            {"credentialValue": "must-not-reach-source"}, expected_status=409)

        write_binding(workspace_a_id, [binding_for("probe.tools")], "workspace-a-plugin-parameters", {"label": "A changed"})
        changed_a = _wait_for(lambda value: (item := status_for(value, workspace_a_id)) is not None
            and item["state"] == "ready" and item["instance"]["instance_id"] != ready_a["instance"]["instance_id"])
        assert_process_exited(ready_a["instance"]["process_id"])
        ready_a = status_for(changed_a, workspace_a_id)
        assert ready_a is not None and call(ready_a)["parameters"] == {"label": "A changed"}
        assert wait_binding(workspace_b_id, "ready")["instance"]["instance_id"] == ready_b["instance"]["instance_id"]

        restarted = exchange("POST", f"/api/gateway/workspaces/{workspace_a_id}/runtime/restart-safe",
            timeout=150).raise_for_status()
        assert restarted.status_code == 200
        same_a = wait_binding(workspace_a_id, "ready")
        assert same_a["instance"]["instance_id"] == ready_a["instance"]["instance_id"]
        assert call(same_a)["parameters"] == {"label": "A changed"}
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
        assert call(ready_a)["parameters"] == {"label": "A changed"}
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


def test_new_workspace_freezes_defaults_without_changing_existing_workspaces(workspace_plugin_case, request: pytest.FixtureRequest) -> None:
    """模板提交、创建、配置交接与卸载引用均经过真实产品入口。"""
    client, context, workspaces = workspace_plugin_case
    gateway_id = client.get("/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]
    evidence: list[dict[str, object]] = []

    def exchange(method: str, path: str, body=None):
        request_id = f"workspace-default-{len(evidence)}"
        response = client.request(method, path, json=body, headers={"X-Request-ID": request_id}, timeout=75)
        assert response.headers["X-Request-ID"] == request_id
        evidence.append({"method": method, "path": path, "status": response.status_code, "body": response.json()})
        return response

    def defaults(entries):
        path = "/api/gateway/config/sources/gateway_mutable_override"
        document = exchange("GET", path).raise_for_status().json()["data"]
        source = commentjson.loads(document["jsonc"])
        source.setdefault("plugins", {})["workspace_defaults"] = {"entries": entries}
        return exchange("PUT", path, {
            "gateway_id": gateway_id, "source_key": "gateway_mutable_override",
            "jsonc": json.dumps(source), "base_file_digest": document["file_digest"],
            "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
            "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"],
            "idempotency_key": f"defaults-{len(evidence)}",
        })

    def create(root, name, *, new):
        response = exchange("POST", "/api/gateway/managed-workspaces", {
            "root_path": str(root), "name": name, "create_directory": new,
        }).raise_for_status().json()["data"]
        return next(item["workspace_id"] for item in response["items"] if item["name"] == name)

    def document(workspace_id):
        return exchange("GET", f"/api/v1/workspaces/{workspace_id}/config/sources/workspace_root_mutable_override").raise_for_status().json()["data"]

    binding = {"package_id": "probe.defaults", "package_version": "1.0.0", "plugin_id": "probe", "enabled": True}
    try:
        # 新目录尚无配置；只给用户配置提供本测试的服务地址及不调用模型的依赖。
        config_path = context.boxteam_home_for_node(request.node.nodeid) / "config/workspace.jsonc"
        source = commentjson.loads((workspaces[0] / ".boxteam/workspace.jsonc").read_text(encoding="utf-8"))
        source["llm"]["providers"] = [{"id": "primary", "model": "unused", "endpoint": "http://127.0.0.1:1/v1",
            "api_key": "unused", "custom_llm_provider": "openai", "api_mode": {"protocol": "chat_completions",
                "model_info": {"supports_function_calling": True, "supports_reasoning": False},
                "supports_reasoning": {"reasoning_content": False}}}]
        for agent in source["agents"].values():
            if "model" in agent:
                agent["model"] = {"primary_provider": "primary", "fallback_providers": []}
            for tool in agent.get("tools", {}).get("custom", []):
                if tool.get("name") == "fetch_webpage":
                    tool.setdefault("embedding", {})["provider_id"] = "primary"
        config_path.write_text(json.dumps(source), encoding="utf-8")

        missing = defaults([binding])
        assert missing.status_code == 422, missing.text
        archive = _package(context.artifacts_dir / "defaults.zip", package_id="probe.defaults")
        exchange("POST", "/api/gateway/service-plugins/install", {"gateway_id": gateway_id,
            "path": str(archive), "expected_digest": hashlib.sha256(archive.read_bytes()).hexdigest()}).raise_for_status()
        saved = defaults([binding]).raise_for_status().json()["data"]
        assert saved["edit"]["state"] == "active"
        assert defaults([binding, binding]).status_code == 422
        template_denied = exchange("POST", "/api/gateway/service-plugins/uninstall", {"gateway_id": gateway_id,
            "package_id": "probe.defaults", "package_version": "1.0.0"})
        assert template_denied.status_code == 409 and "workspace-default" in template_denied.text, template_denied.text
        existing_id = create(workspaces[0], "Existing", new=False)
        assert document(existing_id)["effective"]["plugins"]["runtime"]["entries"] == []
        new_root = context.workspace_root / f"new-project-{context.run_root.name}"
        assert not new_root.exists()
        new_id = create(new_root, "New", new=True)
        deadline = time.monotonic() + 45
        while True:
            state = exchange("POST", "/api/gateway/service-plugins/status", {"gateway_id": gateway_id}).raise_for_status().json()["data"]
            slot = next((item for item in state["plugins"] if item["binding"].get("workspace_id") == new_id), None)
            if slot is not None and slot["state"] == "ready":
                break
            assert time.monotonic() < deadline, state
            time.sleep(0.1)
        assert document(new_id)["effective"]["plugins"]["runtime"]["entries"] == [binding]
        defaults([]).raise_for_status()
        assert document(new_id)["effective"]["plugins"]["runtime"]["entries"] == [binding]
        assert document(existing_id)["effective"]["plugins"]["runtime"]["entries"] == []
        denied = exchange("POST", "/api/gateway/service-plugins/uninstall", {"gateway_id": gateway_id,
            "package_id": "probe.defaults", "package_version": "1.0.0"})
        assert denied.status_code == 409 and new_id in denied.text, denied.text
        source_doc = document(new_id)
        config = commentjson.loads(source_doc["jsonc"])
        config["plugins"]["runtime"]["entries"] = []
        exchange("PUT", f"/api/v1/workspaces/{new_id}/config/sources/workspace_root_mutable_override", {
            "gateway_id": gateway_id, "workspace_id": new_id, "source_key": "workspace_root_mutable_override",
            "jsonc": json.dumps(config), "base_file_digest": source_doc["file_digest"],
            "base_layer_revision": source_doc["layer_revision"], "base_layer_digest": source_doc["layer_digest"],
            "expected_active_revision": source_doc["active_revision"], "expected_active_digest": source_doc["active_digest"],
            "idempotency_key": "remove-created-binding",
        }).raise_for_status()
        deadline = time.monotonic() + 20
        while True:
            state = exchange("POST", "/api/gateway/service-plugins/status", {"gateway_id": gateway_id}).raise_for_status().json()["data"]
            if not any(item["binding"].get("workspace_id") == new_id for item in state["plugins"]):
                break
            assert time.monotonic() < deadline, state
            time.sleep(0.1)
        exchange("POST", "/api/gateway/service-plugins/uninstall", {"gateway_id": gateway_id,
            "package_id": "probe.defaults", "package_version": "1.0.0"}).raise_for_status()
    finally:
        (context.artifacts_dir / "workspace-defaults-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
