"""不构建界面，核真实 Gateway 及其 Python／Node 服务的日志身份交付。"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from app.runtime.log_file_identity import runtime_log_path_identity
from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import resolve_workspace_python_executable


@pytest.fixture
def supported_configuration(
    request: pytest.FixtureRequest,
) -> tuple[TestRunContext, Path, dict[Path, bytes]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    home = context.boxteam_home_for_node(request.node.nodeid)
    config = home / "config"
    config.mkdir(parents=True, exist_ok=True)
    originals = {
        config / "workspace.jsonc": b'// original workspace settings\n{"config_version":1,"logger":{"level":"info"}}\n',
        config / "gateway.jsonc": (
            b'{"config_version":4,"ui":{"theme":{"default_theme_id":"warm","custom_themes":[]}},'
            b'"logging":{"runtime":{"level":"info"}}}\n'
        ),
    }
    for path, original in originals.items():
        path.write_bytes(original)
    return context, home, originals


def _initialize_configuration(context: TestRunContext, home: Path, workspace: Path, label: str) -> dict:
    environment = {
        **os.environ, "BOXTEAM_HOME": str(home),
        "BOXTEAM_GATEWAY_ROOT": str(home / "state/gateway"),
        "BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT": str(workspace),
        "PYTHONIOENCODING": "utf-8",
    }
    command = [str(resolve_workspace_python_executable(context.project_root)), "-m", "configs.boxteam",
               "initialize", "--project-root", str(context.project_root), "--workspace", str(workspace)]
    with (context.artifacts_dir / f"{label}.stdout.log").open("w", encoding="utf-8") as stdout, (
        context.artifacts_dir / f"{label}.stderr.log"
    ).open("w", encoding="utf-8") as stderr:
        completed = subprocess.run(
            command, cwd=context.project_root, env=environment, stdout=stdout, stderr=stderr,
            timeout=60, check=False,
        )
    (context.artifacts_dir / f"{label}.command.json").write_text(json.dumps({
        "command": command, "returncode": completed.returncode,
    }, indent=2) + "\n", encoding="utf-8")
    assert completed.returncode == 0, f"{label} 失败，原始输出: {context.artifacts_dir}"
    return json.loads((context.artifacts_dir / f"{label}.stdout.log").read_text(encoding="utf-8"))


def test_supported_configuration_upgrade_starts_and_restarts_original_workspace(
    supported_configuration: tuple[TestRunContext, Path, dict[Path, bytes]],
    integration_workspace_root_path: str,
) -> None:
    context, home, originals = supported_configuration
    workspace = Path(integration_workspace_root_path)
    result = _initialize_configuration(context, home, workspace, "upgrade")
    migration = result["upgrades"]["configuration"]
    assert migration["state"] == "completed"
    backups = {path: path.read_bytes() for path in Path(migration["backup_root"]).rglob("*") if path.is_file()}
    assert all(original in backups.values() for original in originals.values())
    gateway_config = json.loads((home / "config/gateway.jsonc").read_text(encoding="utf-8"))
    assert gateway_config["config_version"] == 5 and "ui" not in gateway_config
    assert json.loads((home / "config/workspace.jsonc").read_text(encoding="utf-8")) == {"config_version": 3}
    port = integration_port_block_for_file(context.test_file).port(0)
    owners = []
    for generation in range(2):
        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=None, port=port,
            boxteam_home=home, artifacts_dir=context.artifacts_dir / f"start-{generation}",
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", headers=LOCAL_TOKEN_HEADERS,
                # Gateway 列表沿产品合同等待受管 Workspace 恢复，期限为120秒。
                timeout=130, trust_env=False,
            ) as client:
                for endpoint in ("/api/gateway/health", "/api/gateway/workspaces", "/api/v1/workspace"):
                    request_id = f"upgrade-{generation}-{endpoint.rsplit('/', 1)[-1]}"
                    response = client.get(endpoint, headers={"X-Request-ID": request_id})
                    response.raise_for_status()
                    assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
                    if endpoint == "/api/gateway/workspaces":
                        target, = response.json()["data"]["items"]
                        assert target["status"] == "ready"
                        owners.append({key: target[key] for key in ("gateway_id", "workspace_id")})
                    elif endpoint == "/api/v1/workspace":
                        assert response.json()["data"]["workspace_id"] == owners[-1]["workspace_id"]
        finally:
            close_gateway_process(gateway)
        # SQLite 只读连接也可创建 WAL／共享内存；比较持久正文及备份，不把运行态侧文件当作改写。
        before = {path: path.read_bytes() for path in (home / "config").rglob("*")
                  if path.is_file() and not path.name.endswith(("-wal", "-shm"))}
        assert _initialize_configuration(context, home, workspace, f"reenter-{generation}")["upgrades"] == {}
        assert {path: path.read_bytes() for path in (home / "config").rglob("*")
                if path.is_file() and not path.name.endswith(("-wal", "-shm"))} == before
        assert {path: path.read_bytes() for path in Path(migration["backup_root"]).rglob("*") if path.is_file()} == backups
    assert owners[0] == owners[1]
    (context.artifacts_dir / "supported-upgrade.json").write_text(
        json.dumps({"migration": migration, "owners": owners}, indent=2) + "\n", encoding="utf-8",
    )


@pytest.fixture
def logging_gateway(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> Iterator[tuple[httpx.Client, TestRunContext]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    gateway = start_gateway_process(
        workspace_root=Path(integration_workspace_root_path), default_backend_url=None,
        port=integration_port_block_for_file(Path(request.node.path)).port(0),
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid),
        artifacts_dir=context.artifacts_dir,
    )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS,
            timeout=130, trust_env=False,
        ) as client:
            yield client, context
    finally:
        close_gateway_process(gateway)


def test_managed_backend_logging_control_starts_without_workbench(
    logging_gateway: tuple[httpx.Client, TestRunContext],
) -> None:
    client, context = logging_gateway
    response = client.get("/api/gateway/workspaces", headers={"X-Request-ID": "logging-startup"})
    response.raise_for_status()
    envelope = response.json()
    assert response.headers["X-Request-ID"] == envelope["request_id"] == "logging-startup"
    workspace, = envelope["data"]["items"]
    assert workspace["status"] == "ready"
    observed = {}
    try:
        base = workspace["services"]["workspace_api"]["local_url"].rstrip("/")
        reply = client.get(f"{base}/api/v1/internal/runtime-logging")
        observed["backend"] = {"status": reply.status_code, "body": reply.json()}
        assert reply.status_code == 200, observed["backend"]
        state = reply.json()["data"]
        assert state["service"] == "backend" and state["pid"] > 0
        sink = state["log_sink"]
        assert sink["role"] == "backend" and sink["source"] == "gateway"
        assert runtime_log_path_identity(Path(sink["path"])) == (sink["device"], sink["inode"])
        record = json.loads(Path(sink["path"] + ".owner.json").read_text(encoding="utf-8"))
        assert all(record[key] == value for key, value in sink.items())
        assert record["pid"] == state["pid"]
    finally:
        (context.artifacts_dir / "runtime-logging-startup.json").write_text(
            json.dumps(observed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
