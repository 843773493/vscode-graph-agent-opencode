"""通过真实 Workbench 界面管理 Gateway、Workspace 与新建工作区插件配置。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from zipfile import ZipFile

import commentjson
import httpx
import psutil
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.gateway.test_plugin_install_operations import (
    plugin_source as plugin_source,  # noqa: PLC0414 - 复用真实HTTP来源和同一套目录／制品响应。
)
from tests.integration.gateway.test_service_plugins import NODE_PLUGIN, PYTHON_PLUGIN
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.paths import chromium_temporary_environment
from tests.support.plugin_package import native_plugin_target
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@pytest.fixture
def plugin_gateway_port(request: pytest.FixtureRequest) -> Iterator[int]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))
    lease, probes = E2EProcessLease.acquire_available(
        context.runtime_root_for_node(request.node.nodeid) / "gateway-port.json",
        range(ports.port(20), ports.end + 1),
    )
    try:
        (context.artifacts_dir / "port-allocation.json").write_text(json.dumps({
            "gateway_port": lease.ports[0], "bind_probes": probes,
        }, indent=2) + "\n", encoding="utf-8")
        yield lease.ports[0]
    finally:
        lease.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["single", "batch"])
async def test_service_plugin_management_ui_installs_and_edits_each_owner(
    request: pytest.FixtureRequest,
    integration_workspace_config_path: str,
    web_workbench_assets: Path,
    scenario: str,
    plugin_gateway_port: int,
    plugin_source: dict,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "插件管理 UI 验收需要真实 Chromium"
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    # 参数化场景各持有自己的来源和私有引用，不能将前一 Gateway 的秘密引用带入新 Home。
    workspace_root = prepare_default_test_workspace(workspace_root=context.workspace_root / scenario,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        shared_skill_root=Path.cwd() / "resources/skills", fresh_identity=True)
    config_path = install_test_workspace_config(workspace_root=workspace_root,
        config_path=Path(integration_workspace_config_path), schema_path=Path.cwd() / "configs/workspace_schema.jsonc")
    configuration = commentjson.loads(config_path.read_text(encoding="utf-8"))
    for provider in configuration["llm"]["providers"]:
        if "api_key" in provider:
            provider["api_key"] = "plugin-ui-unused-key"
    configuration.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = f"http://127.0.0.1:{plugin_gateway_port}"
    config_path.write_text(json.dumps(configuration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    package_path = context.artifacts_dir / "ui-plugin-package.zip"
    manifest = {
        "manifest_version": 1, "package_id": "ui.preference-probe", "version": "1.0.0", "name": "UI 配置验证包",
        "extension_contract_version": 1,
        "plugins": [
            {"id": "gateway-preference", "name": "Gateway 偏好", "instance_scope": "gateway",
             "requires": [{"extension_point": "configuration", "version": 1}],
             "contributes": {"configuration": {"type": "object", "additionalProperties": False,
                 "properties": {"label": {"type": "string"},
                     "credential": {"type": "string", "minLength": 8, "writeOnly": True}}}}},
            {"id": "workspace-preference", "name": "工作区偏好", "instance_scope": "workspace",
             "requires": [{"extension_point": "configuration", "version": 1}],
             "contributes": {"configuration": {"type": "object", "additionalProperties": False,
                 "properties": {"label": {"type": "string"},
                     "credential": {"type": "string", "minLength": 8, "writeOnly": True}}}}},
        ],
    }
    target_environment = native_plugin_target("python", "node") if scenario == "single" else {}
    if scenario == "single":
        manifest["target"] = target_environment
        for plugin, runtime in zip(manifest["plugins"], ("python", "node"), strict=True):
            plugin.update({"runtime": runtime, "entrypoint": ["entry.py" if runtime == "python" else "entry.mjs"],
                "management_protocol_version": 1})
            plugin["requires"].append({"extension_point": "services", "version": 1})
            plugin["contributes"]["services"] = [{"service_id": "echo", "service_type": "terminal", "protocol": "http"}]
    if scenario == "batch":
        manifest["plugins"].append({
            "id": "workspace-required", "name": "缺少必填参数", "instance_scope": "workspace",
            "requires": [{"extension_point": "configuration", "version": 1}],
            "contributes": {"configuration": {"type": "object", "required": ["label"],
                "properties": {"label": {"type": "string"}}}},
        })
    with ZipFile(package_path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest, ensure_ascii=False))
        if scenario == "single":
            archive.writestr("entry.py", PYTHON_PLUGIN)
            archive.writestr("local_module.py", "VALUE = 1\n")
            archive.writestr("entry.mjs", NODE_PLUGIN)
    version_paths = []
    incompatible_packages = []
    if scenario == "single":
        for version in ("1.1.0", "2.0.0"):
            candidate = copy.deepcopy(manifest)
            candidate["version"] = version
            if version == "2.0.0":
                for plugin in candidate["plugins"]:
                    plugin["contributes"]["configuration"]["properties"]["label"] = {"type": "number"}
            path = context.artifacts_dir / f"ui-plugin-package-{version}.zip"
            with ZipFile(path, "w") as archive:
                archive.writestr("plugin.jsonc", json.dumps(candidate, ensure_ascii=False))
                archive.writestr("entry.py", PYTHON_PLUGIN)
                archive.writestr("local_module.py", "VALUE = 1\n")
                archive.writestr("entry.mjs", NODE_PLUGIN)
            version_paths.append(str(path))
        for index, runtime in enumerate(("node", "python")):
            current_runtime = target_environment[runtime]
            assert isinstance(current_runtime, dict)
            candidate = copy.deepcopy(manifest)
            candidate["version"] = f"8.0.{index}"
            candidate["target"][runtime]["abi"] = "0"
            path = context.artifacts_dir / f"incompatible-{runtime}.zip"
            with ZipFile(path, "w") as archive:
                archive.writestr("plugin.jsonc", json.dumps(candidate, ensure_ascii=False))
                archive.writestr("entry.py", PYTHON_PLUGIN)
                archive.writestr("local_module.py", "VALUE = 1\n")
                archive.writestr("entry.mjs", NODE_PLUGIN)
            incompatible_packages.append({"path": str(path), "version": candidate["version"],
                "runtime": "Node" if runtime == "node" else "Python",
                "required": f"{candidate['target'][runtime]['version']}/0",
                "actual": f"{current_runtime['version']}/{current_runtime['abi']}"})
        # 同一不可变新版包同时供来源安装与后续显式切版消费，不另造模型或安装实现。
        content = Path(version_paths[0]).read_bytes()
        plugin_source["content"] = content
        plugin_source["artifact"] = {
            "package_id": manifest["package_id"], "package_version": "1.1.0", "target": manifest["target"],
            "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            "url": plugin_source["base_url"] + "/plugin.zip", "filename": "ui-plugin-package-1.1.0.zip",
        }
        plugin_source["release_download"].set()
    home = context.boxteam_home_for_node(request.node.nodeid)
    new_workspace_path = context.workspace_root / f"created-{context.run_root.name}-{scenario}"
    later_workspace_path = context.workspace_root / f"later-{context.run_root.name}-{scenario}"
    if scenario == "single":
        # 新目录及插件绑定必须由产品创建；只准备该隔离 Home 的正常用户配置。
        assert not new_workspace_path.exists() and not later_workspace_path.exists()
        user_config = home / "config/workspace.jsonc"
        user_config.parent.mkdir(parents=True, exist_ok=True)
        configuration["$schema"] = "./workspace_schema.jsonc"
        user_config.write_text(json.dumps(configuration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    gateway = start_gateway_process(workspace_root=workspace_root, default_backend_url=None,
        port=plugin_gateway_port, boxteam_home=home,
        artifacts_dir=context.artifacts_dir / "gateway", extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)})
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=30) as client:
            health = client.get("/api/gateway/health").raise_for_status().json()["data"]
            workspace_list = client.get("/api/gateway/workspaces").raise_for_status().json()["data"]
            assert workspace_list["active_workspace"]
            assert workspace_list["active_workspace"]["gateway_id"] == health["gateway_id"]
            if scenario == "single":
                source_path = "/api/gateway/config/sources/gateway_mutable_override"
                document = client.get(source_path).raise_for_status().json()["data"]
                source = commentjson.loads(document["jsonc"])
                source.setdefault("plugins", {})["sources"] = {"entries": [{
                    "source_id": "proof", "protocol_version": 1, "base_url": plugin_source["base_url"], "enabled": True,
                }]}
                saved = client.put(source_path, json={
                    "gateway_id": health["gateway_id"], "source_key": "gateway_mutable_override", "jsonc": json.dumps(source),
                    "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"],
                    "base_layer_digest": document["layer_digest"], "expected_active_revision": document["active_revision"],
                    "expected_active_digest": document["active_digest"], "idempotency_key": "plugin-ui-source",
                }).raise_for_status().json()["data"]
                assert saved["edit"]["state"] == "active", saved
        result_path = context.artifacts_dir / "plugin-management-ui.json"
        driver = "tests/integration/clients/web/" + ("plugin_batch_management_ui.mjs" if scenario == "batch" else "service_plugin_management_ui.mjs")
        environment = os.environ | {
            "BOXTEAM_PLUGIN_UI_FIXTURE": json.dumps({"baseUrl": f"http://127.0.0.1:{gateway.port}",
                "packagePath": str(package_path), "versionPaths": version_paths,
                "newWorkspacePath": str(new_workspace_path), "laterWorkspacePath": str(later_workspace_path),
                "incompatiblePackages": incompatible_packages,
                "sourceArtifact": plugin_source["artifact"] if scenario == "single" else None,
                "artifacts": str(context.artifacts_dir), "resultPath": str(result_path)}),
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
        }
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            run = await asyncio.to_thread(subprocess.run, ["bun", driver],
                cwd=Path.cwd(), env=environment | temporary, capture_output=True, text=True, timeout=300, check=False)
        (context.artifacts_dir / "browser.stdout.raw").write_text(run.stdout, encoding="utf-8")
        (context.artifacts_dir / "browser.stderr.raw").write_text(run.stderr, encoding="utf-8")
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"exit_code": run.returncode,
            "command": ["bun", driver]}, indent=2) + "\n", encoding="utf-8")
        assert run.returncode == 0, f"真实插件管理 UI 失败：\n{run.stdout}\n{run.stderr}\n{result_path}"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["phase"] == "complete" and not result["pageErrors"]
        if scenario == "batch":
            assert result["targetFrozen"] and result["partialFailure"] and result["lostResponseQueried"]
            assert result["defaultsOnly"] and result["cancelledWithoutWrite"]
        else:
            assert {"gateway", "workspace", "defaults"} <= set(result["configOwners"])
            assert result["installed"] and result["uninstalled"]
            assert result["actualRuntimeLoop"] and result["reloadKeptInstances"]
            assert result["gatewayRestartedIndependently"]
            assert len(result["incompatibleInstalls"]) == len(incompatible_packages) == 2
            assert all(item["status"] == "failed" and item["bindingKept"]
                for item in result["incompatibleInstalls"])
            assert result["sourceUpdate"]["artifact"]["digest"] == plugin_source["artifact"]["digest"]
            assert result["sourceUpdate"]["status"] == "succeeded"
            assert plugin_source["calls"] == [
                {"path": "/catalog", "query": {"query": ["ui.preference-probe"]}},
                {"path": "/plugin.zip", "query": {}},
            ]
            assert result["runtimeVersions"] == ["1.1.0", "1.0.0"]
            assert result["closedBindingBlockedUninstall"]
            assert result["defaultsCreatedIndependentInstance"] and result["clearedDefaultsOnlyAffectFuture"]
            assert result["createdWorkspaceBlockedUninstall"]
            assert result["newWorkspace"]["workspace_id"] != result["owner"]["workspaceId"]
            assert all(not psutil.pid_exists(pid) for pid in result["exitedPids"])
            packages_root = context.boxteam_home_for_node(request.node.nodeid) / "plugins/packages/ui.preference-probe"
            assert not packages_root.exists(), "完整卸载后包版本目录仍存在"
            assert result["explicitVersionSwitch"] and result["installKeptBindings"]
            assert result["disabledVersionStayedDisabled"] and result["invalidVersionKeptBinding"]
            assert result["referencedUninstall"]["status"] == 409
            assert result["workspaceHandoff"]["workspace_id"] == result["owner"]["workspaceId"]
            assert {scope: len(reads) for scope, reads in result["scopeReads"].items()} == {
                "gateway": 2, "workspace": 4, "defaults": 3,
            }
    finally:
        close_gateway_process(gateway)
