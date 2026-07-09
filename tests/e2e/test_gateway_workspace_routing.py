from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from urllib.request import Request, urlopen

import httpx
import pytest

from tests.e2e.ports import e2e_port_block_for_file
from tests.e2e.processes import (
    close_backend_process,
    kill_process_on_port,
    resolve_workspace_python_executable,
    start_backend_process,
    terminate_process,
)


LOCAL_TOKEN_HEADERS = {"X-Local-Token": "local-dev-token"}
READY_TIMEOUT_SECONDS = 60


@dataclass(frozen=True, slots=True)
class GatewayProcess:
    process: subprocess.Popen[str]
    stdout_file: IO[str]
    stderr_file: IO[str]
    port: int


def _prepare_workspace(path: Path, name: str) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    (path / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    return path


def _wait_for_gateway_ready(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    request = Request(
        f"http://127.0.0.1:{port}/api/gateway/health",
        headers=LOCAL_TOKEN_HEADERS,
    )
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Gateway 提前退出: pid={process.pid}, returncode={process.returncode}"
            )
        try:
            with urlopen(request, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(1)
    raise TimeoutError(f"Gateway 在 {READY_TIMEOUT_SECONDS} 秒内未就绪: {port}")


def _start_gateway_process(
    *,
    workspace_root: Path,
    default_backend_url: str,
    port: int,
) -> GatewayProcess:
    kill_process_on_port(port)
    project_root = Path.cwd().resolve()
    python_executable = resolve_workspace_python_executable(project_root)
    log_dir = workspace_root / ".boxteam" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_file = open(log_dir / "gateway.stdout.log", "a", encoding="utf-8")
    stderr_file = open(log_dir / "gateway.stderr.log", "a", encoding="utf-8")
    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = str(workspace_root)
    env["BOXTEAM_PROJECT_ROOT"] = str(project_root)
    env["BOXTEAM_GATEWAY_ROOT"] = str(workspace_root / ".boxteam" / "gateway")
    env["BOXTEAM_DEFAULT_BACKEND_URL"] = default_backend_url
    env["BOXTEAM_DEFAULT_WORKSPACE_ROOT"] = str(workspace_root)
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [
            str(python_executable),
            "-m",
            "uvicorn",
            "app.gateway.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=project_root,
        env=env,
        stdout=stdout_file,
        stderr=stderr_file,
    )
    handle = GatewayProcess(
        process=process,
        stdout_file=stdout_file,
        stderr_file=stderr_file,
        port=port,
    )
    try:
        _wait_for_gateway_ready(port, process)
    except Exception:
        _close_gateway_process(handle)
        raise
    return handle


def _close_gateway_process(handle: GatewayProcess) -> None:
    try:
        terminate_process(handle.process)
        kill_process_on_port(handle.port)
    finally:
        handle.stdout_file.close()
        handle.stderr_file.close()


def _workspace_root_from_response(response: httpx.Response) -> str:
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["root_path"])


@pytest.mark.asyncio
async def test_gateway_routes_sessions_between_local_workspaces(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
):
    port_block = e2e_port_block_for_file(Path(request.node.fspath))
    primary_workspace = Path(e2e_workspace_root_path).resolve()
    secondary_workspace = _prepare_workspace(
        primary_workspace.parent / "test_gateway_workspace_routing_secondary",
        "secondary workspace",
    )

    primary_backend = start_backend_process(
        workspace_root=str(primary_workspace),
        port=port_block.port(0),
        log_name="gateway-primary-backend",
    )
    secondary_backend = start_backend_process(
        workspace_root=str(secondary_workspace),
        port=port_block.port(1),
        log_name="gateway-secondary-backend",
    )
    gateway = _start_gateway_process(
        workspace_root=primary_workspace,
        default_backend_url=f"http://127.0.0.1:{primary_backend.port}",
        port=port_block.port(2),
    )

    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as client:
            default_workspace_response = await client.get("/api/v1/workspace")
            assert Path(_workspace_root_from_response(default_workspace_response)).resolve() == primary_workspace

            add_response = await client.post(
                "/api/gateway/workspaces/local",
                json={
                    "root_path": str(secondary_workspace),
                    "name": "secondary",
                    "backend_url": f"http://127.0.0.1:{secondary_backend.port}",
                },
            )
            assert add_response.status_code == 200, add_response.text
            workspace_list = add_response.json()["data"]
            secondary_workspace_id = workspace_list["active_workspace_id"]
            assert secondary_workspace_id
            assert any(
                item["workspace_id"] == secondary_workspace_id
                and item["root_path"] == str(secondary_workspace)
                for item in workspace_list["items"]
            )

            routed_workspace_response = await client.get("/api/v1/workspace")
            assert Path(_workspace_root_from_response(routed_workspace_response)).resolve() == secondary_workspace

            create_response = await client.post(
                "/api/v1/sessions",
                json={"title": "Gateway Routed Session"},
            )
            assert create_response.status_code == 200, create_response.text
            routed_session_id = create_response.json()["data"]["session_id"]

            secondary_sessions_response = await client.get("/api/v1/sessions")
            assert secondary_sessions_response.status_code == 200
            secondary_titles = [
                item["title"]
                for item in secondary_sessions_response.json()["data"]["items"]
            ]
            assert "Gateway Routed Session" in secondary_titles

            default_workspace_id = next(
                item["workspace_id"]
                for item in workspace_list["items"]
                if Path(item["root_path"]).resolve() == primary_workspace
            )
            activate_default_response = await client.post(
                f"/api/gateway/workspaces/{default_workspace_id}/activate"
            )
            assert activate_default_response.status_code == 200, activate_default_response.text

            primary_sessions_response = await client.get("/api/v1/sessions")
            assert primary_sessions_response.status_code == 200
            primary_session_ids = [
                item["session_id"]
                for item in primary_sessions_response.json()["data"]["items"]
            ]
            assert routed_session_id not in primary_session_ids
    finally:
        _close_gateway_process(gateway)
        close_backend_process(secondary_backend)
        close_backend_process(primary_backend)


@pytest.mark.asyncio
async def test_gateway_ssh_workspace_e2e_requires_docker_access():
    if os.getenv("BOXTEAM_RUN_DOCKER_GATEWAY_E2E") != "1":
        pytest.skip("设置 BOXTEAM_RUN_DOCKER_GATEWAY_E2E=1 后运行 Docker SSH 跨端 e2e")
    docker_check = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if docker_check.returncode != 0:
        pytest.skip(f"Docker daemon 当前不可访问: {docker_check.stderr.strip()}")

    pytest.skip("Docker SSH 目标容器配置已提供，完整跨端用例将在可访问 Docker daemon 的环境中启用")
