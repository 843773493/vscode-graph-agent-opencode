"""源码外 Linux 发行物的插件安装、运行、切版与卸载闭环。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode
from zipfile import ZipFile

import commentjson
import httpx
import psutil
import pytest
from websockets.sync.client import connect

from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.integration.gateway.test_service_plugin_dependency_preparation import (
    _dependency_wheel,
    _python_lock,
)
from tests.integration.gateway.test_service_plugins import NODE_PLUGIN, PYTHON_PLUGIN
from tests.integration.gateway.test_terminal_plugin_lifecycle import (
    _receive_output,
    _wait_for_plugin,
)
from tests.support.model_stream.config import prepare_e2e_model_stream_config
from tests.support.paths import chromium_temporary_environment
from tests.support.plugin_package import expect_plugin_install_failure, install_plugin
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.workspace import terminate_process, wait_for_http_ok
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="本用例验证 Linux 发行物")


@pytest.fixture
def distribution_model_stream(request: pytest.FixtureRequest) -> Path:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime = context.runtime_root_for_node(request.node.nodeid)
    assets = runtime / "model-stream-assets"
    (assets / "scenarios").mkdir(parents=True)
    cassette = build_cassette(asset_id="distribution-session", model="distribution-session",
        url="http://distribution-model.invalid/v1/chat/completions",
        frames=chat_completion_frames(response_id="chatcmpl-distribution-session", model="distribution-session",
            deltas=[{"role": "assistant", "content": "发行会话恢复成功。"}], finish_reason="stop"))
    (assets / "session.json").write_text(json.dumps(cassette.raw, ensure_ascii=False), encoding="utf-8")
    (assets / "scenarios/basic-chat-tool-loop.jsonc").write_text(json.dumps({
        "scenario_id": "basic-chat-tool-loop", "asset": "session.json",
        "business_assertion": "relocated-distribution-session-reopen",
    }), encoding="utf-8")
    config_path = prepare_e2e_model_stream_config(
        Path.cwd() / "configs/tests/model_stream/model_stream_web_basic_chat_tool_loop.jsonc",
        runtime_root=runtime, artifacts_dir=context.artifacts_dir,
        mode="replay",
    ).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["model_stream"]["transport"]["fixture_root"] = str(assets)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


@pytest.fixture
def distribution_gateway(request: pytest.FixtureRequest,
    distribution_model_stream: Path,
) -> Iterator[tuple[httpx.Client, TestRunContext, Path, Path]]:
    asset_input = os.environ.get("BOXTEAM_TEST_RUNTIME_ASSET")
    if not asset_input:
        pytest.skip("需要 BOXTEAM_TEST_RUNTIME_ASSET 指向已构建的 Linux runtime tgz")
    asset = Path(asset_input).resolve(strict=True)
    launcher_input = os.environ.get("BOXTEAM_TEST_PREPARED_LAUNCHER")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime = context.runtime_root_for_node(request.node.nodeid)
    initial = runtime / "installed"
    initial.mkdir(parents=True)
    if launcher_input:
        # 安装阶段准备一次 Launcher 依赖；运行阶段仅复制不可变制品，不使用包管理器。
        shutil.copytree(Path(launcher_input).resolve(strict=True), initial / "launcher")
    with tarfile.open(asset) as archive:
        archive.extractall(initial, filter="data")
    relocated = runtime / "relocated distribution"
    initial.rename(relocated)
    package = relocated / "package"
    manifest_path = package / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    application = package / manifest["application_root"]
    python = package / manifest["python_executable"]
    node = package / manifest["node"]["executable"]
    ports = integration_port_block_for_file(Path(request.node.path))
    gateway_port = 8014 if launcher_input else ports.port(21)
    backend_port = 8010 if launcher_input else ports.port(20)
    gateway_url = f"http://127.0.0.1:{gateway_port}"
    workspace = prepare_test_workspace(workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", template_items=())
    config_path = install_test_workspace_config(workspace_root=workspace,
        config_path=Path.cwd() / "configs/tests/workspace/default.jsonc",
        schema_path=application / "configs/workspace_schema.jsonc")
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    provider = next(item for item in config["llm"]["providers"] if item["id"] == "primary")
    provider.update(endpoint="http://distribution-model.invalid/v1", model="distribution-session",
        api_key="distribution-plugin-unused-key", custom_llm_provider="openai")
    for item in config["llm"]["providers"]:
        if "api_key" in item:
            item["api_key"] = "distribution-plugin-unused-key"
    for agent in config["agents"].values():
        agent["model"] = {"primary_provider": "primary", "fallback_providers": []}
    config.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = gateway_url
    config_path.write_text(json.dumps(config), encoding="utf-8")
    (workspace / "distribution-debug.mjs").write_text(
        "const state = { counter: 1 };\nstate.counter += 2;\nstate.counter += 3;\n"
        "console.log('distribution-debug-result', state.counter);\nsetInterval(() => {}, 1000);\n",
        encoding="utf-8",
    )
    # 公共装具会初始化开发版 Home；发行验收必须从发行物自己的初始化入口建新 Home。
    home = runtime / "distribution-home"
    empty_path = runtime / "empty-path"
    empty_path.mkdir()
    environment = {key: value for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME"} and not key.startswith(("BOXTEAM_", "UV_", "BUN_", "npm_"))}
    environment.update({"BOXTEAM_HOME": str(home), "HOME": str(runtime / "user-home"),
        "BOXTEAM_PROJECT_ROOT": str(application), "BOXTEAM_RUNTIME_MANIFEST": str(manifest_path),
        "BOXTEAM_DISTRIBUTION": manifest["distribution"], "BOXTEAM_NODE_BIN": str(node),
        "BOXTEAM_PYTHON_BIN": str(python), "BOXTEAM_WEB_ASSETS": str(package / manifest["web_assets"]),
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": str(package / manifest["chromium_executable"]),
        "BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT": str(workspace), "WORKSPACE_ROOT": str(workspace),
        "BOXTEAM_GATEWAY_URL": gateway_url, "BOXTEAM_DEFAULT_BACKEND_PORT": str(backend_port),
        "PATH": str(empty_path), "XDG_CACHE_HOME": str(runtime / "empty-cache"),
        "BOXTEAM_TEST_MODEL_STREAM_CONFIG": str(distribution_model_stream),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"})
    command = [str(python), "-m", "configs.boxteam", "initialize", "--project-root", str(application)]
    initialized = subprocess.run(command, cwd=application, env=environment,
        text=True, capture_output=True, check=False, timeout=60)
    (context.artifacts_dir / "initialize.stdout.raw").write_text(initialized.stdout, encoding="utf-8")
    (context.artifacts_dir / "initialize.stderr.raw").write_text(initialized.stderr, encoding="utf-8")
    assert initialized.returncode == 0, initialized.stderr
    with asset.open("rb") as stream:
        asset_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    (context.artifacts_dir / "distribution.json").write_text(json.dumps({
        "asset": str(asset), "sha256": asset_digest,
        "runtime_manifest": manifest, "application": str(application), "python": str(python), "node": str(node),
        "gateway_url": gateway_url, "path": str(empty_path), "home": str(home),
        "entrypoint": "launcher" if launcher_input else "gateway",
    }, indent=2) + "\n", encoding="utf-8")
    with (context.artifacts_dir / "gateway.stdout.raw").open("w") as stdout, (
        context.artifacts_dir / "gateway.stderr.raw"
    ).open("w") as stderr:
        command = ([str(node), str(relocated / "launcher/bin/boxteam.mjs"), "--no-open"]
            if launcher_input else [str(python), "-m", "app.runtime.http_server", "app.gateway.main:app",
                "--host", "127.0.0.1", "--port", str(gateway_port), "--shutdown-on-stdin-eof"])
        process = subprocess.Popen(command, cwd=application, env=environment,
            stdin=subprocess.PIPE, text=True, stdout=stdout, stderr=stderr)
        try:
            if launcher_input:
                # Launcher由子进程监听；直接Gateway装具的根PID监听检查不适用于它。
                deadline = time.monotonic() + 60
                with httpx.Client(timeout=1, trust_env=False) as readiness:
                    while True:
                        assert process.poll() is None, "Launcher在就绪前退出"
                        try:
                            response = readiness.get(gateway_url + "/api/gateway/health")
                        except httpx.TransportError:
                            assert time.monotonic() < deadline, "Launcher的Gateway未在期限内就绪"
                            time.sleep(0.2)
                            continue
                        response.raise_for_status()
                        break
            else:
                wait_for_http_ok(gateway_url + "/api/gateway/health", process)
            with httpx.Client(base_url=gateway_url, timeout=90, trust_env=False) as client:
                credential = client.get("/api/gateway/auth/local-credential",
                    headers={"Sec-Fetch-Site": "same-origin"}).raise_for_status().json()["data"]["token"]
                client.headers["X-Local-Token"] = credential
                active = client.get("/api/gateway/workspaces").raise_for_status().json()["data"]["active_workspace"]
                client.headers.update({"X-BoxTeam-Target-Gateway-Id": active["gateway_id"],
                    "X-BoxTeam-Gateway-Id": active["gateway_id"], "X-BoxTeam-Workspace-Id": active["workspace_id"]})
                client.get("/api/v1/workspace").raise_for_status()
                yield client, context, package, home
        finally:
            if launcher_input:
                # 正式停止命令等待Launcher PID消失；先收割本测试持有的Popen。
                with subprocess.Popen([str(node), str(relocated / "launcher/bin/boxteam.mjs"), "stop"],
                    cwd=application, env=environment, text=True, stdout=stdout, stderr=stderr) as stopper:
                    process.wait(timeout=150)
                    assert stopper.wait(timeout=20) == 0
            else:
                terminate_process(process, timeout_seconds=150, stdin_eof=True)


def test_relocated_distribution_completes_service_plugin_lifecycle(
    distribution_gateway, request: pytest.FixtureRequest,
) -> None:
    client, context, distribution, home = distribution_gateway
    evidence: list[dict[str, object]] = []
    gateway_id = client.get("/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]
    workspace_id = client.headers["X-BoxTeam-Workspace-Id"]
    manifest = json.loads((distribution / "runtime-manifest.json").read_text(encoding="utf-8"))
    python = distribution / manifest["python_executable"]
    node = distribution / manifest["node"]["executable"]
    python_target = subprocess.run([str(python), "-c",
        "import json,platform,sys;print(json.dumps({'version':platform.python_version(),'abi':f'cp{sys.version_info.major}{sys.version_info.minor}'}))"],
        text=True, capture_output=True, check=True, timeout=10)
    node_target = subprocess.run([str(node), "--print",
        "JSON.stringify({version:process.versions.node,abi:process.versions.modules,glibc:process.report.getReport().header.glibcVersionRuntime})"],
        text=True, capture_output=True, check=True, timeout=10)
    node_identity = json.loads(node_target.stdout)
    target = {"os": "linux", "architecture": "x64", "system_baseline": f"glibc-{node_identity.pop('glibc')}",
        "python": json.loads(python_target.stdout), "node": node_identity}

    def exchange(method: str, path: str, body: dict[str, object] | None = None) -> httpx.Response:
        request_id = f"distribution-plugin-{len(evidence)}"
        response = client.request(method, path, json=body, headers={"X-Request-ID": request_id})
        assert response.headers["X-Request-ID"] == request_id
        payload = response.json()
        if "request_id" in payload:
            assert payload["request_id"] == request_id
        evidence.append({"method": method, "path": path, "status": response.status_code, "body": payload})
        return response

    def states() -> dict[str, dict]:
        result = exchange("POST", "/api/gateway/service-plugins/status", {"gateway_id": gateway_id}).raise_for_status()
        return {item["binding"]["plugin_id"]: item for item in result.json()["data"]["plugins"]}

    def bind(entries: list[dict[str, object]], *, workspace: bool = False) -> dict[str, dict]:
        source_key = "workspace_root_mutable_override" if workspace else "gateway_mutable_override"
        path = f"/api/v1/workspaces/{workspace_id}/config/sources/{source_key}" if workspace else f"/api/gateway/config/sources/{source_key}"
        document = exchange("GET", path).raise_for_status().json()["data"]
        source = commentjson.loads(document["jsonc"])
        source.setdefault("plugins", {})["runtime"] = {"entries": entries}
        reply = exchange("PUT", path, {"gateway_id": gateway_id,
            **({"workspace_id": workspace_id} if workspace else {}), "source_key": source_key,
            "jsonc": json.dumps(source), "base_file_digest": document["file_digest"],
            "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
            "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"],
            "idempotency_key": f"distribution-bind-{len(evidence)}"}).raise_for_status()
        assert reply.json()["data"]["edit"]["state"] == "active"
        return states()

    def install(path: Path) -> None:
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        install_plugin(lambda endpoint, payload: exchange("POST", endpoint, payload),
            gateway_id=gateway_id, path=str(path), expected_digest=digest)

    def echo(item: dict) -> dict:
        binding, instance = item["binding"], item["instance"]
        path = (f"/api/gateway/service-plugins/{binding['package_id']}/{binding['plugin_id']}/instances/"
            f"{instance['instance_id']}/services/echo/value?package_version={binding['package_version']}")
        result = exchange("GET", path).raise_for_status().json()
        assert result["instance_id"] == instance["instance_id"]
        assert result["version"] == binding["package_version"]
        assert result["pid"] == instance["process_id"]
        return result

    def entries(version: str, *, enabled: bool = True) -> list[dict[str, object]]:
        return [{"package_id": "distribution.probe", "plugin_id": runtime,
            "package_version": version, "enabled": enabled} for runtime in ("python", "node")]

    def require_exited(items: dict[str, dict]) -> None:
        processes = [psutil.Process(item["instance"]["process_id"]) for item in items.values()
            if item.get("instance") and psutil.pid_exists(item["instance"]["process_id"])]
        _, alive = psutil.wait_procs(processes, timeout=5)
        assert not alive, [process.pid for process in alive]

    def uninstall(package_id: str, version: str) -> None:
        exchange("POST", "/api/gateway/service-plugins/uninstall", {"gateway_id": gateway_id,
            "package_id": package_id, "package_version": version}).raise_for_status()
        assert not (home / "plugins/packages" / package_id / version).exists()

    def package(version: str, *, package_target: dict | None = None, missing_dependency: str | None = None) -> Path:
        archive_path = context.artifacts_dir / f"distribution-probe-{version}.zip"
        probe = {"manifest_version": 1, "package_id": "distribution.probe", "version": version,
            "name": "发行闭环探针", "extension_contract_version": 1,
            "target": target if package_target is None else package_target,
            "plugins": [{"id": runtime, "name": runtime, "instance_scope": "gateway",
                "runtime": runtime, "entrypoint": ["entry.py" if runtime == "python" else "entry.mjs"],
                "management_protocol_version": 1,
                "requires": [{"extension_point": "services", "version": 1}],
                "contributes": {"services": [{"service_id": "echo", "service_type": "terminal", "protocol": "http"}]}}
                for runtime in ("python", "node")]}
        with ZipFile(archive_path, "w") as archive:
            archive.writestr("plugin.jsonc", json.dumps(probe))
            archive.writestr("pyproject.toml", "[project]\nname='plugin-dependency-fixture'\nversion='1.0.0'\n"
                "requires-python='==3.12.*'\ndependencies=['plugin-dependency==1.0.0']\n")
            archive.writestr("uv.lock", _python_lock())
            archive.writestr("entry.py", PYTHON_PLUGIN)
            archive.writestr("entry.mjs", NODE_PLUGIN)
            archive.writestr("local_module.py", "from plugin_dependency import VALUE\nassert VALUE == 'locked-dependency-ready'\n")
            if missing_dependency != "python":
                with ZipFile(BytesIO(_dependency_wheel())) as dependency:
                    for entry in dependency.infolist():
                        archive.writestr(f"python/site-packages/{entry.filename}", dependency.read(entry.filename))
            if missing_dependency == "node":
                dependencies = {"distribution-dependency": "1.0.0"}
                archive.writestr("package.json", json.dumps({"name": "distribution-probe", "dependencies": dependencies}))
                archive.writestr("bun.lock", json.dumps({"lockfileVersion": 1,
                    "workspaces": {"": {"name": "distribution-probe", "dependencies": dependencies}},
                    "packages": {"distribution-dependency": ["distribution-dependency@1.0.0", "", {}, "sha512-fixture"]}}))
        return archive_path

    try:
        install(package("1.0.0"))
        assert states() == {}, "安装及读取不能启动插件"
        original = bind(entries("1.0.0"))
        for runtime, executable in (("python", python), ("node", node)):
            assert original[runtime]["state"] == "ready"
            result = echo(original[runtime])
            process = psutil.Process(result["pid"])
            assert Path(process.exe()).resolve() == executable.resolve()
            assert process.environ()["PATH"].endswith("empty-path")
            assert Path(process.environ()["BOXTEAM_PROJECT_ROOT"]) == distribution / "application"
        source_path = "/api/gateway/config/sources/gateway_mutable_override"
        source = exchange("GET", source_path).raise_for_status().json()["data"]
        incompatible_targets = {}
        for runtime in ("node", "python"):
            for field, value in (("version", "0.0.0"), ("abi", "000" if runtime == "node" else "cp000")):
                incompatible = json.loads(json.dumps(target))
                incompatible[runtime][field] = value
                incompatible_targets[f"{runtime}-{field}"] = incompatible
        incompatible_targets["os"] = {**target, "os": "windows", "system_baseline": "windows-10.0.19041"}
        incompatible_targets["architecture"] = {**target, "architecture": "arm64"}
        for index, (label, incompatible) in enumerate(incompatible_targets.items()):
            rejected = expect_plugin_install_failure(lambda endpoint, payload: exchange("POST", endpoint, payload),
                gateway_id=gateway_id, path=str(package(f"0.1.{index}", package_target=incompatible)))
            assert rejected.error and "package=" in rejected.error and "gateway=" in rejected.error, (label, rejected)
            assert "完整插件 ZIP" in rejected.error
            assert states() == original
            for item in original.values():
                echo(item)
        for index, runtime in enumerate(("python", "node")):
            rejected = expect_plugin_install_failure(lambda endpoint, payload: exchange("POST", endpoint, payload),
                gateway_id=gateway_id, path=str(package(f"0.2.{index}", missing_dependency=runtime)))
            expected = "python/site-packages" if runtime == "python" else "node_modules"
            assert rejected.error and f"缺少已准备的 {expected}" in rejected.error
            assert states() == original
            for item in original.values():
                echo(item)
        candidate = commentjson.loads(source["jsonc"])
        candidate["plugins"]["runtime"]["entries"] = entries("99.0.0")
        rejected = exchange("PUT", source_path, {"gateway_id": gateway_id, "source_key": "gateway_mutable_override",
            "jsonc": json.dumps(candidate), "base_file_digest": source["file_digest"],
            "base_layer_revision": source["layer_revision"], "base_layer_digest": source["layer_digest"],
            "expected_active_revision": source["active_revision"], "expected_active_digest": source["active_digest"],
            "idempotency_key": "distribution-missing-version"})
        assert rejected.status_code == 422
        assert "distribution.probe@99.0.0" in rejected.text and "此确切版本" in rejected.text
        retained = exchange("GET", source_path).raise_for_status().json()["data"]
        for field in ("jsonc", "file_digest", "layer_revision", "layer_digest", "active_revision", "active_digest"):
            assert retained[field] == source[field]
        assert states() == original
        for item in original.values():
            echo(item)
        inventory = exchange("POST", "/api/gateway/service-plugins/inventory", {"gateway_id": gateway_id})
        assert {item["manifest"]["version"] for item in inventory.raise_for_status().json()["data"]["packages"]
            if item["manifest"]["package_id"] == "distribution.probe"} == {"1.0.0"}
        assert {path.name for path in (home / "plugins/packages/distribution.probe").iterdir()} == {"1.0.0"}
        install(package("2.0.0"))
        assert states() == original, "安装新版不能暗中换绑或更换实例"
        current = bind(entries("2.0.0"))
        require_exited(original)
        for runtime in ("python", "node"):
            echo(current[runtime])
            assert current[runtime]["instance"]["instance_id"] != original[runtime]["instance"]["instance_id"]
        uninstall("distribution.probe", "1.0.0")
        restarted = exchange("POST", "/api/gateway/service-plugins/restart", {"gateway_id": gateway_id,
            "package_id": "distribution.probe", "package_version": "2.0.0", "plugin_id": "python",
            "revision": current["python"]["revision"]}).raise_for_status().json()["data"]
        require_exited({"python": current["python"]})
        echo(restarted)
        assert states()["node"]["instance"] == current["node"]["instance"]
        current["python"] = restarted
        disabled = bind(entries("2.0.0", enabled=False))
        require_exited(current)
        assert {item["state"] for item in disabled.values()} == {"stopped"}
        blocked = exchange("POST", "/api/gateway/service-plugins/uninstall", {"gateway_id": gateway_id,
            "package_id": "distribution.probe", "package_version": "2.0.0"})
        assert blocked.status_code == 409, "关闭仍保留绑定，不能卸载"
        assert bind([]) == {}
        uninstall("distribution.probe", "2.0.0")

        # 正式自带包真正加载 node-pty 原生模块并创建 PTY，而不止探针 ready。
        developer_tools = distribution / "application/resources/plugins/developer-tools.zip"
        install(developer_tools)
        bind([{"package_id": "boxteam.developer-tools", "package_version": "0.1.0",
            "plugin_id": "terminal", "enabled": True}], workspace=True)
        terminal = _wait_for_plugin(client, gateway_id=gateway_id, workspace_id=workspace_id, state="ready")
        assert terminal["state"] == "ready"
        session = exchange("POST", "/api/v1/sessions", {"title": "源码外发行 PTY"}).raise_for_status().json()["data"]
        resource = exchange("POST", "/api/v1/resources/terminals", {"session_id": session["session_id"],
            "thread_id": session["thread_id"], "retention_scope": "workspace", "title": "发行 node-pty",
            "cwd_relative": ".", "cols": 100, "rows": 30}).raise_for_status().json()["data"]
        grant = exchange("POST", f"/api/v1/resources/{resource['resource_id']}/transport-grant",
            {"expected_revision": resource["owner_revision"]}).raise_for_status().json()["data"]
        query = urlencode({"workspace_id": workspace_id, "package_version": "0.1.0",
            "token": client.headers["X-Local-Token"], "target_gateway_id": gateway_id})
        socket_url = str(client.base_url).rstrip("/").replace("http://", "ws://") + (
            f"/api/gateway/service-plugins/boxteam.developer-tools/terminal/instances/"
            f"{terminal['instance']['instance_id']}/services/socket/terminal?{query}")
        with connect(socket_url, open_timeout=10, close_timeout=3) as socket:
            socket.send(json.dumps({"type": "attach", "terminalId": resource["resource_id"],
                "grantToken": grant["grant_token"], "cols": 100, "rows": 30}))
            assert json.loads(socket.recv(timeout=10))["type"] == "attached"
            # 让 shell 执行后再拼成标记，避免把输入回显误认为命令输出。
            socket.send(json.dumps({"type": "input", "data": "printf 'distribution-%s\\n' 'native-ready'\n"}))
            output = _receive_output(socket, "distribution-native-ready")
            evidence.append({"terminal_output": output})
        latest = exchange("GET", f"/api/v1/resources/{resource['resource_id']}").raise_for_status().json()["data"]
        exchange("POST", f"/api/v1/resources/{resource['resource_id']}/stop",
            {"expected_revision": latest["owner_revision"]}).raise_for_status()
        # 同一发行包的 Debug 插件也必须在空 PATH 下真实启动目标，不能只验管理 ready。
        bind([{"package_id": "boxteam.developer-tools", "package_version": "0.1.0",
            "plugin_id": "debugging", "enabled": True}], workspace=True)
        require_exited({"terminal": terminal})
        debug_plugin = states()["debugging"]
        deadline = time.monotonic() + 30
        while debug_plugin["state"] != "ready":
            assert debug_plugin["state"] != "failed", debug_plugin
            assert time.monotonic() < deadline, debug_plugin
            time.sleep(0.1)
            debug_plugin = states()["debugging"]
        owner = {"session_id": session["session_id"], "thread_id": session["thread_id"]}
        configured = exchange("POST", "/api/v1/debug/node/configurations", {**owner,
            "name": "源码外发行 Debug", "script_path": "distribution-debug.mjs", "working_directory": ".",
            "breakpoints": [{"path": "distribution-debug.mjs", "line": 2}]}).raise_for_status().json()["data"]
        start = {**owner, "path": "distribution-debug.mjs", "configuration_id": configured["active_configuration_id"]}
        run = exchange("POST", "/api/v1/debug/node/start", start).raise_for_status().json()["data"]
        assert run["status"] == "paused" and run["call_stack"][0]["line"] == 2, run
        assert run["debugger_binding"]["instance_id"] == debug_plugin["instance"]["instance_id"]
        assert Path(psutil.Process(run["pid"]).exe()).resolve() == node.resolve()
        evaluated = exchange("POST", "/api/v1/debug/node/action", {**owner, "action": "evaluate",
            "params": {"expression": "state.counter += 10"}}).raise_for_status().json()["data"]
        assert evaluated["run_id"] == run["run_id"] and evaluated["last_evaluation"]["value"] == "11"
        stepped = exchange("POST", "/api/v1/debug/node/action", {**owner, "action": "step_over"}).raise_for_status().json()["data"]
        assert stepped["run_id"] == run["run_id"] and stepped["call_stack"][0]["line"] == 3
        exchange("POST", "/api/v1/debug/node/action", {**owner, "action": "continue"}).raise_for_status()
        deadline = time.monotonic() + 10
        while True:
            observed = exchange("GET", f"/api/v1/debug/node?{urlencode(owner)}").raise_for_status().json()["data"]
            if "distribution-debug-result 16" in observed["output"]:
                break
            assert time.monotonic() < deadline, observed
            time.sleep(0.1)
        stopped = exchange("POST", "/api/v1/debug/node/action", {**owner, "action": "stop"}).raise_for_status().json()["data"]
        assert stopped["status"] == "exited" and not psutil.pid_exists(run["pid"]), stopped
        restarted = exchange("POST", "/api/gateway/service-plugins/restart", {"gateway_id": gateway_id,
            "package_id": "boxteam.developer-tools", "package_version": "0.1.0", "plugin_id": "debugging",
            "workspace_id": workspace_id, "revision": debug_plugin["revision"]}).raise_for_status().json()["data"]
        assert restarted["state"] == "ready"
        assert restarted["instance"]["instance_id"] != debug_plugin["instance"]["instance_id"]
        require_exited({"debugging": debug_plugin})
        second_run = exchange("POST", "/api/v1/debug/node/start", start).raise_for_status().json()["data"]
        assert second_run["status"] == "paused" and second_run["run_id"] != run["run_id"]
        assert second_run["debugger_binding"]["instance_id"] == restarted["instance"]["instance_id"]
        second_stop = exchange("POST", "/api/v1/debug/node/action", {**owner, "action": "stop"}).raise_for_status().json()["data"]
        assert second_stop["status"] == "exited" and not psutil.pid_exists(second_run["pid"]), second_stop
        evidence.append({"debug_runs": [run["run_id"], second_run["run_id"]],
            "debug_pids_exited": [run["pid"], second_run["pid"]],
            "debug_instances": [debug_plugin["instance"]["instance_id"], restarted["instance"]["instance_id"]]})
        bind([{"package_id": "boxteam.developer-tools", "package_version": "0.1.0",
            "plugin_id": "browser", "enabled": True}], workspace=True)
        require_exited({"debugging": restarted})
        deadline = time.monotonic() + 30
        while True:
            browser_plugin = states()["browser"]
            if browser_plugin["state"] == "ready":
                break
            assert browser_plugin["state"] != "failed" and time.monotonic() < deadline, browser_plugin
            time.sleep(0.1)
        browser_resource = exchange("POST", "/api/v1/resources/browsers", {**owner,
            "retention_scope": "workspace", "title": "源码外发行 Browser", "url": "about:blank"}).raise_for_status().json()["data"]
        operated = exchange("POST", f"/api/v1/resources/{browser_resource['resource_id']}/browser-operation", {
            "expected_revision": browser_resource["owner_revision"], "thread": browser_resource["source_thread"],
            "action": "navigate", "payload": {"type": "url", "url": "data:text/html,<title>distribution-browser-api</title>"},
        }).raise_for_status().json()["data"]["browser"]
        assert operated["metadata"]["pages"][0]["title"] == "distribution-browser-api"
        attach = client.get("/api/gateway/attach/browser/index.html").raise_for_status()
        assert 'src="./main.js"' in attach.text
        if os.environ.get("BOXTEAM_TEST_DISTRIBUTION_WORKBENCH") == "1":
            result_path = context.artifacts_dir / "workbench.json"
            temporary = context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp"
            electron_asset = os.environ.get("BOXTEAM_TEST_ELECTRON_ASSET")
            native: dict[str, str] = {}
            if electron_asset:
                destination = context.runtime_root_for_node(request.node.nodeid) / "electron"
                with tarfile.open(electron_asset) as archive:
                    archive.extractall(destination, filter="data")
                executable, = destination.glob("*/boxteam")
                native = {"electron": str(executable), "cwd": str(temporary),
                    "profile": str(temporary / "electron-home"), "emptyPath": str(temporary / "empty-path")}
                Path(native["emptyPath"]).mkdir(parents=True)
            environment = {**os.environ, "HOME": str(temporary),
                "XDG_CACHE_HOME": str(temporary / "cache"),
                "BOXTEAM_DISTRIBUTION_WORKBENCH": json.dumps({"baseUrl": str(client.base_url),
                    "application": str(distribution / manifest["application_root"]),
                    "chromium": str(distribution / manifest["chromium_executable"]),
                    "browserResource": {"resourceId": browser_resource["resource_id"], "workspaceId": workspace_id,
                        "gatewayId": gateway_id, "instanceId": browser_plugin["instance"]["instance_id"]},
                    "packages": [str(context.artifacts_dir / f"distribution-probe-{version}.zip")
                        for version in ("1.0.0", "2.0.0")],
                    "resultPath": str(result_path), "screenshot": str(context.artifacts_dir / "workbench.png"), **native})}
            command = [str(node), "tests/integration/packaging/service_plugin_distribution_workbench.mjs"]
            if electron_asset:
                command = ["/usr/bin/xvfb-run", "-a", *command]
            with chromium_temporary_environment(temporary) as browser_environment:
                browser = subprocess.run(command, env=environment | browser_environment,
                    capture_output=True, text=True, timeout=180, check=False)
            (context.artifacts_dir / "browser.stdout.raw").write_text(browser.stdout, encoding="utf-8")
            (context.artifacts_dir / "browser.stderr.raw").write_text(browser.stderr, encoding="utf-8")
            assert browser.returncode == 0, f"源码外 Workbench 插件闭环失败：{result_path}；{browser.stderr}"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            assert result["phase"] == "complete" and not result["pageErrors"] and not result["externalRequests"]
            assert result["browserAttachment"]["reconnected"] and result["browserAttachment"]["connections"] == 2
            assert all(not psutil.pid_exists(pid) for pid in result["exitedPids"])
            assert not (home / "plugins/packages/distribution.probe").exists()
            if electron_asset:
                assert len(result["native"]["launches"]) == len(result["native"]["exits"]) == 2
                assert not result["native"]["storageErrors"]
        latest_browser = exchange("GET", f"/api/v1/resources/{browser_resource['resource_id']}").raise_for_status().json()["data"]
        stopped_browser = exchange("POST", f"/api/v1/resources/{browser_resource['resource_id']}/stop", {
            "expected_revision": latest_browser["owner_revision"],
        }).raise_for_status().json()["data"]
        assert stopped_browser["status"] == "closed"
        bind([], workspace=True)
        require_exited({"browser": browser_plugin})
        uninstall("boxteam.developer-tools", "0.1.0")
        context.write_result("passed", {"target": target, "exchanges": len(evidence)})
    finally:
        (context.artifacts_dir / "lifecycle.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
