"""真实 Gateway／双 Backend 共用原生 profile；VSIX 是明确的测试扩展。"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

import httpx
import psutil
import pytest

from app.core.resource_uri import resource_display_uri
from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)


@dataclass(frozen=True)
class NativeManagementCase:
    context: TestRunContext
    home: Path
    workspaces: tuple[Path, Path]
    client: httpx.Client
    gateway_pid: int


@pytest.fixture
def native_management_case(request: pytest.FixtureRequest, runtime_manifest_path: Path) -> Iterator[NativeManagementCase]:
    executable = os.getenv("BOXTEAM_TEST_NODE_HOST_EXECUTABLE")
    modules = os.getenv("BOXTEAM_TEST_NODE_HOST_MODULES")
    if not executable or not modules:
        pytest.skip("UNMET_PREREQUISITE: 需显式提供固定 Node 及原扩展管理依赖")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    component = context.runtime_root_for_node(request.node.nodeid) / "component"
    command = ["bun", "--eval", """
import {buildNodeExtensionHost} from './src/clients/workbench/scripts/nodeHost.mjs';
import {symlink} from 'node:fs/promises';
import {resolve,join} from 'node:path';
const [output,modules]=process.argv.slice(-2);
await buildNodeExtensionHost(resolve('src/clients/workbench'),output);
await symlink(resolve(modules),join(output,'node_modules'),'junction');
""", str(component), modules]
    build = subprocess.run(command, cwd=context.project_root, capture_output=True, text=True, timeout=60, check=False)
    (context.artifacts_dir / "build.json").write_text(json.dumps({"command": command, "returncode": build.returncode,
        "stdout": build.stdout, "stderr": build.stderr}, ensure_ascii=False, indent=2), encoding="utf-8")
    assert build.returncode == 0, build.stderr
    manifest = component.with_name("runtime-manifest.json")
    runtime = json.loads(runtime_manifest_path.read_text(encoding="utf-8"))
    runtime["extension_host"] = {"node_executable": str(Path(executable).resolve()), "component_root": str(component)}
    manifest.write_text(json.dumps(runtime), encoding="utf-8")
    workspaces = tuple(prepare_test_workspace(workspace_root=context.workspace_root / context.run_root.name / name,
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace", template_items=())
        for name in ("A1", "A2"))
    for workspace in workspaces:
        install_test_workspace_config(workspace_root=workspace, config_path=context.project_root / "configs/tests/workspace/default.jsonc",
            schema_path=context.project_root / "configs/workspace_schema.jsonc")
    home = context.boxteam_home_for_node(request.node.nodeid)
    gateway = start_gateway_process(workspace_root=workspaces[0], default_backend_url=None,
        port=integration_port_block_for_file(Path(request.node.path)).port(0), boxteam_home=home,
        artifacts_dir=context.artifacts_dir, extra_env={"BOXTEAM_RUNTIME_MANIFEST": str(manifest)})
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS,
                          timeout=30, trust_env=False) as client:
            client.post("/api/gateway/users/guest", json={"tracking": {"test": True}}).raise_for_status()
            yield NativeManagementCase(context, home, (workspaces[0], workspaces[1]), client, gateway.process.pid)
    finally:
        managers = [process for process in psutil.Process(gateway.process.pid).children(recursive=True)
            if any("extensionManagement.mjs" in argument for argument in process.cmdline())]
        close_gateway_process(gateway)
        exited, alive = psutil.wait_procs(managers, timeout=5)
        (context.artifacts_dir / "management-exit.json").write_text(json.dumps({
            "gateway_pid": gateway.process.pid, "exited": [process.pid for process in exited],
            "alive": [process.pid for process in alive]}, indent=2), encoding="utf-8")
        assert len(managers) == 1 and not alive


@pytest.fixture
def native_vsix() -> Callable[[Path, str, str, str | None], Path]:
    def create(directory: Path, name: str, version: str, content: str | None) -> Path:
        source = directory / f"{name}-{version}.vsix"
        with ZipFile(source, "w") as archive:
            archive.writestr("extension/package.json", json.dumps({"publisher": "boxteam-probe", "name": name,
                "version": version, "engines": {"vscode": "^1.0.0"}, "main": "./extension.js",
                "extensionKind": ["workspace"], "activationEvents": ["onCommand:boxteam.concurrent"],
                "contributes": {"commands": [{"command": "boxteam.concurrent", "title": "并发测试"}]}}))
            archive.writestr("extension/extension.js", f"exports.activate = () => '{content or f'{name}-{version}'}';\n")
        return source
    return create


def test_two_backend_processes_share_native_profile_without_losing_publications(
    native_management_case: NativeManagementCase, native_vsix: Callable[[Path, str, str, str | None], Path],
) -> None:
    case = native_management_case
    evidence: list[dict[str, object]] = []

    def exchange(method: str, path: str, key: str, body: dict[str, object] | None = None) -> dict:
        started = time.perf_counter()
        response = case.client.request(method, path, headers={"X-Request-ID": key}, json=body)
        receipt: dict[str, object] = {"method": method, "path": path, "request_id": key, "status": response.status_code,
            "elapsed_seconds": time.perf_counter() - started, "body": response.text}
        evidence.append(receipt)
        payload = response.json()
        receipt["body"] = payload
        assert response.headers["X-Request-ID"] == payload["request_id"] == key
        assert response.status_code == 200, response.text
        return payload["data"]

    try:
        first = exchange("GET", "/api/gateway/workspaces", "native-workspaces")["items"][0]
        listing = exchange("POST", "/api/gateway/managed-workspaces", "native-register-A2",
            {"root_path": str(case.workspaces[1]), "name": "共享原生安装域 A2"})
        second = next(item for item in listing["items"] if item["workspace_id"] != first["workspace_id"])
        targets: list[dict] = []
        healths: list[dict] = []
        for index, item in enumerate((first, second)):
            base = f"/api/v1/workspaces/{item['workspace_id']}"
            exchange("GET", base + "/workspace", f"native-workspace-{index}")
            response = case.client.get(base + "/health")
            response.raise_for_status()
            healths.append(response.json())
            target = exchange("POST", "/api/gateway/extension-targets/resolve", f"native-target-{index}",
                {"protocol_version": 1, "gateway_id": item["gateway_id"], "workspace_ids": [item["workspace_id"]]})
            assert target["capabilities"]["installation_manage"] is True
            targets.append({key: target[key] for key in ("protocol_version", "gateway_id", "workspace_ids", "backend_id")})
        assert healths[0]["process_id"] != healths[1]["process_id"]
        assert targets[0]["backend_id"] == targets[1]["backend_id"] == healths[0]["backend_id"] == healths[1]["backend_id"]

        def inventory(index: int, label: str) -> dict:
            return exchange("POST", "/api/gateway/extension-installations/inventory", label,
                {key: value for key, value in targets[index].items() if key != "workspace_ids"})["inventory"]

        def install(index: int, name: str, version: str, gate: threading.Barrier | None = None,
                    *, content: str | None = None) -> dict:
            source = native_vsix(case.workspaces[index], name, version, content)
            target = targets[index]
            if gate is not None:
                gate.wait(timeout=10)
            return exchange("POST", "/api/gateway/extension-installations/install", f"native-{index}-{name}-{version}",
                {**target, "source_uri": resource_display_uri(scope="workspace", scope_id=target["workspace_ids"][0],
                    kind="files", tail_segments=("root", source.name), gateway_authority=target["gateway_id"])})

        assert inventory(0, "native-empty-A1")["extensions"] == []
        assert inventory(1, "native-empty-A2")["extensions"] == []
        # 两个请求在不同 Backend OS 进程同时开始，必须共用原安装器及同一个 profile。
        for operations in (((0, "first", "1.0.0"), (1, "second", "1.0.0")),
                           ((0, "first", "2.0.0"), (1, "first", "3.0.0"))):
            gate = threading.Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(install, *operation, gate) for operation in operations]
                publications = [future.result(timeout=30) for future in futures]
            for publication, (_, name, version) in zip(publications, operations, strict=True):
                installed = publication["installed"]
                assert installed["id"] == f"boxteam-probe.{name}" and installed["version"] == version
                assert installed["reused"] is False
                own = next(extension for extension in publication["inventory"]["extensions"]
                    if extension["description"]["identifier"]["value"] == installed["id"])
                assert own["description"]["version"] == version and own["is_valid"]
            left, right = inventory(0, f"native-left-{operations[0][2]}"), inventory(1, f"native-right-{operations[0][2]}")
            assert left == right
            assert {extension["description"]["identifier"]["value"] for extension in left["extensions"]} == {
                "boxteam-probe.first", "boxteam-probe.second"}

        installation = case.home / "state/backend/extension-management" / targets[0]["backend_id"] / "extensions"
        for name, version in (("first", "1.0.0"), ("first", "2.0.0"), ("first", "3.0.0"), ("second", "1.0.0")):
            directories = [path for path in installation.iterdir() if path.is_dir() and path.name.startswith(f"boxteam-probe.{name}-{version}")]
            assert len(directories) == 1
            assert (directories[0] / "extension.js").read_text() == f"exports.activate = () => '{name}-{version}';\n"
        current = next(extension["description"]["version"] for extension in left["extensions"]
                       if extension["description"]["identifier"]["value"] == "boxteam-probe.first")
        # 从另一个 Backend 重装当前版本、切回旧版、再切回当前版，均只复用原 ExtensionKey。
        # 不以另一份同版本 VSIX 的正文替换已发布代码；每次回执必须报告实际复用。
        for version in (current, "1.0.0", current):
            scripts = [path / "extension.js" for path in installation.iterdir()
                       if path.is_dir() and path.name.startswith(f"boxteam-probe.first-{version}")]
            assert len(scripts) == 1
            script = scripts[0]
            before = script.read_bytes(), script.stat().st_mtime_ns
            reused = install(1, "first", version, content="must-not-replace-published-version")
            assert reused["installed"]["reused"] is True and reused["installed"]["version"] == version
            assert (script.read_bytes(), script.stat().st_mtime_ns) == before
            assert inventory(0, f"native-reuse-first-{version}") == reused["inventory"]
        left = reused["inventory"]
        # 常驻组件归 Gateway，Backend 请求、重启均不得关闭或重建它。
        def management_processes() -> list[psutil.Process]:
            return [process for process in psutil.Process(case.gateway_pid).children(recursive=True)
                if any("extensionManagement.mjs" in argument for argument in process.cmdline())]

        [manager] = management_processes()
        identity = manager.pid, manager.create_time()
        evidence.append({"management_pid": identity[0], "management_created_at": identity[1]})
        for health in healths:
            assert not [process for process in psutil.Process(health["process_id"]).children(recursive=True)
                if any("extensionManagement.mjs" in argument for argument in process.cmdline())]
        exchange("POST", f"/api/gateway/workspaces/{first['workspace_id']}/runtime/restart-safe", "native-restart-A1")
        recovered = case.client.get(f"/api/v1/workspaces/{first['workspace_id']}/health")
        recovered.raise_for_status()
        assert recovered.json()["process_id"] != healths[0]["process_id"]
        assert recovered.json()["backend_id"] == targets[0]["backend_id"]
        assert inventory(0, "native-recovered") == left == inventory(1, "native-peer-after-restart")
        [manager] = management_processes()
        assert (manager.pid, manager.create_time()) == identity
    finally:
        (case.context.artifacts_dir / "concurrency.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
