"""真实常驻 Node 控制入口；不以 Gateway／Backend 装具代替原生安装与文件服务。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from base64 import b64decode, b64encode
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest

from app.core.child_process import OwnedChildProcess
from app.gateway.runtime.extension_management import GatewayExtensionManagement
from app.services.infrastructure.extension_host.configuration import native_path
from app.services.infrastructure.extension_host.management import (
    NativeExtensionPublication,
    NativeFileRead,
    ProfileExtensionInventory,
)
from app.services.infrastructure.extension_host.package_files import publication_file
from app.services.infrastructure.extension_host.runtime import NodeHostRuntime
from tests.harness.python.run_context import TestRunContext


@pytest.fixture(scope="module")
def management_component() -> tuple[Path, Path]:
    executable = os.getenv("BOXTEAM_TEST_NODE_HOST_EXECUTABLE")
    modules = os.getenv("BOXTEAM_TEST_NODE_HOST_MODULES")
    if not executable or not modules:
        pytest.skip("UNMET_PREREQUISITE: 需显式提供固定 Node 及原扩展管理依赖")
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    component = context.run_root / "component"
    command = ["bun", "--eval", """
import {buildNodeExtensionHost} from './src/clients/workbench/scripts/nodeHost.mjs';
import {symlink} from 'node:fs/promises';
import {resolve,join} from 'node:path';
const [output,modules]=process.argv.slice(-2);
await buildNodeExtensionHost(resolve('src/clients/workbench'),output);
await symlink(resolve(modules),join(output,'node_modules'),'junction');
""", str(component), modules]
    built = subprocess.run(command, cwd=context.project_root, capture_output=True, text=True, timeout=60, check=False)
    (context.artifacts_dir / "build.json").write_text(json.dumps({"command": command, "returncode": built.returncode,
        "stdout": built.stdout, "stderr": built.stderr}, ensure_ascii=False, indent=2), encoding="utf-8")
    assert built.returncode == 0, built.stderr
    return Path(executable).resolve(), component


@dataclass
class ManagementProcess:
    context: TestRunContext
    directory: Path
    child: OwnedChildProcess
    client: httpx.AsyncClient
    service: dict[str, object]
    frames: list[dict[str, object]]

    async def exchange(self, payload: dict[str, object]) -> dict[str, object]:
        endpoint = "/publication" if payload.get("method") == "extension-publication" else "/management"
        # 精确发布元数据的实际读取者必须在流内完成 I/O；此方法只供拒绝分支使用。
        response = await self.client.post(endpoint, json=payload)
        assert response.status_code == 200, response.text
        value: object = response.json()
        assert isinstance(value, dict) and set(value) in ({"result"}, {"error", "code"}), value
        self.frames.append(value)
        assert self.child.process is not None and self.child.process.returncode is None
        return value

    async def result(self, payload: dict[str, object]) -> dict[str, object]:
        frame = await self.exchange(payload)
        value = frame.get("result")
        assert isinstance(value, dict), frame
        return value


@pytest.fixture
async def management_process(
    management_component: tuple[Path, Path], request: pytest.FixtureRequest,
) -> AsyncIterator[ManagementProcess]:
    executable, component = management_component
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    directory = context.runtime_root_for_node(request.node.nodeid) / "installation"
    directory.mkdir(parents=True)
    child = OwnedChildProcess(f"native-management:{uuid4()}")
    backend_id, token = str(uuid4()), uuid4().hex
    command = [str(executable), str(component / "extensionManagement.mjs"), str(directory), backend_id]
    environment = {**os.environ, "BOXTEAM_PROJECT_ROOT": str(context.project_root), "BOXTEAM_RUNTIME_CONTROL_TOKEN": token}
    frames: list[dict[str, object]] = []
    with (context.artifacts_dir / "node.log").open("w", encoding="utf-8") as log:
        try:
            await child.start(command, env=environment, cwd=directory, stderr=log, stdout=log)
            assert child.process is not None
            async with asyncio.timeout(15):
                while not (directory / "service.json").exists():
                    assert child.process.returncode is None, "Node 管理组件在发布发现记录前退出"
                    await asyncio.sleep(0.02)
            service = json.loads((directory / "service.json").read_text(encoding="utf-8"))
            async with httpx.AsyncClient(base_url=service["base_url"], headers={"X-Local-Token": token}, timeout=10, trust_env=False) as client:
                assert (await client.get("/health")).json() == {**service, "status": "ok"}
                assert service["backend_id"] == backend_id and service["process_id"] == child.process.pid
                process = ManagementProcess(context, directory, child, client, service, frames)
                try:
                    yield process
                finally:
                    if child.process.returncode is None:
                        response = await client.post("/shutdown")
                        assert response.status_code == 200 and response.json() == {"drained": True}, response.text
                        async with asyncio.timeout(10):
                            assert await child.process.wait() == 0
        finally:
            await child.close()
            (context.artifacts_dir / "process.json").write_text(json.dumps({"command": command,
                "state": asdict(child.state)}, ensure_ascii=False, indent=2), encoding="utf-8")
            assert child.state.exit_verified
            (context.artifacts_dir / "frames.json").write_text(json.dumps(frames,
                ensure_ascii=False, indent=2), encoding="utf-8")


class NativeVsixFactory(Protocol):
    def __call__(self, name: str, version: str, script: str, *, uninstall_script: str | None = None) -> Path: ...


@pytest.fixture
def native_vsix(management_process: ManagementProcess) -> NativeVsixFactory:
    context = management_process.context
    sources = context.workspace_root / context.run_root.name / uuid4().hex
    sources.mkdir(parents=True)

    def create(name: str, version: str, script: str, *, uninstall_script: str | None = None) -> Path:
        source = sources / f"{name}-{version}-{uuid4().hex}.vsix"
        manifest = {"publisher": "boxteam-probe", "name": name,
            "version": version, "engines": {"vscode": "^1.0.0"}, "main": "./extension.js",
            "extensionKind": ["workspace"], "activationEvents": ["onCommand:boxteam.persistent"],
            "contributes": {"commands": [{"command": "boxteam.persistent", "title": "常驻管理测试"}]}}
        if uninstall_script is not None:
            manifest["scripts"] = {"vscode:uninstall": "node ./uninstall.js"}
        with ZipFile(source, "w") as archive:
            archive.writestr("extension/package.json", json.dumps(manifest))
            archive.writestr("extension/extension.js", script)
            if uninstall_script is not None:
                archive.writestr("extension/uninstall.js", uninstall_script)
        return source

    return create


async def test_continuous_http_requests_preserve_profile_versions(
    management_process: ManagementProcess, native_vsix: Callable[[str, str, str], Path],
) -> None:
    process = management_process
    empty = await process.result({"method": "inventory"})
    assert empty["extensions"] == [] and isinstance(empty["profile_id"], str) and empty["profile_id"]
    original = await process.result({"method": "install", "vsix_path": str(native_vsix("first", "1.0.0", "first-v1"))})
    installed = cast(dict[str, object], original["installed"])
    profile = empty["profile_id"]
    assert installed == {"id": "boxteam-probe.first", "version": "1.0.0", "profile_id": profile, "reused": False}
    old_key = "boxteam-probe.first-1.0.0"
    second = await process.result({"method": "install", "vsix_path": str(native_vsix("second", "1.0.0", "second-v1"))})
    upgraded = await process.result({"method": "install", "vsix_path": str(native_vsix("first", "2.0.0", "first-v2"))})
    inventory = await process.result({"method": "inventory"})
    assert inventory == upgraded["inventory"]
    assert {entry["extension_key"] for entry in cast(list[dict[str, object]], inventory["extensions"])} == {
        "boxteam-probe.first-2.0.0", "boxteam-probe.second-1.0.0"}
    assert len(cast(dict[str, list[object]], second["inventory"])["extensions"]) == 2

    package = {"method": "extension-publication", "profile_id": profile, "extension_id": "boxteam-probe.first",
        "extension_key": old_key, "current_profile_member": False}

    async def read_publication(selection: dict[str, object]) -> bytes:
        async with process.client.stream("POST", "/publication", json=selection) as response:
            assert response.status_code == 200
            lines = response.aiter_lines()
            frame = json.loads(await anext(lines))
            publication = NativeExtensionPublication.model_validate(frame["result"])
            result = publication_file(publication, extension_key=old_key, relative_segments=["extension.js"], file_operation="read")
            assert isinstance(result, NativeFileRead)
            return b64decode(result.content_base64)

    assert await read_publication(package) == b"first-v1"
    for method in ("extension-file-read", "extension-file-stat"):
        rejected_file = await process.exchange({**package, "method": method, "relative_segments": ["extension.js"]})
        assert set(rejected_file) == {"error", "code"}
    unavailable = await process.exchange({**package, "current_profile_member": True})
    assert set(unavailable) == {"error", "code"}
    assert await process.result({"method": "inventory"}) == inventory

    removed = await process.result({"method": "uninstall", "profile_id": profile,
        "extension_id": "boxteam-probe.first", "version": "2.0.0"})
    assert {entry["extension_key"] for entry in cast(list[dict[str, object]],
        cast(dict[str, object], removed["inventory"])["extensions"])} == {"boxteam-probe.second-1.0.0"}
    assert await read_publication(package) == b"first-v1"
    reused = await process.result({"method": "install", "vsix_path": str(native_vsix("first", "1.0.0", "must-not-replace-v1"))})
    assert cast(dict[str, object], reused["installed"])["reused"] is True
    assert await read_publication({**package, "current_profile_member": True}) == b"first-v1"

    rejected = await process.exchange({"method": "unsupported-operation"})
    assert set(rejected) == {"error", "code"}
    assert (process.directory / "extensions" / old_key / "extension.js").read_bytes() == b"first-v1"


async def test_node_rejects_removed_configuration_operations_without_touching_files(
    management_process: ManagementProcess,
) -> None:
    process = management_process
    environment = await process.result({"method": "environment"})
    folders = [{"native_uri": (process.context.workspace_root / name).as_uri(), "name": name} for name in ("A", "B")]
    view_id = str(uuid4())
    settings = native_path(str(environment["settings_native_uri"]))
    settings.parent.mkdir(parents=True, exist_ok=True)
    original = b'{"probe.original": true}\n'
    settings.write_bytes(original)
    workspace_home = native_path(str(environment["untitled_workspaces_home_uri"]))
    options = {"create": True, "overwrite": True, "unlock": False, "atomic": False, "append": False}
    for request in ({"method": "configuration-view", "id": view_id, "folders": folders},
        *({"method": method, "native_uri": settings.as_uri(), "view": None,
            "content_base64": b64encode(b'{"probe.original": false}').decode(), "options": options}
          for method in ("configuration-stat", "configuration-read", "configuration-write"))):
        frame = await process.exchange(request)
        assert set(frame) == {"error", "code"} and "不支持" in str(frame["error"])
        assert (await process.result({"method": "inventory"}))["extensions"] == []
        assert settings.read_bytes() == original and not (workspace_home / view_id).exists()


async def test_application_restart_recovers_management_and_normal_cleanup_preserves_installed_storage(
    management_process: ManagementProcess, management_component: tuple[Path, Path],
    native_vsix: NativeVsixFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = management_process
    installed = await process.result({"method": "install", "vsix_path": str(native_vsix("crash-held", "1.0.0", "retained-before-crash"))})
    # 真实服务接受原引用后崩溃；不把测试登记的引用称为已运行宿主的退出验证。
    reference = await process.client.post("/references", json={"method": "open-host",
        "backend_id": process.service["backend_id"], "runtime_instance_id": process.service["runtime_instance_id"],
        "generation": str(uuid4()), "capability": uuid4().hex})
    reference.raise_for_status()
    assert set(reference.json()) == {"result"}
    record = process.directory / "service.json"
    environment = await process.result({"method": "environment"})
    storage = native_path(str(environment["global_storage_home_uri"])) / "boxteam-probe.crash-held" / "saved.json"
    storage.parent.mkdir(parents=True, exist_ok=True)
    storage.write_text('{"persisted": true}', encoding="utf-8")
    hook_result = process.context.artifacts_dir / "uninstall-hook.json"
    hook_script = "require('node:fs').writeFileSync(" + json.dumps(str(hook_result)) + ", JSON.stringify({" \
        "controlCredentialPresent: Boolean(process.env.BOXTEAM_RUNTIME_CONTROL_TOKEN)}));"
    removed = await process.result({"method": "install", "vsix_path": str(native_vsix("crash-removed", "1.0.0",
        "not-activated", uninstall_script=hook_script))})
    await process.result({"method": "uninstall", "profile_id": cast(dict[str, object], removed["installed"])["profile_id"],
        "extension_id": "boxteam-probe.crash-removed", "version": "1.0.0"})
    obsolete_package = process.directory / "extensions/boxteam-probe.crash-removed-1.0.0"
    obsolete = process.directory / "extensions/.obsolete"
    assert json.loads(obsolete.read_text())["boxteam-probe.crash-removed-1.0.0"] is True
    assert obsolete_package.is_dir() and not hook_result.exists()
    package = process.directory / "extensions/boxteam-probe.crash-held-1.0.0/extension.js"
    assert package.read_bytes() == b"retained-before-crash"
    assert process.child.process is not None
    process.child.process.kill()
    async with asyncio.timeout(10):
        assert await process.child.process.wait() != 0
    await process.child.close()
    assert process.child.state.exit_verified
    executable, component = management_component
    metadata = json.loads((component / "version.json").read_text())
    runtime = NodeHostRuntime(executable, component / "extensionHostProcess.mjs",
        metadata["commit"], metadata["version"], metadata["nodeVersion"])
    monkeypatch.setattr("app.gateway.runtime.extension_management.get_gateway_local_token",
        lambda: process.client.headers["X-Local-Token"])
    candidate = GatewayExtensionManagement(backend_id=str(process.service["backend_id"]), runtime=runtime,
        directory=process.directory, environment={**os.environ, "BOXTEAM_PROJECT_ROOT": str(process.context.project_root)},
        log_dir=process.directory / "candidate-logs",
        logging_startup={"revision": "application-restart", "level": "info", "pretty": False})
    try:
        await candidate.start()
        candidate.assert_healthy()
        assert candidate.instance is not None and candidate.process is not None
        replacement = candidate.instance.model_dump(mode="json")
        assert replacement["process_id"] != process.service["process_id"]
        assert replacement["runtime_instance_id"] != process.service["runtime_instance_id"]
        assert json.loads(record.read_text()) == replacement
        inventory = await candidate.call({"method": "inventory"}, response_type=ProfileExtensionInventory)
        assert inventory.model_dump(mode="json") == installed["inventory"]
        assert package.read_bytes() == b"retained-before-crash" and storage.read_text() == '{"persisted": true}'
        # ready 不清包、不恢复崩溃前的内存引用，也不执行未知安装／卸载脚本。
        assert obsolete_package.is_dir() and not hook_result.exists()
    finally:
        await candidate.close()
    assert candidate.drained and not record.exists()
    assert not obsolete_package.exists()
    assert json.loads(hook_result.read_text()) == {"controlCredentialPresent": False}
    assert package.read_bytes() == b"retained-before-crash" and storage.read_text() == '{"persisted": true}'
    (process.context.artifacts_dir / "reconstruction.json").write_text(json.dumps({
        "original_service": process.service, "original_reference": reference.json(), "installed": installed,
        "replacement_service": replacement, "management_exit": asdict(process.child.state),
        "package_preserved": package.read_text(), "storage_preserved": json.loads(storage.read_text()),
        "obsolete_removed_on_shutdown": not obsolete_package.exists(), "uninstall_hook": json.loads(hook_result.read_text()),
        "boundary": "真实Node原生安装、卸载及内存引用准入，真实kill后由新应用owner恢复；正常停机运行原清理与卸载hook。持久数据是原globalStorage目录文件，未创建运行宿主或验证插件Memento恢复。",
    }, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.parametrize("body,status", [("{broken", 400), ('{"method":null}', 200)])
async def test_invalid_http_request_does_not_kill_shared_service(
    management_process: ManagementProcess, body: str, status: int,
) -> None:
    process = management_process
    response = await process.client.post("/management", content=body)
    assert response.status_code == status and set(response.json()) == {"error", "code"}
    assert (await process.result({"method": "inventory"}))["extensions"] == []
    assert (await process.client.get("/health")).json() == {**process.service, "status": "ok"}


async def test_publication_stream_blocks_other_clients_and_full_shutdown(
    management_process: ManagementProcess, native_vsix: Callable[[str, str, str], Path],
) -> None:
    process = management_process
    installed = await process.result({"method": "install", "vsix_path": str(native_vsix("held", "1.0.0", "held-v1"))})
    selection = {"method": "extension-publication", "profile_id": cast(dict[str, object], installed["installed"])["profile_id"],
        "extension_id": "boxteam-probe.held", "extension_key": "boxteam-probe.held-1.0.0", "current_profile_member": True}
    async with process.client.stream("POST", "/publication", json=selection) as response:
        lines = response.aiter_lines()
        assert "result" in json.loads(await anext(lines))
        other = asyncio.create_task(process.result({"method": "inventory"}))
        # 请求被流持有的全服务队列阻塞，另一连接的健康检查仍可达。
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(other), 0.2)
        assert (await process.client.get("/health")).json()["status"] == "ok"
    assert (await other)["extensions"] == cast(dict[str, object], installed["inventory"])["extensions"]

    async with process.client.stream("POST", "/publication", json=selection) as response:
        lines = response.aiter_lines()
        assert "result" in json.loads(await anext(lines))
        shutdown = asyncio.create_task(process.client.post("/shutdown"))
        async with asyncio.timeout(5):
            while (await process.client.get("/health")).json()["status"] != "stopping":
                await asyncio.sleep(0.02)
        assert not shutdown.done()
        rejected = await process.client.post("/management", json={"method": "inventory"})
        assert rejected.status_code == 503
    assert (await shutdown).json() == {"drained": True}
    assert process.child.process is not None
    async with asyncio.timeout(10):
        assert await process.child.process.wait() == 0
    assert not (process.directory / "service.json").exists()


@pytest.mark.parametrize("headers,status", [({"X-Local-Token": "wrong"}, 401), ({"Origin": "http://localhost"}, 403),
    ({"Sec-Fetch-Site": "same-origin"}, 403)])
async def test_http_management_keeps_original_local_authentication(
    management_process: ManagementProcess, headers: dict[str, str], status: int,
) -> None:
    response = await management_process.client.get("/health", headers=headers)
    assert response.status_code == status
    assert (await management_process.client.get("/health")).json()["status"] == "ok"


async def test_second_service_cannot_take_the_installation_lock(
    management_process: ManagementProcess, management_component: tuple[Path, Path],
) -> None:
    process = management_process
    executable, component = management_component
    second = OwnedChildProcess(f"competing-management:{uuid4()}")
    environment = {**os.environ, "BOXTEAM_PROJECT_ROOT": str(process.context.project_root),
        "BOXTEAM_RUNTIME_CONTROL_TOKEN": process.client.headers["X-Local-Token"]}
    with (process.context.artifacts_dir / "competing-node.log").open("w", encoding="utf-8") as log:
        try:
            await second.start([str(executable), str(component / "extensionManagement.mjs"), str(process.directory),
                str(process.service["backend_id"])], env=environment, cwd=process.directory, stderr=log, stdout=log)
            assert second.process is not None
            async with asyncio.timeout(10):
                assert await second.process.wait() != 0
        finally:
            await second.close()
            assert second.state.exit_verified
    assert "扩展管理安装域锁不可用: busy" in (process.context.artifacts_dir / "competing-node.log").read_text(encoding="utf-8")
    assert json.loads((process.directory / "service.json").read_text(encoding="utf-8")) == process.service
    assert (await process.result({"method": "inventory"}))["extensions"] == []


async def test_discovery_publication_failure_releases_lock_and_temporary_record(
    management_component: tuple[Path, Path], request: pytest.FixtureRequest,
) -> None:
    executable, component = management_component
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    directory = context.runtime_root_for_node(request.node.nodeid) / "installation"
    # 发布目标已有目录时原子替换明确失败；不能删除用户原件或留下本次临时文件。
    (directory / "service.json").mkdir(parents=True)
    environment = {**os.environ, "BOXTEAM_PROJECT_ROOT": str(context.project_root),
        "BOXTEAM_RUNTIME_CONTROL_TOKEN": uuid4().hex}
    for attempt in range(2):
        child = OwnedChildProcess(f"failed-publication:{uuid4()}")
        log_path = context.artifacts_dir / f"failed-publication-{attempt}.log"
        with log_path.open("w", encoding="utf-8") as log:
            try:
                await child.start([str(executable), str(component / "extensionManagement.mjs"), str(directory), str(uuid4())],
                    env=environment, cwd=directory, stderr=log, stdout=log)
                assert child.process is not None
                async with asyncio.timeout(10):
                    assert await child.process.wait() != 0
            finally:
                await child.close()
                assert child.state.exit_verified
        assert not list(directory.glob("service-*.tmp"))
        output = log_path.read_text(encoding="utf-8")
        assert "rename" in output and "扩展管理安装域锁不可用" not in output
        assert (directory / "service.json").is_dir()
