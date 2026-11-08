"""按正式插件管理合同启动 Terminal 服务的测试装具。"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.core.workspace_identity import load_or_create_workspace_id
from tests.harness.python.process_output import spawn_logged_process
from tests.support.processes.workspace import (
    ensure_local_port_available,
    terminate_process,
)

PACKAGE_ID = "boxteam.developer-tools"
PACKAGE_VERSION = "0.1.0"
PLUGIN_ID = "terminal"


@dataclass(slots=True)
class TerminalPluginService:
    process: subprocess.Popen[str]
    root_url: str
    http_url: str
    token: str
    gateway_id: str
    workspace_id: str
    instance_id: str

    @property
    def websocket_url(self) -> str:
        return f"{self.root_url.replace('http://', 'ws://', 1)}/services/socket/terminal"

    @property
    def headers(self) -> dict[str, str]:
        return {
            "X-Local-Token": self.token,
            "X-BoxTeam-Workspace-Id": self.workspace_id,
            "X-BoxTeam-Resource-Owner": "workspace-terminal",
        }


def start_terminal_plugin_service(
    *,
    backend_port: int,
    workspace_root: str,
    artifacts_dir: Path,
) -> TerminalPluginService:
    """以 Gateway 提供的插件环境、身份和服务清单启动真实 Node 包入口。"""

    ensure_local_port_available(backend_port, service_name="Terminal 插件测试服务")
    project_root = Path.cwd().resolve()
    node_bin = shutil.which("node")
    if node_bin is None:
        raise RuntimeError("未找到 node，无法启动终端插件进程")

    workspace = Path(workspace_root).resolve(strict=True)
    workspace_id = load_or_create_workspace_id(workspace)
    gateway_id = f"test-gateway-{secrets.token_hex(8)}"
    instance_id = secrets.token_hex(16)
    token = secrets.token_urlsafe(32)
    identity = {
        "gateway_id": gateway_id,
        "package_id": PACKAGE_ID,
        "package_version": PACKAGE_VERSION,
        "plugin_id": PLUGIN_ID,
        "workspace_id": workspace_id,
        "instance_scope": "workspace",
        "instance_id": instance_id,
        "management_protocol_version": 1,
        "extension_contract_version": 1,
        "services": [
            {"service_id": "http", "service_type": "terminal", "protocol": "http"},
            {"service_id": "socket", "service_type": "terminal", "protocol": "websocket"},
        ],
    }
    env = {
        **os.environ,
        "BOXTEAM_PROJECT_ROOT": str(project_root),
        "WORKSPACE_ROOT": str(workspace),
        "BOXTEAM_WORKSPACE_ID": workspace_id,
        "BOXTEAM_RUNTIME_CONTROL_TOKEN": token,
        "BOXTEAM_PLUGIN_PORT": str(backend_port),
        "BOXTEAM_PLUGIN_INSTANCE": json.dumps(identity),
    }
    process, logs = spawn_logged_process(
        [node_bin, "backend.js"],
        cwd=project_root / "src/workspace-services/terminal/server",
        env=env,
        artifacts_dir=artifacts_dir,
        log_name="terminal-plugin",
    )
    root_url = f"http://127.0.0.1:{backend_port}"
    http_url = f"{root_url}/services/http"
    try:
        deadline = time.monotonic() + 30
        with httpx.Client(timeout=1, trust_env=False) as client:
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"Terminal 插件在 ready 前退出: exit_code={process.returncode}")
                try:
                    response = client.get(
                        f"{root_url}/internal/plugin/status",
                        headers={"X-Local-Token": token},
                    )
                    response.raise_for_status()
                    receipt = response.json()
                    break
                except httpx.ConnectError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Terminal 插件未在期限内 ready") from None
                    time.sleep(0.025)
        expected = {
            **identity,
            "status": "ready",
            "capabilities": ["status", "shutdown"],
        }
        for field, value in expected.items():
            if receipt.get(field) != value:
                raise RuntimeError(
                    f"Terminal 插件 ready 身份不匹配: field={field}, expected={value!r}, actual={receipt.get(field)!r}"
                )
        return TerminalPluginService(process, root_url, http_url, token, gateway_id, workspace_id, instance_id)
    except BaseException as startup_error:
        try:
            terminate_process(process)
        except BaseException as cleanup_error:  # noqa: BLE001 — 保留启动与清理两个原始失败。
            raise BaseExceptionGroup("Terminal 插件启动与进程清理均失败", [startup_error, cleanup_error]) from None
        for path in logs:
            startup_error.add_note(f"Terminal 插件日志：{path}")
        raise


def stop_terminal_plugin_service(service: TerminalPluginService) -> None:
    """经插件管理合同请求关闭，等待同一 owner 的真实 Node 进程退出。"""

    process = service.process
    if process.poll() is not None:
        return
    with httpx.Client(timeout=10, trust_env=False) as client:
        response = client.post(
            f"{service.root_url}/internal/plugin/shutdown",
            headers={"X-Local-Token": service.token},
            json={},
        )
        response.raise_for_status()
        receipt = response.json()
    if receipt != {"accepted": True, "process_id": process.pid, "instance_id": service.instance_id}:
        raise RuntimeError(f"Terminal 插件 shutdown 回执与持有进程不匹配: {receipt}")
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"Terminal 插件收到 shutdown 后未退出: pid={process.pid}") from None
