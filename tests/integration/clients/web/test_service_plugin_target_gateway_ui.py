"""通过正式 Workbench 标题栏验证 A→B 插件管理目标及换版归属。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZipFile

import commentjson
import httpx
import psutil
import pytest
from boxteam_plugin_sdk.logging_contract import load_runtime_log_contract

from app.gateway.federation.ports import principal_ref_for_gateway
from tests.harness.python.run_context import TestRunContext
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
def target_management_workbench_environment(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    prepared = os.getenv("BOXTEAM_TARGET_GATEWAY_UI_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


def test_workbench_settings_selects_remote_gateway_and_returns_to_local(
    target_gateway_network: TargetGatewayNetwork,
) -> None:
    network = target_gateway_network
    context = network.context
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "设置目标验收需要真实 Chromium"
    client_a = network.clients[0]
    gateway_a, gateway_b = network.gateway_ids
    topology = json.loads((context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
    remote = client_a.post("/api/gateway/remote-gateways", json={
        "name": "设置目标 Gateway B", "host": "127.0.0.1", "port": 22, "username": "target-ui",
        "private_key_path": topology["private_key"], "remote_gateway_port": topology["ports"][3],
    })
    assert remote.status_code == 200, remote.text
    remote_target, = [item for item in remote.json()["data"]["items"] if item["gateway_id"] == gateway_b]
    connection, = remote_target["remote_gateways"]
    settings_build = Path(os.environ["BOXTEAM_WEB_ASSETS"])
    assert settings_build.is_dir(), "必须显式复用已验收的完整 Workbench 构建"
    result_path = context.artifacts_dir / "settings-target-gateway-result.json"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if os.name == "nt" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180",
        "--", "bun", "tests/integration/clients/web/settings_target_gateway_ui.mjs"]
    environment = os.environ | {
        "BOXTEAM_SETTINGS_TARGET_UI_FIXTURE": json.dumps({"baseUrl": str(client_a.base_url), "gatewayA": gateway_a,
            "gatewayB": gateway_b, "remoteName": "设置目标 Gateway B", "workspaceId": network.workspace_id}),
        "BOXTEAM_BROWSER_ARTIFACTS": str(context.artifacts_dir),
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
    }
    (context.artifacts_dir / "settings-target-workbench-assets.json").write_text(json.dumps({
        "path": str(settings_build.resolve()), "main_sha256": hashlib.sha256((settings_build / "out/main.js").read_bytes()).hexdigest(),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with chromium_temporary_environment(context.runtime_root_for_node("settings-browser") / "chromium-tmp") as temporary:
        run = subprocess.run(command, cwd=context.project_root, env=environment | temporary,
            capture_output=True, text=True, timeout=195, check=False)
    (context.artifacts_dir / "settings-target-browser.stdout.raw").write_text(run.stdout, encoding="utf-8")
    (context.artifacts_dir / "settings-target-browser.stderr.raw").write_text(run.stderr, encoding="utf-8")
    (context.artifacts_dir / "settings-target-browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": run.returncode, "gateway_ids": [gateway_a, gateway_b],
        "connection_id": connection["gateway_connection_id"],
        "assets_sha256": hashlib.sha256((settings_build / "out/main.js").read_bytes()).hexdigest(),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert run.returncode == 0, f"Settings 双 Gateway 目标验收失败：\n{run.stdout}\n{run.stderr}\n{context.artifacts_dir}"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["phase"] == "complete" and result["pageErrors"] == [] and result["externalRequests"] == []
    assert result["settingsTargetRoundTrip"] == {
        "startupDefault": gateway_a, "selectedRemote": gateway_b, "switchedBack": gateway_a,
        "rememberedAgentEntry": gateway_a, "activeWorkspaceUnchanged": True, "sourceReads": result["sourceReads"],
    }
    assert any(read["gateway_id"] == gateway_b and read["workspace_id"] == network.workspace_id for read in result["sourceReads"])


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
    response = client.put(source_path, json={
        "gateway_id": gateway_id,
        "source_key": "gateway_mutable_override",
        "jsonc": json.dumps(configuration, ensure_ascii=False, indent=2) + "\n",
        "base_file_digest": document["file_digest"],
        "base_layer_revision": document["layer_revision"],
        "base_layer_digest": document["layer_digest"],
        "expected_active_revision": document["active_revision"],
        "expected_active_digest": document["active_digest"],
        "idempotency_key": idempotency_key,
    })
    assert response.status_code == 200, response.text
    saved = response.json()["data"]
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
    # 日志专项由真实插件输出固定字节；关闭访问噪声，保证用户显式续查期间原文件不变。
    entry = entry.replace(
        "    if resource == 'failure':",
        "    if resource.startswith('LOG_COORD_'):\n"
        "        os.write(sys.stdout.fileno(), ('🧭前缀 ' + resource + ' / 🧪 ' + resource + '\\r\\n').encode())\n"
        "    elif resource.startswith('LOG_PAGE_'):\n"
        "        os.write(sys.stdout.fileno(), ('padding\\n' * 600 + resource + ' first\\n' + resource + ' second\\n' + 'padding\\n' * 600).encode())\n"
        "    elif resource.startswith('LOG_NAV_'):\n"
        "        os.write(sys.stdout.fileno(), (resource + '\\n').encode())\n"
        "    if resource == 'failure':",
    ).replace("log_config=None)", "log_config=None, access_log=False)")
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


def test_workbench_logs_open_read_locate_and_keep_original_gateway(
    target_gateway_network: TargetGatewayNetwork,
) -> None:
    """日志正文、query/locate 与插件进程真实；仅 SSH 隧道沿原本机装具替换。"""
    network = target_gateway_network
    context = network.context
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "日志导航验收需要真实 Chromium"
    client_a = network.clients[0]
    gateway_a, gateway_b = network.gateway_ids
    topology = json.loads((context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
    remote = client_a.post("/api/gateway/remote-gateways", json={
        "name": "日志目标 Gateway B", "host": "127.0.0.1", "port": 22, "username": "log-ui",
        "private_key_path": topology["private_key"], "remote_gateway_port": topology["ports"][3],
    }).raise_for_status().json()["data"]
    target, = [item for item in remote["items"] if item["gateway_id"] == gateway_b]
    connection, = target["remote_gateways"]
    package = context.artifacts_dir / "log-navigation-plugin.zip"
    _write_package(package, "1.0.0")
    for client, gateway_id in zip(network.clients, network.gateway_ids, strict=True):
        _install(client, gateway_id, package)

        def configure(document: dict[str, object], owner_id: str = gateway_id) -> None:
            settings = document.setdefault("settings", {}).setdefault("gateway", {})
            settings.setdefault("plugins", {})["runtime"] = {"entries": [{
                "package_id": _PACKAGE_ID, "plugin_id": _PLUGIN_ID, "package_version": "1.0.0", "enabled": True,
            }]}
            # 双 Gateway 装具只安装本专项包；不继承另一个 Home 的新工作区默认插件。
            settings["plugins"]["workspace_defaults"] = {"entries": []}
            if owner_id == gateway_b:
                settings["permissions"] = {"federation": {"default_effect": "deny", "rules": [
                    {"capability": capability, "effect": "allow", "principal_ref": principal_ref_for_gateway(gateway_a)}
                    for capability in ("configuration_read", "service_plugin_installation_manage", "service_plugin_runtime_manage", "read", "send")
                ]}}

        _save_gateway_config(client, gateway_id, configure, f"log-ui-seed-{gateway_id}")
    slots = [_wait_ready(client, gateway_id, "1.0.0")
             for client, gateway_id in zip(network.clients, network.gateway_ids, strict=True)]
    result_path = context.artifacts_dir / "log-navigation-ui.json"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if os.name == "nt" else "posix_job.py")
    node = shutil.which("node")
    assert node, "Playwright 日志驱动需要 Node"
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180",
        "--", node, "tests/integration/clients/web/service_plugin_log_navigation.mjs"]
    log_contract = load_runtime_log_contract()
    remote_log_path = log_contract.path_for(
        log_contract.root_for(network.gateway_homes[1]), "service-plugin", slots[1]["instance"]["instance_id"],
    )
    assert remote_log_path.is_file()
    assets = Path(os.environ["BOXTEAM_WEB_ASSETS"])
    environment = os.environ | {
        "BOXTEAM_PLUGIN_LOG_UI_FIXTURE": json.dumps({
            "baseUrl": str(client_a.base_url), "gatewayA": gateway_a, "gatewayB": gateway_b,
            "workspaceId": network.workspace_id, "remoteName": "日志目标 Gateway B",
            "connectionId": connection["gateway_connection_id"], "packageId": _PACKAGE_ID, "pluginId": _PLUGIN_ID,
            "remoteLogPath": str(remote_log_path),
            "remoteRegistration": {"host": "127.0.0.1", "port": 22, "username": "log-ui-second",
                "private_key_path": topology["private_key"], "remote_gateway_port": topology["ports"][3]},
            "slots": slots, "resultPath": str(result_path), "artifacts": str(context.artifacts_dir),
        }),
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
    }
    with chromium_temporary_environment(context.runtime_root_for_node("log-browser") / "chromium-tmp") as temporary:
        run = subprocess.run(command, cwd=context.project_root, env=environment | temporary,
            capture_output=True, text=True, timeout=195, check=False)
    (context.artifacts_dir / "log-browser.stdout.raw").write_text(run.stdout, encoding="utf-8")
    (context.artifacts_dir / "log-browser.stderr.raw").write_text(run.stderr, encoding="utf-8")
    (context.artifacts_dir / "log-browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": run.returncode, "assets": str(assets),
        "assets_sha256": hashlib.sha256((assets / "out/main.js").read_bytes()).hexdigest(),
        "gateway_ids": network.gateway_ids, "instances": slots,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert run.returncode == 0, f"真实日志导航验收失败：\n{run.stdout}\n{run.stderr}\n{result_path}"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["phase"] == "complete" and result["pageErrors"] == [] and result["externalRequests"] == []
    assert result["activeWorkspaceUnchanged"] is True
    if not result["navigationOnly"]:
        assert result["detailLog"]["gateway_id"] == result["managementLog"]["gateway_id"] == gateway_b
        assert result["settingsLog"]["gateway_id"] == gateway_b
    assert result["location"]["copied_selection"] == result["location"]["query"]
    assert result["lateLocation"]["old_operation"]["ok"] is False
    assert result["lateLocation"]["visible_gateway_id"] == gateway_a
    assert result["coordinates"]["physical_crlf"] is True
    assert result["coordinates"]["copied_second_match"] == result["coordinates"]["query"]
    assert result["pagedCandidates"]["pages"] >= 2
    assert result["pagedCandidates"]["selected_second"] is True
    assert {item["case"] for item in result["staleReferences"]} == {"truncate", "replace", "delete"}
    assert result["bodyAndStatus"] == {"truncated": True, "empty": True, "refresh_kept_body": True}
    connections = result["connections"]
    assert connections["first"] == connection["gateway_connection_id"]
    assert connections["second"] != connections["first"]
    assert connections["deleted_connection_rejected"] is True
    assert result["replacement"]["original_log_id"] != result["replacement"]["replacement"]["log_id"]
    assert result["replacement"]["historic_reference"] == result["coordinates"]["reference"]
    assert result["localWindow"] == {"channel": "窗口", "gateway_reads": 0}
    startup = result["startupFailure"]
    assert startup["blocked_main"] == "/out/main.js"
    assert startup["copy_success"] is True and startup["copy_denied"] is True
    assert startup["temporary_nodes"] == 0 and startup["failure_api_requests"] == 0
    assert startup["focus_preserved"] is True
    assert startup["reload_recovered"] is True and startup["stale_memory_discarded"] is True
    assert startup["page_errors"] == []


@pytest.fixture
def plugin_source_server(request: pytest.FixtureRequest):
    """仅自有目录／下载服务受控，包安装与运行仍经过真实目标 Gateway。"""
    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self) -> None:
            if urlsplit(self.path).path != "/catalog":
                return super().do_GET()
            body = json.dumps({
                "protocol_version": 1, "source_id": "target-ui", "catalog_revision": "target-ui-v1",
                "items": [{"package_id": _PACKAGE_ID, "package_version": "3.0.0",
                    "target": native_plugin_target("python"), "digest": "sha256:" + hashlib.sha256(package.read_bytes()).hexdigest(),
                    "url": f"http://127.0.0.1:{server.server_port}/{package.name}", "filename": package.name}],
                "next_cursor": None,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    package = context.artifacts_dir / "target-ui-3.0.0.zip"
    server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *args, **kwargs: Handler(
        *args, directory=str(context.artifacts_dir), **kwargs))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_workbench_extension_title_target_keeps_remote_plugin_state_on_original_gateway(
    target_gateway_network: TargetGatewayNetwork,
    plugin_source_server: str,
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
    package_b_v3 = context.artifacts_dir / "target-ui-3.0.0.zip"
    _write_package(package_a, "2.0.0")
    _write_package(package_b_v1, "1.0.0")
    _write_package(package_b_v2, "2.0.0")
    _write_package(package_b_v3, "3.0.0")
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
        configuration["plugins"]["sources"] = {"entries": [{
            "source_id": "target-ui", "protocol_version": 1, "base_url": plugin_source_server, "enabled": True,
        }]}
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
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if os.name == "nt" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
        "--timeout-seconds", "240", "--", "bun", "tests/integration/clients/web/service_plugin_target_gateway_ui.mjs"]
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
            "connectionId": connection["gateway_connection_id"],
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
                timeout=255,
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
                "timeout_seconds": 255,
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
    assert result["independentTargets"] == {"extensions": gateway_a, "settings": gateway_b}
    assert result["lateInventory"] == {"gateway_id": gateway_b, "visible_gateway_id": gateway_a}
    assert result["installation"]["gateway_id"] == gateway_b
    assert result["installation"]["gateway_connection_id"] == connection["gateway_connection_id"]
    assert result["installation"]["artifact"]["package_version"] == "3.0.0"
    assert result["installation"]["status"] == "succeeded"
    assert result["uninstall"]["uninstalled"] is True
    assert result["uninstall"]["package_version"] == "3.0.0"
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
