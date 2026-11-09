"""通过真实 Launcher 验证 Gateway 换代接管和完整停机中的插件生命周期。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import commentjson
import httpx
import psutil

from tests.harness.python.run_context import TestRunContext
from tests.support.processes.scope import register_test_process, unregister_test_process

_PLUGIN_SOURCE = '''
import asyncio, os, sys
sys.path.insert(0, os.environ['BOXTEAM_PROJECT_ROOT'])
from fastapi import FastAPI
import uvicorn
from app.core.logging_config import configure_application_logging
from app.core.trace_middleware import TraceMiddleware
from app.runtime.plugin_commands import plugin_command_router

configure_application_logging(service='lifecycle-probe')
async def shutdown(): server.should_exit = True
app = FastAPI()
app.add_middleware(TraceMiddleware)
app.include_router(plugin_command_router({}, shutdown=shutdown))
server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=int(os.environ['BOXTEAM_PLUGIN_PORT']),
    log_config=None))
asyncio.run(server.serve())
'''


def _json_request(
    client: httpx.Client,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    response = client.request(method, path, json=payload)
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise TypeError(f"Gateway API 响应必须是对象: {path}")
    return value


def _wait_for(
    client: httpx.Client,
    path: str,
    predicate,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    process: subprocess.Popen[str] | None = None,
    timeout_seconds: float = 120,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last_value: dict[str, object] | None = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Launcher 提前退出: returncode={process.returncode}")
        try:
            value = _json_request(client, method, path, payload)
            last_value = value
            if predicate(value):
                return value
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise TimeoutError(f"Gateway 响应未在期限内满足条件: path={path}, last={last_value}")


def _package_archive(path: Path) -> None:
    import zipfile

    manifest = {
        "manifest_version": 1,
        "package_id": "launcher.lifecycle",
        "version": "1.0.0",
        "name": "Launcher lifecycle probe",
        "extension_contract_version": 1,
        "plugins": [{
            "id": "probe",
            "name": "Lifecycle probe",
            "instance_scope": "gateway",
            "requires": [{"extension_point": "services", "version": 1}],
            "contributes": {"services": [{
                "service_id": "probe",
                "service_type": "terminal",
                "protocol": "http",
            }]},
            "runtime": "python",
            "entrypoint": ["nested/runtime/plugin.py"],
            "management_protocol_version": 1,
        }],
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest))
        archive.writestr("nested/runtime/plugin.py", _PLUGIN_SOURCE)


def _install_and_enable(
    client: httpx.Client,
    *,
    gateway_id: str,
    archive: Path,
) -> dict[str, object]:
    from hashlib import sha256

    _json_request(client, "POST", "/api/gateway/service-plugins/install", {
        "gateway_id": gateway_id,
        "path": str(archive),
        "expected_digest": sha256(archive.read_bytes()).hexdigest(),
    })
    source_path = "/api/gateway/config/sources/gateway_mutable_override"
    document = _json_request(client, "GET", source_path)["data"]
    if not isinstance(document, dict):
        raise TypeError("Gateway 来源文档 data 无效")
    config = commentjson.loads(str(document["jsonc"]))
    config.setdefault("plugins", {})["runtime"] = {"entries": [{
        "package_id": "launcher.lifecycle",
        "plugin_id": "probe",
        "package_version": "1.0.0",
        "enabled": True,
    }]}
    saved = _json_request(client, "PUT", source_path, {
        "gateway_id": gateway_id,
        "source_key": "gateway_mutable_override",
        "jsonc": json.dumps(config, ensure_ascii=False, indent=2),
        "base_file_digest": document["file_digest"],
        "base_layer_revision": document["layer_revision"],
        "base_layer_digest": document["layer_digest"],
        "expected_active_revision": document["active_revision"],
        "expected_active_digest": document["active_digest"],
        "idempotency_key": "launcher-plugin-lifecycle-enable",
    })
    saved_data = saved["data"]
    assert isinstance(saved_data, dict)
    assert saved_data["edit"]["state"] == "active"
    return saved


def _wait_for_plugin(
    client: httpx.Client,
    gateway_id: str,
    process: subprocess.Popen[str],
) -> dict[str, object]:
    response = _wait_for(
        client,
        "/api/gateway/service-plugins/status",
        lambda value: (
            isinstance(value.get("data"), dict)
            and any(
                isinstance(item, dict)
                and item.get("binding", {}).get("plugin_id") == "probe"
                and item.get("state") == "ready"
                for item in value["data"].get("plugins", [])
            )
        ),
        method="POST",
        payload={"gateway_id": gateway_id},
        process=process,
        timeout_seconds=90,
    )
    data = response["data"]
    assert isinstance(data, dict)
    return next(item for item in data["plugins"] if item["binding"]["plugin_id"] == "probe")


def _same_process(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _stop_launcher(
    launcher: subprocess.Popen[str],
    *,
    node: str,
    project_root: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    stopper = subprocess.Popen(
        [node, "packages/launcher/bin/boxteam.mjs", "stop"],
        cwd=project_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # stop 等待 launcher PID 消失；由本测试持有的 Popen 同步回收 PID，避免
    # Linux zombie 在同一父进程内被误认为 Launcher 仍存活。
    launcher.wait(timeout=180)
    stdout, stderr = stopper.communicate(timeout=20)
    return subprocess.CompletedProcess(stopper.args, stopper.returncode, stdout, stderr)


def test_launcher_handoff_preserves_plugin_instance_and_full_stop_exits_it(
    request,
    unused_tcp_port: int,
    integration_workspace_root_path: str,
) -> None:
    project_root = Path.cwd().resolve()
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    home = context.boxteam_home_for_node(request.node.nodeid)
    home.mkdir(parents=True, exist_ok=True)
    artifacts = context.artifacts_dir
    artifacts.mkdir(parents=True, exist_ok=True)
    workspace = Path(integration_workspace_root_path).resolve()
    # 生命周期验收不调用模型，隔离配置不能依赖开发机的 Provider 凭据。
    config_path = workspace / ".boxteam/workspace.jsonc"
    workspace_config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    for provider in workspace_config["llm"]["providers"]:
        if "api_key" in provider:
            provider["api_key"] = "lifecycle-test-unused-key"
    config_path.write_text(json.dumps(workspace_config), encoding="utf-8")
    runtime_manifest = project_root / "out/development-runtime/runtime-manifest.json"
    python = project_root / ".venv/bin/python"
    node = shutil.which("node")
    bun = shutil.which("bun")
    if not runtime_manifest.is_file() or not python.is_file() or node is None or bun is None:
        raise RuntimeError("Launcher lifecycle 集成需要现有 source-development runtime、venv、Node 与 Bun")

    token = "launcher-plugin-lifecycle-token"
    token_path = home / "state/gateway/credentials/local-token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token + "\n", encoding="utf-8")
    token_path.chmod(0o600)
    archive = artifacts / "plugin.zip"
    _package_archive(archive)
    environment = {
        **os.environ,
        "BOXTEAM_HOME": str(home),
        "BOXTEAM_PROJECT_ROOT": str(project_root),
        "BOXTEAM_RUNTIME_MANIFEST": str(runtime_manifest),
        "BOXTEAM_PYTHON_BIN": str(python),
        "BOXTEAM_NODE_BIN": node,
        "BOXTEAM_GATEWAY_PORT": str(unused_tcp_port),
        "BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT": str(workspace),
        "WORKSPACE_ROOT": str(workspace),
        "BOXTEAM_DEVELOPMENT_RESTART_RUNNER": bun,
        "BOXTEAM_DEVELOPMENT_RESTART_SCRIPT": str(project_root / "scripts/launch/dev.mjs"),
        "BOXTEAM_DEVELOPMENT_RESTART_CWD": str(project_root),
    }
    stdout_path, stderr_path = artifacts / "launcher.stdout.log", artifacts / "launcher.stderr.log"
    launcher = None
    launcher_stdout = stdout_path.open("w", encoding="utf-8")
    launcher_stderr = stderr_path.open("w", encoding="utf-8")
    plugin_process: psutil.Process | None = None
    try:
        launcher = subprocess.Popen(
            [node, "packages/launcher/bin/boxteam.mjs", "start", "--no-open"],
            cwd=project_root,
            env=environment,
            stdout=launcher_stdout,
            stderr=launcher_stderr,
            text=True,
        )
        register_test_process(launcher, lambda: launcher.terminate())
        base_url = f"http://127.0.0.1:{unused_tcp_port}"
        with httpx.Client(base_url=base_url, headers={"X-Local-Token": token},
            timeout=10, trust_env=False) as client:
            health_path = "/api/gateway/health"
            initial = _wait_for(client, health_path,
                lambda value: value.get("code") == 0,
                process=launcher, timeout_seconds=90)
            initial_data = initial["data"]
            assert isinstance(initial_data, dict)
            initial_gateway_pid = initial_data["process_id"]
            gateway_id = initial_data["gateway_id"]
            _install_and_enable(client, gateway_id=gateway_id, archive=archive)
            before_handoff = _wait_for_plugin(client, gateway_id, launcher)
            original_instance = before_handoff["instance"]
            assert isinstance(original_instance, dict)
            plugin_pid_before = original_instance["process_id"]
            instance_id_before = original_instance["instance_id"]

            def logging_state() -> dict:
                diagnostics = _json_request(client, "GET", "/api/gateway/diagnostics")["data"]
                assert isinstance(diagnostics, dict)
                processes = diagnostics["processes"]
                process, = [item for item in processes if item["service"] == "launcher.lifecycle/probe"]
                assert process["error"] is None, process
                return process["state"]

            initial_logging = logging_state()
            assert initial_logging["service"] == "lifecycle-probe"
            assert initial_logging["pid"] == plugin_pid_before

            source_path = "/api/gateway/config/sources/gateway_mutable_override"
            document = _json_request(client, "GET", source_path)["data"]
            assert isinstance(document, dict)
            config = commentjson.loads(str(document["jsonc"]))
            config["runtime"]["gateway"]["process"]["health"]["poll_interval_seconds"] += 0.25
            config["logging"]["runtime"] = {"level": "warning", "pretty": False}
            pending_save = _json_request(client, "PUT", source_path, {
                "gateway_id": gateway_id,
                "source_key": "gateway_mutable_override",
                "jsonc": json.dumps(config, ensure_ascii=False, indent=2),
                "base_file_digest": document["file_digest"],
                "base_layer_revision": document["layer_revision"],
                "base_layer_digest": document["layer_digest"],
                "expected_active_revision": document["active_revision"],
                "expected_active_digest": document["active_digest"],
                "idempotency_key": "launcher-plugin-lifecycle-handoff",
            })
            pending_data = pending_save["data"]
            assert isinstance(pending_data, dict)
            assert pending_data["edit"]["state"] == "pending_restart"
            assert logging_state() == initial_logging, "pending 不得提前改变仍在使用的实例"
            pending = _wait_for(client, "/api/gateway/config/reload-status",
                lambda value: isinstance(value.get("data"), dict)
                and value["data"].get("state") == "pending_restart",
                process=launcher)
            pending_status = pending["data"]
            assert isinstance(pending_status, dict)
            candidate_ref = pending_status["candidate_ref"]
            assert isinstance(candidate_ref, str)

            restart = _json_request(client, "POST", "/api/gateway/runtime/restart-development")
            restart_data = restart["data"]
            assert isinstance(restart_data, dict)
            assert restart_data["previous_process_id"] == initial_gateway_pid
            next_health = _wait_for(client, health_path,
                lambda value: isinstance(value.get("data"), dict)
                and value["data"].get("process_id") != initial_gateway_pid,
                process=launcher)
            next_data = next_health["data"]
            assert isinstance(next_data, dict)
            assert next_data["gateway_id"] == gateway_id
            _wait_for(client, "/api/gateway/config/reload-status",
                lambda value: isinstance(value.get("data"), dict)
                and value["data"].get("state") == "active"
                and value["data"].get("candidate_ref") is None,
                process=launcher)
            plugin_after = _wait_for_plugin(client, gateway_id, launcher)
            instance_after = plugin_after["instance"]
            assert isinstance(instance_after, dict)
            assert instance_after["process_id"] == plugin_pid_before
            assert instance_after["instance_id"] == instance_id_before
            active_config = _json_request(client, "GET", source_path)["data"]
            assert isinstance(active_config, dict)
            adopted_logging = logging_state()
            assert adopted_logging == initial_logging | {
                "level": "warning", "pretty": False, "applied_revision": active_config["active_digest"],
            }
            plugin_pid = instance_after["process_id"]
            plugin_process = psutil.Process(plugin_pid)
            instance_id = instance_after["instance_id"]
            assert plugin_pid > 0 and instance_id

            stopped = _stop_launcher(launcher, node=node, project_root=project_root,
                environment=environment)
            assert stopped.returncode == 0, stopped.stderr
            plugin_process.wait(timeout=20)
            assert not _same_process(plugin_process)
            (artifacts / "lifecycle.json").write_text(json.dumps({
                "initial_gateway_pid": initial_gateway_pid,
                "replaced_gateway_pid": next_data["process_id"],
                "plugin_pid": plugin_pid,
                "plugin_instance_id": instance_id,
                "candidate_ref": candidate_ref,
                "launcher_stop_returncode": stopped.returncode,
                "plugin_exited": True,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        if launcher is not None:
            if launcher.poll() is None:
                try:
                    _stop_launcher(launcher, node=node, project_root=project_root,
                        environment=environment)
                finally:
                    if launcher.poll() is None:
                        launcher.terminate()
                        launcher.wait(timeout=10)
            unregister_test_process(launcher)
        launcher_stdout.close()
        launcher_stderr.close()
