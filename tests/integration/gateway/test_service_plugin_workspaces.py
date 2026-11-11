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

from app.schemas.gateway.service_plugin import PluginSlotStatusDTO
from tests.harness.python.run_context import TestRunContext
from tests.integration.gateway.test_service_plugins import PYTHON_PLUGIN
from tests.support.plugin_package import install_plugin, native_plugin_target
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

    interrupted_creation = getattr(request, "param", "normal") in {"interrupted", "deleted", "cancelled"}
    creation_gate = context.artifacts_dir / "creation.blocked"
    if interrupted_creation:
        creation_gate.touch()
    gateway = start_gateway_process(workspace_root=gateway_workspace, default_backend_url=None,
        port=unused_tcp_port,
        asgi_app="tests.integration.gateway.plugin_creation_app:app" if interrupted_creation else "app.gateway.main:app",
        extra_env={"BOXTEAM_PLUGIN_CREATION_TEST_MODE": getattr(request, "param", "normal"),
            "BOXTEAM_PLUGIN_CREATION_TEST_GATE": str(creation_gate),
            "BOXTEAM_PLUGIN_CREATION_TEST_AUDIT": str(context.artifacts_dir / "creation-attempts.jsonl")}
            if interrupted_creation else None,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS,
            timeout=30, trust_env=False) as client:
            yield client, context, workspaces
    finally:
        close_gateway_process(gateway)


def _package(path: Path, *, package_id: str, version: str = "1.0.0", mode: str = "") -> Path:
    manifest = {
        "manifest_version": 1,
        "package_id": package_id,
        "version": version,
        "name": "Workspace 生命周期探针",
        "target": native_plugin_target("python"),
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
            "'parameters': {key: ('<private-present>' if key == 'credentialValue' else value) for key, value in json.loads(os.environ['BOXTEAM_PLUGIN_PARAMETERS']).items()}, 'credential_digest': __import__('hashlib').sha256(json.loads(os.environ['BOXTEAM_PLUGIN_PARAMETERS']).get('credentialValue', '').encode()).hexdigest(), 'pid': os.getpid(), 'instance_id': identity['instance_id'],"))
        archive.writestr("nested/local_module.py", "VALUE = 1\n")
    return path


def test_workspace_plugins_follow_owner_configuration_and_independent_lifecycle(
    workspace_plugin_case, request: pytest.FixtureRequest,
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

    def binding_for(package_id: str, *, enabled: bool = True, version: str = "1.0.0") -> dict[str, object]:
        return {"package_id": package_id, "package_version": version, "plugin_id": "probe", "enabled": enabled}

    def write_binding(workspace_id: str, entries: list[dict[str, object]], key: str,
                      parameters: dict[str, object] | None = None, expected_status: int = 200,
                      preflight: bool = False, expected_detail: str | None = None) -> None:
        path = f"/api/v1/workspaces/{workspace_id}/config/sources/workspace_root_mutable_override"
        headers = {"X-BoxTeam-Workspace-Id": workspace_id}
        document = exchange("GET", path, headers=headers).raise_for_status().json()["data"]
        source = commentjson.loads(document["jsonc"])
        source.setdefault("plugins", {})["runtime"] = {"entries": entries}
        if parameters is not None:
            source["plugins"]["configuration"] = {"values": {"probe.tools": {"probe": parameters}}}
        response = exchange("POST" if preflight else "PUT", path + ("/preflight" if preflight else ""), {
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
        if expected_detail is not None:
            assert expected_detail in response.json()["detail"], response.text
        if expected_status != 200:
            after = exchange("GET", path, headers=headers).raise_for_status().json()["data"]
            for field in ("jsonc", "file_digest", "layer_revision", "layer_digest", "active_revision", "active_digest", "effective"):
                assert after[field] == document[field], field
            return
        assert response.json()["data"]["edit"]["state"] == "active"

    def wait_binding(workspace_id: str, state: str, *, version: str | None = None) -> dict[str, object]:
        def matches(value: dict[str, object]) -> bool:
            item = status_for(value, workspace_id)
            if item is None:
                return False
            status = PluginSlotStatusDTO.model_validate(item)
            return status.state == state and (version is None or status.binding.package_version == version)

        data = _wait_for(matches)
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
        return call_response(item).raise_for_status().json()

    def call_response(item: dict[str, object]) -> httpx.Response:
        status = PluginSlotStatusDTO.model_validate(item)
        binding, instance = status.binding, status.instance
        assert instance is not None and binding.workspace_id is not None
        path = (f"/api/gateway/service-plugins/{binding.package_id}/probe/instances/"
            f"{instance.instance_id}/services/echo/value")
        return exchange("GET", path + f"?package_version={binding.package_version}&workspace_id={binding.workspace_id}")

    def uninstall(version: str) -> httpx.Response:
        return exchange("POST", "/api/gateway/service-plugins/uninstall", {
            "gateway_id": target_gateway, "package_id": "probe.tools", "package_version": version,
        })

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
            install_plugin(
                lambda path, body: exchange("POST", path, body),
                gateway_id=target_gateway,
                path=str(archive),
                expected_digest=hashlib.sha256(archive.read_bytes()).hexdigest(),
            )

        # 参数容器尚为空：预检与直接保存都必须在原文件/CAS写入前拒绝不存在的确切版本。
        for preflight in (True, False):
            write_binding(workspace_a_id, [binding_for("probe.tools", version="9.0.0", enabled=False)],
                f"workspace-empty-parameters-rejected-{preflight}", expected_status=409,
                preflight=preflight, expected_detail="插件包版本不存在或不是普通目录: probe.tools@9.0.0")
        assert status_for(exchange("POST", "/api/gateway/service-plugins/status", {"gateway_id": target_gateway})
            .raise_for_status().json()["data"], workspace_a_id) is None
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

        private_value = "workspace-private-input-sentinel"
        private_response = exchange("POST", f"/api/v1/workspaces/{workspace_a_id}/config/plugin-private-inputs", {
            "gateway_id": target_gateway, "workspace_id": workspace_a_id, "package_id": "probe.tools",
            "plugin_id": "probe", "package_version": "1.0.0", "source_key": "workspace_root_mutable_override",
            "parameter_pointer": "/credentialValue", "value": private_value,
        }).raise_for_status()
        assert private_response.headers["Cache-Control"] == "no-store"
        private_reference = private_response.json()["data"]
        assert private_value not in private_response.text
        write_binding(workspace_b_id, [binding_for("probe.tools")], "workspace-b-foreign-private-reference",
            {"credentialValue": private_reference}, expected_status=409)
        missing_reference = {**private_reference, "version": "0" * 32}
        write_binding(workspace_a_id, [binding_for("probe.tools")], "workspace-a-missing-private-reference",
            {"credentialValue": missing_reference}, expected_status=409)
        write_binding(workspace_a_id, [binding_for("probe.tools")], "workspace-a-plugin-parameters",
            {"label": "A changed", "credentialValue": private_reference})
        source_path = f"/api/v1/workspaces/{workspace_a_id}/config/sources/workspace_root_mutable_override"
        document = exchange("GET", source_path).raise_for_status()
        assert private_value not in document.text
        assert private_reference["version"] in document.text
        assert private_value not in (workspaces[0] / ".boxteam/workspace.jsonc").read_text(encoding="utf-8")
        private_file = workspaces[0] / ".boxteam/credentials/plugins" / (private_reference["version"] + ".json")
        assert json.loads(private_file.read_text(encoding="utf-8"))["value"] == private_value
        changed_a = _wait_for(lambda value: (item := status_for(value, workspace_a_id)) is not None
            and item["state"] == "ready" and item["instance"]["instance_id"] != ready_a["instance"]["instance_id"])
        assert_process_exited(ready_a["instance"]["process_id"])
        ready_a = status_for(changed_a, workspace_a_id)
        assert ready_a is not None and call(ready_a)["parameters"] == {"label": "A changed", "credentialValue": "<private-present>"}
        assert call(ready_a)["credential_digest"] == hashlib.sha256(private_value.encode()).hexdigest()
        assert wait_binding(workspace_b_id, "ready")["instance"]["instance_id"] == ready_b["instance"]["instance_id"]

        unavailable_private_file = private_file.with_suffix(".unavailable")
        private_file.rename(unavailable_private_file)
        try:
            rejected = exchange("POST", "/api/gateway/service-plugins/restart", {
                "gateway_id": target_gateway, "workspace_id": workspace_a_id,
                "package_id": "probe.tools", "package_version": "1.0.0", "plugin_id": "probe",
                "revision": ready_a["revision"],
            })
            assert rejected.status_code == 409 and "私有输入版本不存在" in rejected.text
            assert wait_binding(workspace_a_id, "ready") == ready_a
            assert call(ready_a)["credential_digest"] == hashlib.sha256(private_value.encode()).hexdigest()
            assert wait_binding(workspace_b_id, "ready") == ready_b
        finally:
            unavailable_private_file.rename(private_file)

        restarted = exchange("POST", f"/api/gateway/workspaces/{workspace_a_id}/runtime/restart-safe",
            timeout=150).raise_for_status()
        assert restarted.status_code == 200
        same_a = wait_binding(workspace_a_id, "ready")
        assert same_a["instance"]["instance_id"] == ready_a["instance"]["instance_id"]
        assert call(same_a)["parameters"] == {"label": "A changed", "credentialValue": "<private-present>"}
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
        assert call(ready_a)["parameters"] == {"label": "A changed", "credentialValue": "<private-present>"}
        assert wait_binding(workspace_b_id, "ready")["instance"]["instance_id"] == ready_b["instance"]["instance_id"]

        # 整包更新不改变任一绑定；显式切 A 后，B 继续使用同一旧实例及旧版本。
        updated_archive = _package(context.artifacts_dir / "probe.tools-v2.zip", package_id="probe.tools", version="2.0.0")
        install_plugin(lambda path, body: exchange("POST", path, body),
            gateway_id=target_gateway, path=str(updated_archive),
            expected_digest=hashlib.sha256(updated_archive.read_bytes()).hexdigest())
        assert wait_binding(workspace_a_id, "ready")["instance"] == ready_a["instance"]
        assert wait_binding(workspace_b_id, "ready")["instance"] == ready_b["instance"]
        assert call(ready_a)["version"] == call(ready_b)["version"] == "1.0.0"
        write_binding(workspace_a_id, [binding_for("probe.tools", version="2.0.0")], "workspace-a-plugin-switch")
        switched_a = wait_binding(workspace_a_id, "ready", version="2.0.0")
        assert PluginSlotStatusDTO.model_validate(switched_a).binding.package_version == "2.0.0"
        assert call(switched_a)["version"] == "2.0.0"
        assert call(switched_a)["credential_digest"] == hashlib.sha256(private_value.encode()).hexdigest()
        assert_process_exited(ready_a["instance"]["process_id"])
        assert call_response(ready_a).status_code == 409
        assert wait_binding(workspace_b_id, "ready")["instance"] == ready_b["instance"]
        assert call(ready_b)["version"] == "1.0.0"
        ready_a = switched_a

        write_binding(workspace_a_id, [binding_for("probe.tools", enabled=False, version="2.0.0")], "workspace-a-plugin-disable")
        assert wait_binding(workspace_a_id, "stopped")["instance"] is None
        assert_process_exited(ready_a["instance"]["process_id"])
        blocked = uninstall("2.0.0")
        assert blocked.status_code == 409 and f"workspace-configuration:{workspace_a_id}:probe" in blocked.text, blocked.text
        still_b = wait_binding(workspace_b_id, "ready")
        assert still_b["instance"]["instance_id"] == ready_b["instance"]["instance_id"]
        assert call(still_b)["pid"] == ready_b["instance"]["process_id"]

        write_binding(workspace_a_id, [binding_for("probe.tools", version="2.0.0")], "workspace-a-plugin-reenable")
        enabled_a = wait_binding(workspace_a_id, "ready")
        blocked = exchange("POST", "/api/gateway/service-plugins/uninstall", {
            "gateway_id": target_gateway, "package_id": "probe.tools", "package_version": "1.0.0",
        })
        assert blocked.status_code == 409

        exchange("POST", f"/api/gateway/workspaces/{workspace_a_id}/runtime/stop", timeout=150).raise_for_status()
        assert wait_workspace_state(workspace_a_id, "stopped")["state"] == "stopped"
        assert_process_exited(enabled_a["instance"]["process_id"])
        blocked = uninstall("2.0.0")
        assert blocked.status_code == 409 and f"workspace-configuration:{workspace_a_id}:probe" in blocked.text, blocked.text
        assert wait_workspace_state(workspace_a_id, "stopped")["state"] == "stopped", "卸载检查不能启动已停止工作区"
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

        # 单个已提交插件失败不能阻断原工作区重启、配置编辑及兄弟工作区。
        exchange("POST", f"/api/gateway/workspaces/{workspace_a_id}/runtime/restart-safe",
            timeout=150).raise_for_status()
        assert workspace_identity(workspace_a_id) == workspace_a_id
        assert wait_binding(workspace_a_id, "failed")["binding"] == failed_a["binding"]
        assert wait_binding(workspace_b_id, "ready")["instance"] == final_b["instance"]
        assert call(final_b)["pid"] == ready_b["instance"]["process_id"]

        packages = context.boxteam_home_for_node(request.node.nodeid) / "plugins/packages/probe.tools"
        uninstall("2.0.0").raise_for_status()
        assert not (packages / "2.0.0").exists() and (packages / "1.0.0").is_dir()
        assert call(final_b)["version"] == "1.0.0"
        write_binding(workspace_b_id, [binding_for("probe.tools", enabled=False)], "workspace-b-plugin-disable")
        assert wait_binding(workspace_b_id, "stopped")["instance"] is None
        final_b_status = PluginSlotStatusDTO.model_validate(ready_b)
        assert final_b_status.instance is not None
        assert_process_exited(final_b_status.instance.process_id)
        blocked = uninstall("1.0.0")
        assert blocked.status_code == 409 and f"workspace-configuration:{workspace_b_id}:probe" in blocked.text, blocked.text
        write_binding(workspace_b_id, [], "workspace-b-plugin-unbind")
        _wait_for(lambda value: status_for(value, workspace_b_id) is None)
        uninstall("1.0.0").raise_for_status()
        assert not (packages / "1.0.0").exists()
        inventory = exchange("POST", "/api/gateway/service-plugins/inventory", {"gateway_id": target_gateway}).raise_for_status().json()["data"]
        assert not any(item["manifest"]["package_id"] == "probe.tools" for item in inventory["packages"])
        assert private_file.is_file() and json.loads(private_file.read_text(encoding="utf-8"))["value"] == private_value
        assert private_reference["version"] in exchange("GET", source_path).raise_for_status().text
        assert all(workspace.is_dir() for workspace in workspaces), "卸载只删除包版本，用户配置、秘密及工作区保留"
    finally:
        (context.artifacts_dir / "workspace-plugin-flow.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize("workspace_plugin_case", ["normal", "interrupted", "deleted", "cancelled"], indirect=True)
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
        })
        if new and request.node.callspec.params["workspace_plugin_case"] == "cancelled":
            assert response.status_code == 503, response.text
            assert "controlled creation request cancelled after identity" in response.text
            response = exchange("GET", "/api/gateway/managed-workspaces")
        data = response.raise_for_status().json()["data"]
        return next(item["workspace_id"] for item in data["items"] if item["name"] == name)

    def document(workspace_id, intent_id=None):
        query = f"?idempotency_key={intent_id}" if intent_id else ""
        return exchange("GET", f"/api/v1/workspaces/{workspace_id}/config/sources/workspace_root_mutable_override{query}").raise_for_status().json()["data"]

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
        install_plugin(
            lambda path, body: exchange("POST", path, body),
            gateway_id=gateway_id,
            path=str(archive),
            expected_digest=hashlib.sha256(archive.read_bytes()).hexdigest(),
        )
        saved = defaults([binding]).raise_for_status().json()["data"]
        assert saved["edit"]["state"] == "active"
        assert defaults([binding, binding]).status_code == 422
        template_denied = exchange("POST", "/api/gateway/service-plugins/uninstall", {"gateway_id": gateway_id,
            "package_id": "probe.defaults", "package_version": "1.0.0"})
        assert template_denied.status_code == 409 and "workspace-default" in template_denied.text, template_denied.text
        existing_id = create(workspaces[0], "Existing", new=False)
        assert document(existing_id)["effective"]["plugins"]["runtime"]["entries"] == []
        creation_suffix = f"{context.run_root.name}-{context.runtime_root_for_node(request.node.nodeid).name}"
        new_root = context.workspace_root / f"new-project-{creation_suffix}"
        assert not new_root.exists()
        new_id = create(new_root, "New", new=True)
        gate = context.artifacts_dir / "creation.blocked"
        if request.node.callspec.params["workspace_plugin_case"] == "cancelled":
            attempts = [json.loads(line) for line in (context.artifacts_dir / "creation-attempts.jsonl").read_text(encoding="utf-8").splitlines()]
            frozen, = [item["creation"] for item in attempts if item["workspace_id"] == new_id]
            assert frozen["revision"] == saved["active_digest"]
            assert frozen["bindings"] == [{**binding, "workspace_id": None}]
            defaults([]).raise_for_status()
            blocked = exchange("POST", "/api/gateway/service-plugins/uninstall", {"gateway_id": gateway_id,
                "package_id": "probe.defaults", "package_version": "1.0.0"})
            assert blocked.status_code == 409 and new_id in blocked.text, blocked.text
            state = exchange("POST", "/api/gateway/service-plugins/status", {"gateway_id": gateway_id}).raise_for_status().json()["data"]
            assert all(item["binding"].get("workspace_id") != new_id for item in state["plugins"])
            listing = exchange("GET", "/api/gateway/managed-workspaces").raise_for_status().json()["data"]
            cancelled, = [item for item in listing["items"] if item["workspace_id"] == new_id]
            assert cancelled["status"] == "offline", cancelled
            removed = exchange("DELETE", f"/api/gateway/managed-workspaces/{new_id}").raise_for_status().json()["data"]
            assert all(item["workspace_id"] != new_id for item in removed["items"])
            exchange("POST", "/api/gateway/service-plugins/uninstall", {"gateway_id": gateway_id,
                "package_id": "probe.defaults", "package_version": "1.0.0"}).raise_for_status()
            assert new_root.is_dir(), "取消登记后删除托管不删除用户目录"
            assert document(existing_id)["effective"]["plugins"]["runtime"]["entries"] == []
            return
        if gate.exists():
            # 原创建入口已经登记冻结 intent；原 writer 的真实 HTTP 验证被受控拒绝。
            deadline = time.monotonic() + 30
            while True:
                state = exchange("POST", "/api/gateway/service-plugins/status", {"gateway_id": gateway_id}).raise_for_status().json()["data"]
                failed = next((item for item in state["workspace_configurations"] if item["workspace_id"] == new_id), None)
                if failed is not None and failed["state"] == "failed":
                    break
                assert time.monotonic() < deadline, state
                time.sleep(0.1)
            attempts_path = context.artifacts_dir / "creation-attempts.jsonl"
            attempts = [json.loads(line) for line in attempts_path.read_text(encoding="utf-8").splitlines()]
            frozen = next(item["creation"] for item in attempts if item["workspace_id"] == new_id)
            assert frozen["bindings"] == [{**binding, "workspace_id": None}]
            assert frozen["revision"] == saved["active_digest"]
            rejected = document(new_id, frozen["intent_id"])
            assert rejected["edit"]["state"] == "rejected"
            assert rejected["effective"]["plugins"]["runtime"]["entries"] == []
            changed_template = defaults([]).raise_for_status().json()["data"]
            assert changed_template["active_digest"] != frozen["revision"]
            denied = exchange("POST", "/api/gateway/service-plugins/uninstall", {"gateway_id": gateway_id,
                "package_id": "probe.defaults", "package_version": "1.0.0"})
            assert denied.status_code == 409 and f"workspace-create:{new_id}:{frozen['intent_id']}" in denied.text, denied.text
            assert isinstance(request.node, pytest.Function)
            if request.node.callspec.params["workspace_plugin_case"] == "deleted":
                # 删除原创建目标必须结清 intent；不伪造配置交接，也不为卸载重启 Backend。
                removed = exchange("DELETE", f"/api/gateway/managed-workspaces/{new_id}").raise_for_status().json()["data"]
                assert all(item["workspace_id"] != new_id for item in removed["items"])
                state = exchange("POST", "/api/gateway/service-plugins/status", {"gateway_id": gateway_id}).raise_for_status().json()["data"]
                assert all(item["binding"].get("workspace_id") != new_id for item in state["plugins"])
                assert all(item["workspace_id"] != new_id for item in state["workspace_configurations"])
                exchange("POST", "/api/gateway/service-plugins/uninstall", {"gateway_id": gateway_id,
                    "package_id": "probe.defaults", "package_version": "1.0.0"}).raise_for_status()
                assert document(existing_id)["effective"]["plugins"]["runtime"]["entries"] == []
                assert new_root.is_dir(), "移除托管不删除用户工作区数据"
                return
            # 缺陷消除后沿原工作区停止／启动显式重试，不能新建 intent 或读取新模板。
            gate.unlink()
            exchange("POST", f"/api/gateway/workspaces/{new_id}/runtime/stop").raise_for_status()
            exchange("POST", f"/api/gateway/workspaces/{new_id}/runtime/start").raise_for_status()
            resumed = document(new_id, frozen["intent_id"])
            assert resumed["edit"]["state"] == "active"
            assert resumed["edit"]["candidate_id"] == rejected["edit"]["candidate_id"]
            attempts = [json.loads(line) for line in attempts_path.read_text(encoding="utf-8").splitlines()]
            assert all(item["creation"] == frozen for item in attempts if item["workspace_id"] == new_id)
            later_root = context.workspace_root / f"later-project-{creation_suffix}"
            later_id = create(later_root, "Later", new=True)
            assert document(later_id)["effective"]["plugins"]["runtime"]["entries"] == []
            attempts = [json.loads(line) for line in attempts_path.read_text(encoding="utf-8").splitlines()]
            later = next(item["creation"] for item in attempts if item["workspace_id"] == later_id)
            assert later["intent_id"] != frozen["intent_id"]
            assert later["revision"] == changed_template["active_digest"] and later["bindings"] == []
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
        assert denied.status_code == 409 and f"workspace-configuration:{new_id}:probe" in denied.text, denied.text
        assert f"workspace-create:{new_id}:" not in denied.text, denied.text
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
