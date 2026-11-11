"""通过正式 Workbench 标题栏验证 A→B 插件管理目标及换版归属。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from zipfile import ZipFile

import commentjson
import httpx
import psutil
import pytest

from app.gateway.federation.ports import principal_ref_for_gateway
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.gateway.test_service_plugins import PYTHON_PLUGIN
from tests.integration.gateway.test_target_gateway_routing import (
    TargetGatewayNetwork,
)
from tests.integration.gateway.test_target_gateway_routing import (
    target_gateway_network as target_gateway_network,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment
from tests.support.plugin_package import install_plugin, native_plugin_target

pytest_plugins = ["tests.integration.gateway.test_target_gateway_routing"]

_PACKAGE_ID = "target-owner.ui-probe"
_PLUGIN_ID = "echo"


@pytest.fixture(autouse=True)
def target_management_workbench_environment(
    monkeypatch: pytest.MonkeyPatch, web_workbench_assets: Path,
) -> None:
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(web_workbench_assets))


def _save_gateway_config(
    client: httpx.Client,
    gateway_id: str,
    update: Callable[[dict[str, object]], None],
    idempotency_key: str,
) -> dict[str, object]:
    source_path = "/api/gateway/config/sources/gateway_mutable_override"
    document = client.get(source_path).raise_for_status().json()["data"]
    configuration = commentjson.loads(document["jsonc"])
    update(configuration)
    saved = client.put(source_path, json={
        "gateway_id": gateway_id,
        "source_key": "gateway_mutable_override",
        "jsonc": json.dumps(configuration, ensure_ascii=False, indent=2) + "\n",
        "base_file_digest": document["file_digest"],
        "base_layer_revision": document["layer_revision"],
        "base_layer_digest": document["layer_digest"],
        "expected_active_revision": document["active_revision"],
        "expected_active_digest": document["active_digest"],
        "idempotency_key": idempotency_key,
    }).raise_for_status().json()["data"]
    assert saved["edit"]["state"] == "active", saved
    return saved


def _write_package(path: Path, version: str) -> None:
    manifest = {
        "manifest_version": 1,
        "package_id": _PACKAGE_ID,
        "version": version,
        "name": "目标归属 UI 验收包",
        "target": native_plugin_target("python"),
        "extension_contract_version": 1,
        "plugins": [{
            "id": _PLUGIN_ID,
            "name": "目标归属 Echo",
            "instance_scope": "gateway",
            "runtime": "python",
            "entrypoint": ["entry.py"],
            "management_protocol_version": 1,
            "requires": [
                {"extension_point": "services", "version": 1},
                {"extension_point": "configuration", "version": 1},
            ],
            "contributes": {
                "services": [{"service_id": "echo", "service_type": "terminal", "protocol": "http"}],
                "configuration": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"marker": {"type": "string"}},
                },
            },
        }],
    }
    entry = PYTHON_PLUGIN.replace(
        "identity = json.loads(os.environ['BOXTEAM_PLUGIN_INSTANCE'])",
        "identity = json.loads(os.environ['BOXTEAM_PLUGIN_INSTANCE'])\n"
        "if identity['package_version'] == '2.0.0':\n"
        "    import time\n"
        "    time.sleep(2.0)",
    )
    with ZipFile(path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest, ensure_ascii=False))
        archive.writestr("entry.py", entry)
        archive.writestr("local_module.py", "VALUE = 1\n")


def _install(client: httpx.Client, gateway_id: str, package: Path) -> None:
    operation = install_plugin(
        lambda endpoint, payload: client.post(endpoint, json=payload),
        gateway_id=gateway_id,
        path=str(package),
    )
    assert operation.gateway_id == gateway_id and operation.status == "succeeded", operation


def _runtime_slot(client: httpx.Client, gateway_id: str) -> dict[str, object]:
    status = client.post("/api/gateway/service-plugins/status", json={"gateway_id": gateway_id})
    assert status.status_code == 200, status.text
    slots = status.json()["data"]["plugins"]
    slot, = [item for item in slots if item["binding"]["package_id"] == _PACKAGE_ID
        and item["binding"]["plugin_id"] == _PLUGIN_ID]
    return slot


def _wait_ready(client: httpx.Client, gateway_id: str, version: str) -> dict[str, object]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        slot = _runtime_slot(client, gateway_id)
        if slot["state"] == "ready" and slot["instance"]:
            assert slot["binding"]["package_version"] == version, slot
            return slot
        time.sleep(0.1)
    raise AssertionError(f"Gateway {gateway_id} 插件未进入 ready：{slot}")


def test_workbench_extension_title_target_keeps_remote_plugin_state_on_original_gateway(
    target_gateway_network: TargetGatewayNetwork,
    web_workbench_assets: Path,
) -> None:
    network = target_gateway_network
    context = network.context
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "标题栏目标管理验收需要真实 Chromium"

    client_a, client_b = network.clients
    gateway_a, gateway_b = network.gateway_ids
    topology = json.loads((context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
    remote = client_a.post("/api/gateway/remote-gateways", json={
        "name": "标题栏目标 Gateway B",
        "host": "127.0.0.1",
        "port": 22,
        "username": "target-ui",
        "private_key_path": topology["private_key"],
        "remote_gateway_port": topology["ports"][3],
    })
    assert remote.status_code == 200, remote.text
    remote_target, = [item for item in remote.json()["data"]["items"] if item["gateway_id"] == gateway_b]
    connection, = remote_target["remote_gateways"]

    package_a = context.artifacts_dir / "target-ui-2.0.0.zip"
    package_b_v1 = context.artifacts_dir / "target-ui-1.0.0.zip"
    package_b_v2 = context.artifacts_dir / "target-ui-2.0.0.zip"
    _write_package(package_a, "2.0.0")
    _write_package(package_b_v1, "1.0.0")
    _write_package(package_b_v2, "2.0.0")
    _install(client_a, gateway_a, package_a)
    _install(client_b, gateway_b, package_b_v1)
    _install(client_b, gateway_b, package_b_v2)

    capabilities = (
        "configuration_read", "configuration_write", "service_plugin_installation_manage",
        "service_plugin_runtime_manage", "send", "read",
    )
    principal = principal_ref_for_gateway(gateway_a)

    def bind_gateway(configuration: dict[str, object], version: str, marker: str) -> None:
        plugins = configuration.setdefault("plugins", {})
        plugins["runtime"] = {"entries": [{
            "package_id": _PACKAGE_ID,
            "plugin_id": _PLUGIN_ID,
            "package_version": version,
            "enabled": True,
        }]}
        plugins["configuration"] = {"values": {_PACKAGE_ID: {_PLUGIN_ID: {"marker": marker}}}}

    def configure_b(configuration: dict[str, object]) -> None:
        bind_gateway(configuration, "1.0.0", "B-before")
        configuration["permissions"] = {"federation": {
            "default_effect": "deny",
            "rules": [{"capability": capability, "effect": "allow", "principal_ref": principal}
                for capability in capabilities],
        }}

    def configure_a(configuration: dict[str, object]) -> None:
        bind_gateway(configuration, "2.0.0", "A-unchanged")

    before_b_policy = client_b.get("/api/gateway/config/reload-status").raise_for_status().json()["data"]
    _save_gateway_config(client_b, gateway_b, configure_b, "target-ui-seed-b")
    _save_gateway_config(client_a, gateway_a, configure_a, "target-ui-seed-a")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        after_b_policy = client_b.get("/api/gateway/config/reload-status").raise_for_status().json()["data"]
        if after_b_policy["revision"] != before_b_policy["revision"] and after_b_policy["healthy"] is True:
            break
        time.sleep(0.1)
    else:
        raise AssertionError(f"Gateway B 未激活插件运行权限：{after_b_policy}")

    initial_a = _wait_ready(client_a, gateway_a, "2.0.0")
    initial_b = _wait_ready(client_b, gateway_b, "1.0.0")
    initial_a_instance = initial_a["instance"]
    initial_b_instance = initial_b["instance"]
    result_path = context.artifacts_dir / "target-gateway-plugin-ui.json"
    command = ["bun", "tests/integration/clients/web/service_plugin_target_gateway_ui.mjs"]
    environment = os.environ | {
        "BOXTEAM_TARGET_PLUGIN_UI_FIXTURE": json.dumps({
            "baseUrl": str(client_a.base_url),
            "gatewayA": gateway_a,
            "gatewayB": gateway_b,
            "remoteName": "标题栏目标 Gateway B",
            "packageId": _PACKAGE_ID,
            "pluginId": _PLUGIN_ID,
            "versionA": "2.0.0",
            "versionB1": "1.0.0",
            "versionB2": "2.0.0",
            "initialBInstanceId": initial_b_instance["instance_id"],
            "resultPath": str(result_path),
        }),
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
    }
    with chromium_temporary_environment(context.runtime_root_for_node("browser") / "chromium-tmp") as temporary:
        try:
            run = subprocess.run(
                command,
                cwd=context.project_root,
                env=environment | temporary,
                capture_output=True,
                text=True,
                timeout=240,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            captured_stdout = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else error.stdout or ""
            captured_stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else error.stderr or ""
            (context.artifacts_dir / "browser.stdout.raw").write_text(captured_stdout, encoding="utf-8")
            (context.artifacts_dir / "browser.stderr.raw").write_text(captured_stderr, encoding="utf-8")
            (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
                "command": command,
                "timed_out": True,
                "timeout_seconds": 240,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            raise
    (context.artifacts_dir / "browser.stdout.raw").write_text(run.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(run.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command,
        "exit_code": run.returncode,
        "initial_a_instance": initial_a_instance,
        "initial_b_instance": initial_b_instance,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert run.returncode == 0, f"真实 Workbench 目标归属验收失败：\n{run.stdout}\n{run.stderr}\n{result_path}"

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert result["externalRequests"] == []
    assert result["inventoryA"] == ["2.0.0"]
    assert result["inventoryB"] == ["1.0.0", "2.0.0"]
    assert result["detailOwners"] == [gateway_b, gateway_a]
    assert result["delayedWrite"] == {
        "gateway_id": gateway_b,
        "status": 200,
        "b_marker": "B-from-Workbench",
        "a_marker": "A-unchanged",
        "page_owner_after_release": gateway_a,
    }
    assert "ready" in result["runtimeStages"]
    assert result["businessCall"]["gateway_id"] == gateway_b
    assert result["businessCall"]["version"] == "2.0.0"
    assert result["businessCall"]["pid"] != initial_b_instance["process_id"]
    assert result["retiredCall"]["gateway_id"] == gateway_b
    assert result["retiredCall"]["status"] == 409
    assert result["retiredCall"]["request_id"]
    assert "版本已切换" in result["retiredCall"]["detail"]

    final_a = _runtime_slot(client_a, gateway_a)
    final_b = _wait_ready(client_b, gateway_b, "2.0.0")
    assert final_a["state"] == "ready" and final_a["instance"]["instance_id"] == initial_a_instance["instance_id"]
    assert final_a["binding"]["package_version"] == "2.0.0"
    assert final_b["instance"]["process_id"] == result["businessCall"]["pid"]
    assert not psutil.pid_exists(initial_b_instance["process_id"])
    (context.artifacts_dir / "target-gateway-runtime-summary.json").write_text(json.dumps({
        "gateway_ids": [gateway_a, gateway_b],
        "remote_connection_id": connection["gateway_connection_id"],
        "initial_instances": [initial_a_instance, initial_b_instance],
        "final_instances": [final_a["instance"], final_b["instance"]],
        "result": result,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
