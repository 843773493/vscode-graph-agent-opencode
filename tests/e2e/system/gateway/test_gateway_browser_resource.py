from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path

import commentjson
import httpx
import pytest
import websockets

from tests.e2e.system.gateway.gateway_docker import (
    docker_daemon_error,
    ensure_gateway_ssh_container,
)
from tests.e2e.system.gateway.gateway_target import (
    GatewaySshTarget,
    GatewayTargetE2EPaths,
    build_remote_pair_command,
    install_gateway_ssh_assets_for_e2e,
    start_remote_gateway_via_ssh,
    stop_remote_gateway,
)
from tests.harness.python.browser_manager import (
    BrowserFrontendProcess,
    browser_test_data_url,
    close_browser_frontend_process,
    start_browser_frontend_process,
)
from tests.harness.python.terminal_frontend import (
    TerminalFrontendProcess,
    close_terminal_frontend_process,
    start_terminal_frontend_process,
)
from tests.support.ports import e2e_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    GatewayProcess,
    acquire_gateway_guest,
    close_gateway_process,
    reset_gateway_persistent_state,
    reset_gateway_user_configuration,
    start_gateway_process,
    write_gateway_remote_gateway_config,
)
from tests.support.processes.scope import register_test_process, unregister_test_process
from tests.support.processes.workspace import (
    ensure_local_port_available,
    resolve_workspace_python_executable,
)


async def _receive_websocket_type(websocket, expected_type: str) -> dict[str, object]:
    deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < deadline:
        raw = await asyncio.wait_for(
            websocket.recv(),
            timeout=max(deadline - asyncio.get_running_loop().time(), 0.1),
        )
        # Browser 的二进制帧承载画面；控制事件只从文本 JSON 帧读取。
        if not isinstance(raw, str):
            continue
        message = json.loads(raw)
        if message.get("type") == expected_type:
            return message
    raise TimeoutError(f"未收到 WebSocket 消息: {expected_type}")


async def _receive_terminal_output(websocket, expected_text: str) -> None:
    deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < deadline:
        raw = await asyncio.wait_for(
            websocket.recv(),
            timeout=max(deadline - asyncio.get_running_loop().time(), 0.1),
        )
        message = json.loads(raw)
        if message.get("type") == "output" and expected_text in str(message.get("data")):
            return
    raise TimeoutError(f"终端 WebSocket 未输出: {expected_text}")


def _write_launcher_runtime_manifest(
    *, project_root: Path, output_root: Path, python_executable: Path
) -> Path:
    package = json.loads((project_root / "package.json").read_text(encoding="utf-8"))
    runtime_root = output_root / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    manifest_path = runtime_root / "runtime-manifest.json"
    manifest = {
        "schema_version": 1,
        "distribution": "source-development",
        "version": package["version"],
        "python_executable": str(python_executable),
        "application_root": str(project_root),
        "config_resources": {
            "gateway_inline": str(project_root / "configs" / "gateway_inline.jsonc"),
            "gateway_schema": str(project_root / "configs" / "gateway_schema.jsonc"),
            "workspace_inline": str(project_root / "configs" / "workspace_inline.jsonc"),
            "workspace_schema": str(project_root / "configs" / "workspace_schema.jsonc"),
        },
        "skill_resources": str(project_root / "resources" / "skills"),
        "web_assets": None,
        "chromium_executable": os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"),
        "node": {"source": "launcher", "executable": None},
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _gateway_response_data(response: httpx.Response) -> dict[str, object]:
    assert response.status_code == 200, response.text
    payload = response.json()
    if not isinstance(payload, dict):
        raise TypeError(f"Gateway 响应必须是对象: {payload!r}")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or response.headers.get("X-Request-ID") != request_id:
        raise AssertionError(f"Gateway request_id 头体不一致: payload={payload}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise TypeError(f"Gateway 响应 data 必须是对象: {payload!r}")
    return data


async def _wait_for_gateway_data(
    client: httpx.AsyncClient,
    path: str,
    launcher_process: subprocess.Popen[str],
    predicate: Callable[[dict[str, object]], bool],
    *,
    timeout_seconds: float = 60,
) -> dict[str, object]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_payload: object = None
    last_error: str | None = None
    while asyncio.get_running_loop().time() < deadline:
        if launcher_process.poll() is not None:
            raise RuntimeError(
                "Gateway Launcher 提前退出: "
                f"pid={launcher_process.pid}, returncode={launcher_process.returncode}"
            )
        try:
            response = await client.get(path)
        except httpx.RequestError as error:
            last_error = str(error)
        else:
            if response.status_code == 200:
                last_payload = _gateway_response_data(response)
                if predicate(last_payload):
                    return last_payload
                last_error = None
            else:
                last_error = f"HTTP {response.status_code}: {response.text}"
        await asyncio.sleep(0.5)
    raise TimeoutError(
        f"Gateway 响应在 {timeout_seconds} 秒内未满足条件: "
        f"path={path}, last_payload={last_payload}, last_error={last_error}"
    )


def _stop_owned_launcher_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"Gateway Launcher 未能优雅退出: pid={process.pid}"
        ) from error


@pytest.fixture(autouse=True)
def isolated_gateway_state(
    e2e_workspace_root_path: str,
) -> None:
    """每个测试函数都从干净的 Gateway 持久状态开始。

    Gateway 注册表把远程连接和投影分开落盘：scoped 保存会沿用注册表元数据里
    已有的 `remote_gateway_connections`，因此在已经写入过元数据的 BOXTEAM_HOME
    上新增配置声明的远程 Gateway 时，连接不会再次落盘，只剩投影。下一次
    Gateway 启动就会因为“投影引用未知连接”而按 fail-closed 拒绝加载。这里在
    每个测试函数开始前整体重建隔离 BOXTEAM_HOME，保证单个用例内写入的是自洽
    的完整快照；用例内部的多次重启仍然共享同一份持久化状态。
    """

    reset_gateway_persistent_state(workspace_root=Path(e2e_workspace_root_path))


@pytest.mark.asyncio
async def test_gateway_restart_reuses_local_browser_manager_and_live_page(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
) -> None:
    workspace_root = Path(e2e_workspace_root_path).resolve()
    output_root = workspace_root.parent
    project_root = Path.cwd().resolve()
    gateway_port = e2e_port_block_for_file(Path(request.node.fspath)).port(40)
    ensure_local_port_available(gateway_port, service_name="Gateway E2E Launcher")

    python_executable = resolve_workspace_python_executable(project_root)
    node_executable = shutil.which("node")
    bun_executable = shutil.which("bun")
    if node_executable is None or bun_executable is None:
        raise RuntimeError("Gateway Launcher E2E 需要 PATH 中存在 node 和 bun")
    launcher_entry = project_root / "packages" / "launcher" / "bin" / "boxteam.mjs"
    if not launcher_entry.is_file():
        raise FileNotFoundError(f"Launcher 入口不存在: {launcher_entry}")

    boxteam_home = output_root / "boxteam-home"
    credential_path = (
        boxteam_home / "state" / "gateway" / "credentials" / "local-token"
    )
    credential_path.parent.mkdir(parents=True, exist_ok=True)
    credential_path.write_text("local-dev-token\n", encoding="utf-8")
    credential_path.chmod(0o600)

    runtime_manifest = _write_launcher_runtime_manifest(
        project_root=project_root,
        output_root=output_root,
        python_executable=python_executable,
    )
    artifacts = output_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    stdout_file = (artifacts / "browser-manager-restart-launcher.stdout.log").open(
        "w", encoding="utf-8"
    )
    stderr_file = (artifacts / "browser-manager-restart-launcher.stderr.log").open(
        "w", encoding="utf-8"
    )
    environment = os.environ.copy()
    environment["BOXTEAM_HOME"] = str(boxteam_home)
    environment["BOXTEAM_PROJECT_ROOT"] = str(project_root)
    environment["BOXTEAM_RUNTIME_MANIFEST"] = str(runtime_manifest)
    environment["BOXTEAM_PYTHON_BIN"] = str(python_executable)
    environment["BOXTEAM_NODE_BIN"] = node_executable
    environment["BOXTEAM_GATEWAY_PORT"] = str(gateway_port)
    environment["BOXTEAM_GATEWAY_URL"] = f"http://127.0.0.1:{gateway_port}"
    environment["BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT"] = str(workspace_root)
    environment["WORKSPACE_ROOT"] = str(workspace_root)
    environment["BOXTEAM_DEVELOPMENT_RESTART_RUNNER"] = bun_executable
    environment["BOXTEAM_DEVELOPMENT_RESTART_SCRIPT"] = str(
        project_root / "scripts" / "launch" / "dev.mjs"
    )
    environment["BOXTEAM_DEVELOPMENT_RESTART_CWD"] = str(project_root)
    environment.pop("BOXTEAM_GATEWAY_ROOT", None)
    environment.pop("BOXTEAM_DEFAULT_BACKEND_URL", None)
    for variable in (
        "BOXTEAM_CONFIG_CANDIDATE_REF",
        "BOXTEAM_CONFIG_GENERATION",
        "BOXTEAM_CONFIG_FENCING_TOKEN",
    ):
        environment.pop(variable, None)

    launcher_process: subprocess.Popen[str] | None = None
    try:
        launcher_process = await asyncio.to_thread(
            subprocess.Popen,
            [node_executable, str(launcher_entry), "start", "--no-open"],
            cwd=project_root,
            env=environment,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
        )
        register_test_process(
            launcher_process,
            lambda: _stop_owned_launcher_process(launcher_process),
        )
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway_port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=15,
        ) as client:
            initial_health = await _wait_for_gateway_data(
                client,
                "/api/gateway/health",
                launcher_process,
                lambda _data: True,
            )
            initial_gateway_pid = initial_health.get("process_id")
            assert isinstance(initial_gateway_pid, int), initial_health
            assert initial_health.get("development_restart_available") is True
            await acquire_gateway_guest(client)

            initial_status = await _wait_for_gateway_data(
                client,
                "/api/gateway/config/reload-status",
                launcher_process,
                lambda data: (
                    data.get("state") == "active"
                    and data.get("candidate_ref") is None
                ),
            )
            initial_active_revision = initial_status.get("active_revision")
            assert isinstance(initial_active_revision, int), initial_status

            workspace_list = _gateway_response_data(
                await client.get("/api/gateway/workspaces")
            )
            workspace_id = workspace_list.get("active_workspace_id")
            assert isinstance(workspace_id, str), workspace_list
            health_path = (
                f"/api/gateway/workspaces/{workspace_id}/browser-manager/health"
            )
            first_health = await client.get(health_path)
            assert first_health.status_code == 200, first_health.text
            first_browser_manager_pid = first_health.json().get("process_id")
            assert isinstance(first_browser_manager_pid, int), first_health.text

            session_response = await client.post(
                "/api/v1/sessions",
                json={"title": "Gateway Browser Manager Restart E2E"},
            )
            session_data = _gateway_response_data(session_response)
            session_id = session_data.get("session_id")
            assert isinstance(session_id, str), session_data
            browser_url = browser_test_data_url()
            expected_page_title = "Gateway Docker Browser"
            browser_response = await client.post(
                "/api/v1/resources/browsers",
                headers={"X-BoxTeam-Workspace-Id": workspace_id},
                json={
                    "session_id": session_id,
                    "url": browser_url,
                },
            )
            assert browser_response.status_code == 200, browser_response.text
            created_browser = browser_response.json().get("data")
            if not isinstance(created_browser, dict):
                raise TypeError(f"Browser Manager 创建响应无效: {browser_response.text}")
            browser_id = created_browser.get("resource_id")
            page_id = created_browser["metadata"].get("page_id")
            assert isinstance(browser_id, str), created_browser
            assert isinstance(page_id, str), created_browser
            assert created_browser["metadata"].get("url") == browser_url, created_browser
            assert created_browser["metadata"].get("title") == expected_page_title, created_browser

            gateway_config_path = boxteam_home / "config" / "gateway.jsonc"
            gateway_config = commentjson.loads(
                gateway_config_path.read_text(encoding="utf-8")
            )
            gateway_health_config = gateway_config["runtime"]["gateway"]["process"][
                "health"
            ]
            old_poll_interval = gateway_health_config["poll_interval_seconds"]
            if not isinstance(old_poll_interval, (int, float)):
                raise TypeError(
                    "Gateway process health poll_interval_seconds 必须是数字: "
                    f"{old_poll_interval!r}"
                )
            gateway_health_config["poll_interval_seconds"] = old_poll_interval + 0.25
            gateway_config_path.write_text(
                json.dumps(gateway_config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            pending_status = await _wait_for_gateway_data(
                client,
                "/api/gateway/config/reload-status",
                launcher_process,
                lambda data: data.get("state") == "pending_restart",
                timeout_seconds=40,
            )
            candidate_id = pending_status.get("candidate_id")
            candidate_ref = pending_status.get("candidate_ref")
            pending_revision = pending_status.get("pending_revision")
            assert isinstance(candidate_id, str), pending_status
            assert isinstance(candidate_ref, str), pending_status
            assert isinstance(pending_revision, int), pending_status
            assert pending_status.get("restart_required") is True, pending_status

            gateway_events_path = "/api/gateway/config/events"
            candidate_events = await _wait_for_gateway_data(
                client,
                gateway_events_path,
                launcher_process,
                lambda data: any(
                    isinstance(event, dict)
                    and event.get("candidate_id") == candidate_id
                    for event in data.get("events", [])
                ),
            )
            event_cursor = candidate_events.get("cursor")
            assert isinstance(event_cursor, int), candidate_events

            restart_response = await client.post(
                "/api/gateway/runtime/restart-development"
            )
            restart_data = _gateway_response_data(restart_response)
            assert restart_data.get("previous_process_id") == initial_gateway_pid
            helper_process_id = restart_data.get("helper_process_id")
            assert isinstance(helper_process_id, int), restart_data

            restarted_health = await _wait_for_gateway_data(
                client,
                "/api/gateway/health",
                launcher_process,
                lambda data: data.get("process_id") != initial_gateway_pid,
                timeout_seconds=90,
            )
            restarted_gateway_pid = restarted_health.get("process_id")
            assert isinstance(restarted_gateway_pid, int), restarted_health
            assert restarted_health.get("active_workspace_id") == workspace_id

            final_status = await _wait_for_gateway_data(
                client,
                "/api/gateway/config/reload-status",
                launcher_process,
                lambda data: (
                    data.get("state") == "active"
                    and data.get("candidate_ref") is None
                    and isinstance(data.get("active_revision"), int)
                    and data["active_revision"] > initial_active_revision
                ),
                timeout_seconds=90,
            )
            final_active_revision = final_status["active_revision"]
            assert final_status.get("restart_required") is False, final_status

            applied_events = await _wait_for_gateway_data(
                client,
                f"{gateway_events_path}?after={event_cursor}",
                launcher_process,
                lambda data: any(
                    isinstance(event, dict)
                    and event.get("candidate_id") == candidate_id
                    and event.get("result") == "applied"
                    for event in data.get("events", [])
                ),
            )
            assert any(
                isinstance(event, dict)
                and event.get("candidate_id") == candidate_id
                and event.get("result") == "applied"
                for event in applied_events.get("events", [])
            )

            gateway_database = sqlite3.connect(
                boxteam_home / "state" / "gateway" / "gateway.sqlite"
            )
            try:
                restart_intent = gateway_database.execute(
                    "SELECT state, target_generation, health_proof_json, "
                    "candidate_ref FROM gateway_restart_intent "
                    "WHERE candidate_id = ?",
                    (candidate_id,),
                ).fetchone()
                assert restart_intent is not None
                assert restart_intent[0] == "active"
                target_generation = restart_intent[1]
                assert isinstance(target_generation, str)
                assert restart_intent[3] == candidate_ref
                proof_json = restart_intent[2]
                assert isinstance(proof_json, str)
                health_proof = json.loads(proof_json)
                assert health_proof["candidate_id"] == candidate_id
                assert health_proof["candidate_ref"] == candidate_ref
                assert health_proof["generation"] == target_generation
                assert health_proof["loaded_source"] == "pending"
                assert health_proof["active_revision"] == initial_active_revision
                assert health_proof["pending_revision"] == pending_revision
                assert isinstance(health_proof.get("gateway_id"), str)
                assert isinstance(health_proof.get("consumer_health_digests"), dict)
                assert health_proof["consumer_health_digests"]
                proof_digest = health_proof.get("health_digest")
                assert isinstance(proof_digest, str)
                proof_payload = {
                    key: value
                    for key, value in health_proof.items()
                    if key != "health_digest"
                }
                computed_proof_digest = hashlib.sha256(
                    json.dumps(
                        proof_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                assert proof_digest == computed_proof_digest

                active_snapshot = gateway_database.execute(
                    "SELECT active_revision, promoted_generation "
                    "FROM config_active_snapshot WHERE config_domain = 'gateway'"
                ).fetchone()
                assert active_snapshot == (final_active_revision, target_generation)
                pending_record = gateway_database.execute(
                    "SELECT state FROM config_pending_candidate "
                    "WHERE config_domain = 'gateway' AND candidate_id = ?",
                    (candidate_id,),
                ).fetchone()
                assert pending_record == ("active",)
                generation_record = gateway_database.execute(
                    "SELECT process_id, loaded_source, candidate_id, state, "
                    "listener_state, health_proof_json "
                    "FROM gateway_runtime_generation WHERE generation_id = ?",
                    (target_generation,),
                ).fetchone()
                assert generation_record is not None
                assert generation_record[:5] == (
                    restarted_gateway_pid,
                    "pending",
                    candidate_id,
                    "active",
                    "serving",
                )
                assert json.loads(generation_record[5]) == health_proof
            finally:
                gateway_database.close()

            restarted_browser_health = await client.get(health_path)
            assert restarted_browser_health.status_code == 200, (
                restarted_browser_health.text
            )
            assert (
                restarted_browser_health.json()["process_id"]
                == first_browser_manager_pid
            )
            restored_browser_response = await client.get(
                f"/api/v1/resources/{browser_id}",
                headers={"X-BoxTeam-Workspace-Id": workspace_id}
            )
            assert restored_browser_response.status_code == 200, (
                restored_browser_response.text
            )
            restored_browser = restored_browser_response.json()["data"]
            assert restored_browser["resource_id"] == browser_id
            assert restored_browser["metadata"]["page_id"] == page_id
            assert restored_browser["source_thread"]["session_id"] == session_id
            assert restored_browser["metadata"]["url"] == browser_url
            assert restored_browser["metadata"]["title"] == expected_page_title
            assert restored_browser["status"] == "running"
            detached = await client.post(
                f"/api/v1/resources/{browser_id}/detach", headers={"X-BoxTeam-Workspace-Id": workspace_id},
                json={"expected_revision": restored_browser["owner_revision"], "thread": restored_browser["source_thread"]},
            )
            assert detached.status_code == 200, detached.text
            delete_response = await client.post(
                f"/api/v1/resources/{browser_id}/delete", headers={"X-BoxTeam-Workspace-Id": workspace_id},
                json={"expected_revision": detached.json()["data"]["owner_revision"]},
            )
            assert delete_response.status_code == 200, delete_response.text
            deleted_browser = delete_response.json().get("data")
            assert isinstance(deleted_browser, dict), delete_response.text
            assert deleted_browser.get("status") == "deleted", deleted_browser
            assert deleted_browser.get("resource_id") == browser_id, deleted_browser
    finally:
        try:
            if launcher_process is not None:
                _stop_owned_launcher_process(launcher_process)
                unregister_test_process(launcher_process)
        finally:
            stdout_file.close()
            stderr_file.close()


@pytest.mark.asyncio
async def test_gateway_routes_remote_browser_and_terminal_services(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
    docker_e2e_paths: GatewayTargetE2EPaths,
):
    if os.getenv("BOXTEAM_RUN_DOCKER_GATEWAY_E2E") != "1":
        pytest.skip("设置 BOXTEAM_RUN_DOCKER_GATEWAY_E2E=1 后运行 Docker SSH 辅助服务 e2e")
    docker_error = docker_daemon_error()
    if docker_error is not None:
        pytest.skip(f"Docker daemon 当前不可访问: {docker_error}")

    port_block = e2e_port_block_for_file(Path(request.node.fspath))
    local_workspace = Path(e2e_workspace_root_path).resolve()
    gateway_port = port_block.port(41)
    remote_gateway_port = port_block.port(43)
    terminal_frontend_port = port_block.port(50)
    browser_frontend_port = port_block.port(51)
    tunnel_port_range = (port_block.port(60), port_block.port(75))
    remote_workspace_path = docker_e2e_paths.remote_workspace
    remote_boxteam_home = docker_e2e_paths.remote_boxteam_home

    gateway: GatewayProcess | None = None
    terminal_frontend: TerminalFrontendProcess | None = None
    browser_frontend: BrowserFrontendProcess | None = None
    remote_gateway_pid: str | None = None
    docker_target: GatewaySshTarget | None = None
    remote_gateway_declared = False

    try:
        docker_target = ensure_gateway_ssh_container(
            known_hosts_path=(
                local_workspace.parent / "artifacts" / "gateway_ssh_known_hosts"
            ),
        )
        remote_gateway_pid = start_remote_gateway_via_ssh(
            target=docker_target,
            remote_workspace_path=remote_workspace_path,
            remote_gateway_port=remote_gateway_port,
            remote_boxteam_home=remote_boxteam_home,
        )
        write_gateway_remote_gateway_config(
            workspace_root=local_workspace,
            ssh_port=docker_target.ssh_port,
            username=docker_target.username,
            remote_gateway_port=remote_gateway_port,
            private_key_path=install_gateway_ssh_assets_for_e2e(local_workspace),
            remote_pair_command=build_remote_pair_command(
                docker_target,
                remote_boxteam_home=remote_boxteam_home,
            ),
        )
        remote_gateway_declared = True
        terminal_frontend = start_terminal_frontend_process(
            workspace_root=local_workspace,
            artifacts_dir=local_workspace.parent / "artifacts",
            frontend_port=terminal_frontend_port,
        )
        browser_frontend = start_browser_frontend_process(
            workspace_root=local_workspace,
            artifacts_dir=local_workspace.parent / "artifacts",
            frontend_port=browser_frontend_port,
        )
        gateway_env = {
            "BOXTEAM_TERMINAL_FRONTEND_URL": f"http://127.0.0.1:{terminal_frontend_port}",
            "BOXTEAM_BROWSER_FRONTEND_URL": f"http://127.0.0.1:{browser_frontend_port}",
            "BOXTEAM_GATEWAY_SSH_KNOWN_HOSTS_FILE": str(
                docker_target.known_hosts_path
            ),
        }
        gateway = start_gateway_process(
            workspace_root=local_workspace,
            default_backend_url=None,
            port=gateway_port,
            ssh_tunnel_port_range=tunnel_port_range,
            extra_env=gateway_env,
        )

        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as client:
            await acquire_gateway_guest(client)
            workspace_list = (await client.get("/api/gateway/workspaces")).json()["data"]
            remote_workspace = next(
                item
                for item in workspace_list["items"]
                if item["connection_kind"] == "remote_gateway"
                and item["root_path"] == remote_workspace_path
            )
            assert remote_workspace["status"] == "ready"
            assert remote_workspace["services"]["terminal_manager"]["status"] == "ready"
            assert remote_workspace["services"]["browser_manager"]["status"] == "ready"
            workspace_id = remote_workspace["workspace_id"]

            session_response = await client.post(
                "/api/v1/sessions",
                headers={"X-BoxTeam-Workspace-Id": workspace_id},
                json={"title": "Docker Remote Auxiliary Services"},
            )
            assert session_response.status_code == 200, session_response.text
            session_id = session_response.json()["data"]["session_id"]

            browser_response = await client.post(
                "/api/v1/resources/browsers",
                headers={"X-BoxTeam-Workspace-Id": workspace_id},
                json={
                    "session_id": session_id,
                    "title": "Remote Docker Browser",
                    "url": browser_test_data_url(),
                },
            )
            assert browser_response.status_code == 200, browser_response.text
            browser_resource = browser_response.json()["data"]
            browser_id = browser_resource["resource_id"]

            terminal_response = await client.post(
                "/api/v1/resources/terminals",
                headers={"X-BoxTeam-Workspace-Id": workspace_id},
                json={
                    "session_id": session_id,
                    "retention_scope": "thread",
                    "title": "Remote Docker Terminal",
                    "cwd_relative": ".",
                    "cols": 100,
                    "rows": 30,
                },
            )
            assert terminal_response.status_code == 200, terminal_response.text
            terminal_id = terminal_response.json()["data"]["resource_id"]

            resources_response = await client.get(
                f"/api/v1/sessions/{session_id}/resources",
                headers={"X-BoxTeam-Workspace-Id": workspace_id},
            )
            assert resources_response.status_code == 200, resources_response.text
            resources = resources_response.json()["data"]["items"]
            routed_resources = {
                (item["kind"], item["resource_id"]): item
                for item in resources
            }
            assert ("browser", browser_id) in routed_resources
            assert ("terminal", terminal_id) in routed_resources
            assert "attach_url" not in routed_resources[("browser", browser_id)]["metadata"]
            assert "attach_url" not in routed_resources[("terminal", terminal_id)]["metadata"]

            browser_attach = await client.get(
                "/api/gateway/attach/browser/",
                params={"workspaceId": workspace_id, "browserId": browser_id},
            )
            terminal_attach = await client.get(
                "/api/gateway/attach/terminal/",
                params={"workspaceId": workspace_id, "terminalId": terminal_id},
            )
            assert browser_attach.status_code == 200
            assert "可附加浏览器" in browser_attach.text
            assert terminal_attach.status_code == 200
            assert "持久终端" in terminal_attach.text

            browser_grant = await client.post(
                f"/api/v1/resources/{browser_id}/transport-grant", headers={"X-BoxTeam-Workspace-Id": workspace_id},
                json={"expected_revision": browser_resource["owner_revision"], "thread": browser_resource["source_thread"]},
            )
            browser_grant.raise_for_status()
            browser_ws_url = (
                f"ws://127.0.0.1:{gateway.port}/api/gateway/workspaces/"
                f"{workspace_id}/browser-manager/browser?token=local-dev-token"
            )
            async with websockets.connect(browser_ws_url) as websocket:
                await websocket.send(
                    json.dumps({"type": "attach", "browserId": browser_id, "grantToken": browser_grant.json()["data"]["grant_token"]})
                )
                attached = await _receive_websocket_type(websocket, "attached")
                assert attached["browserId"] == browser_id
                await websocket.send(
                    json.dumps({"type": "detach", "browserId": browser_id})
                )
                await _receive_websocket_type(websocket, "detached")

            terminal_resource = terminal_response.json()["data"]
            grant_response = await client.post(
                f"/api/v1/resources/{terminal_id}/transport-grant",
                headers={"X-BoxTeam-Workspace-Id": workspace_id},
                json={"expected_revision": terminal_resource["owner_revision"], "thread": terminal_resource["source_thread"]},
            )
            grant_response.raise_for_status()
            grant_token = grant_response.json()["data"]["grant_token"]
            terminal_ws_url = (
                f"ws://127.0.0.1:{gateway.port}/api/gateway/workspaces/"
                f"{workspace_id}/terminal-manager/terminal?token=local-dev-token"
            )
            async with websockets.connect(terminal_ws_url) as websocket:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "attach",
                            "terminalId": terminal_id,
                            "grantToken": grant_token,
                            "cols": 100,
                            "rows": 30,
                        }
                    )
                )
                await _receive_websocket_type(websocket, "attached")
                await websocket.send(
                    json.dumps(
                        {
                            "type": "input",
                            "data": "printf 'GATEWAY_TERMINAL_OK\\n'\r",
                        }
                    )
                )
                await _receive_terminal_output(websocket, "GATEWAY_TERMINAL_OK")

        close_gateway_process(gateway)
        gateway = None
        # 这里刻意保留用户配置里的远程 Gateway 声明，用于验证重启后从注册表恢复
        # 远程投影；声明的清理统一放在 finally 中，避免跨运行残留。
        gateway = start_gateway_process(
            workspace_root=local_workspace,
            default_backend_url=None,
            port=gateway_port,
            ssh_tunnel_port_range=tunnel_port_range,
            extra_env=gateway_env,
        )
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as restarted_client:
            await acquire_gateway_guest(restarted_client)
            restored_response = await restarted_client.get(
                f"/api/v1/resources/{browser_id}",
                headers={"X-BoxTeam-Workspace-Id": workspace_id}
            )
            assert restored_response.status_code == 200, restored_response.text
            assert restored_response.json()["data"]["resource_id"] == browser_id
    finally:
        if gateway is not None:
            close_gateway_process(gateway)
        if remote_gateway_declared:
            reset_gateway_user_configuration(workspace_root=local_workspace)
        if remote_gateway_pid is not None and docker_target is not None:
            stop_remote_gateway(docker_target, remote_gateway_pid)
        if browser_frontend is not None:
            close_browser_frontend_process(browser_frontend)
        if terminal_frontend is not None:
            close_terminal_frontend_process(terminal_frontend)
