"""受支持旧产品经真实 CLI 迁移，真实重启仍读取原会话、视图与历史 hash。"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import tarfile
from contextlib import ExitStack, closing, suppress
from dataclasses import dataclass
from pathlib import Path

import httpx
import psutil
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.integration.gateway.workspace_identity_legacy_input import (
    business_snapshot,
    data,
)
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    resolve_workspace_python_executable,
    start_backend_process,
    terminate_process,
)

LEGACY_COMMIT = "bf0f2e1d9d037efcb0210c458fa157fb4d54a27a"


@dataclass(frozen=True)
class LegacyInput:
    context: TestRunContext
    workspace: Path
    home: Path
    runtime: Path
    artifacts: Path
    before: dict
    backend_port: int
    gateway_port: int


def _command(command: list[str], *, cwd: Path, artifacts: Path, label: str,
             environment: dict[str, str] | None = None, deadline: int = 240) -> None:
    """保留真实命令、原始输出与退出码；子进程继承测试外部的内存上限。"""
    with (artifacts / f"{label}.stdout.log").open("wb") as stdout, (artifacts / f"{label}.stderr.log").open("wb") as stderr:
        process = subprocess.Popen(command, cwd=cwd, env=environment, stdout=stdout, stderr=stderr, text=True)
        completed = False
        try:
            returncode = process.wait(timeout=deadline)
            completed = True
        finally:
            if process.poll() is None:
                # 中断等待时先记住整棵子进程树，不能只杀驱动而遗留旧服务。
                children: list[psutil.Process] = []
                with suppress(psutil.NoSuchProcess):
                    children = psutil.Process(process.pid).children(recursive=True)
                for child in reversed(children):
                    with suppress(psutil.NoSuchProcess):
                        child.terminate()
                terminate_process(process)
                _, remaining = psutil.wait_procs(children, timeout=10)
                for child in remaining:
                    with suppress(psutil.NoSuchProcess):
                        child.kill()
                psutil.wait_procs(remaining, timeout=10)
            (artifacts / f"{label}.command.json").write_text(json.dumps({
                "command": command, "cwd": str(cwd), "returncode": process.returncode, "completed": completed,
            }, indent=2) + "\n", encoding="utf-8")
    assert returncode == 0, f"{label} 失败，exit={returncode}；原件: {artifacts}"


@pytest.fixture
def legacy_input(request: pytest.FixtureRequest) -> LegacyInput:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime = context.runtime_root_for_node(request.node.nodeid)
    artifacts = context.artifacts_dir
    runtime.mkdir(parents=True, exist_ok=True)
    workspace = context.workspace_root / context.run_root.name
    home = context.boxteam_home_for_node(request.node.nodeid)
    source = runtime / "legacy-source"
    archive = runtime / "legacy-source.tar"
    _command(["git", "archive", "--format=tar", f"--output={archive}", LEGACY_COMMIT],
             cwd=context.project_root, artifacts=artifacts, label="archive", deadline=120)
    source.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(source, filter="data")
    assert (source / "uv.lock").read_bytes() == (context.project_root / "uv.lock").read_bytes(), "旧源码依赖必须与复用的项目环境完全一致"
    (source / ".venv").symlink_to(context.project_root / ".venv", target_is_directory=True)
    ports = integration_port_block_for_file(Path(request.node.path))
    script = context.project_root / "tests/integration/gateway/workspace_identity_legacy_input.py"
    command = [str(resolve_workspace_python_executable(context.project_root)), "-c",
               "import runpy, sys; runpy.run_path(sys.argv.pop(1), run_name='__main__')", str(script),
               "--workspace", str(workspace), "--home", str(home), "--artifacts", str(artifacts / "legacy"),
               "--runtime", str(runtime / "legacy-control"), "--backend-port", str(ports.port(0)),
               "--gateway-port", str(ports.port(1)), "--model-port", str(ports.port(80))]
    _command(command, cwd=source, artifacts=artifacts, label="generate-legacy")
    before = json.loads((artifacts / "legacy/legacy-input.json").read_text(encoding="utf-8"))
    (artifacts / "source.json").write_text(json.dumps({
        "legacy_commit": LEGACY_COMMIT, "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "legacy_source": str(source), "generator_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
    }, indent=2) + "\n", encoding="utf-8")
    return LegacyInput(context, workspace, home, runtime, artifacts, before, ports.port(0), ports.port(1))


def _dump(database: Path) -> str:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        return "\n".join(connection.iterdump())


def _migrate(case: LegacyInput, label: str) -> dict:
    environment = {**os.environ, "BOXTEAM_HOME": str(case.home), "BOXTEAM_GATEWAY_ROOT": str(case.home / "state/gateway")}
    command = [str(resolve_workspace_python_executable(case.context.project_root)), "-m", "configs.boxteam",
               "migrate-workspace-identity", "--project-root", str(case.context.project_root),
               "--recovery-output", str(case.artifacts / "client-recovery.json")]
    _command(command, cwd=case.context.project_root, artifacts=case.artifacts, label=label, environment=environment, deadline=60)
    result = json.loads((case.artifacts / f"{label}.stdout.log").read_text(encoding="utf-8"))
    assert result["state"] == "completed"
    return result


def _assert_running_history(case: LegacyInput, label: str) -> None:
    before = case.before
    owner = {"gateway_id": before["gateway_id"], "workspace_id": before["workspace_id"]}
    session_id, thread_id = before["session"]["session_id"], before["thread"]["thread_id"]
    artifacts = case.artifacts / label
    artifacts.mkdir()
    with ExitStack() as lifetime:
        backend = start_backend_process(
            workspace_root=str(case.workspace), port=case.backend_port, log_name="current-backend",
            boxteam_home=case.home, artifacts_dir=artifacts, refresh_user_configuration=False,
            env_overrides={"BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{case.gateway_port}"},
        )
        lifetime.callback(close_backend_process, backend)
        gateway = start_gateway_process(
            workspace_root=case.workspace, default_backend_url=f"http://127.0.0.1:{case.backend_port}",
            port=case.gateway_port, boxteam_home=case.home, artifacts_dir=artifacts,
        )
        lifetime.callback(close_gateway_process, gateway)
        client = lifetime.enter_context(httpx.Client(
            base_url=f"http://127.0.0.1:{case.gateway_port}", headers={**LOCAL_TOKEN_HEADERS,
                "X-BoxTeam-Target-Gateway-Id": owner["gateway_id"], "X-BoxTeam-Workspace-Id": owner["workspace_id"]}, timeout=30,
        ))
        assert backend.workspace_id == before["workspace_id"]
        workspaces = data(client.get("/api/gateway/workspaces"))
        assert workspaces["active_workspace"] == owner
        target, = workspaces["items"]
        assert (target["gateway_id"], target["workspace_id"]) == (owner["gateway_id"], owner["workspace_id"])
        data(client.post(f"/api/gateway/users/{before['user']['user_id']}/access", json={"client_label": label}))
        workspace = data(client.get("/api/v1/workspace"))
        assert workspace["workspace_id"] == owner["workspace_id"]
        assert data(client.get(f"/api/v1/sessions/{session_id}")) == before["session"]
        assert data(client.get(f"/api/v1/sessions/{session_id}/main-thread")) == before["thread"]
        history = data(client.post(f"/api/v1/sessions/{session_id}/history", json={"direction": "tail"}))
        assert history["items"] == before["history"]["items"]
        assert history["thread_id"] == thread_id
        messages = data(client.get(f"/api/v1/sessions/{session_id}/messages"))
        assert messages["items"] == before["messages"]["items"]
        view = data(client.get("/api/gateway/users/current/view-state", params={**owner, "session_id": session_id}))
        assert view == {**before["view"], **owner}
        navigation = data(client.get("/api/gateway/workspace-navigation"))
        layout = lambda tree: [(node["node_id"], node["kind"], node["parent_node_id"], node["name"], node["position"])
                               for node in tree["nodes"]]
        assert layout(navigation) == layout(before["navigation"])
        reference, = [node for node in navigation["nodes"] if node["kind"] == "workspace_ref"]
        assert reference["workspace"] == owner
        business = business_snapshot(case.workspace, owner["workspace_id"], session_id, thread_id, before["captures"])
        assert business == before["business"]
        assert (case.workspace / ".boxteam/workspace-identity.json").read_text(encoding="utf-8") == before["identity_bytes"]
        data(client.delete("/api/gateway/users/current"))
        (artifacts / "observed.json").write_text(json.dumps({
            "workspaces": workspaces, "view": view, "navigation": navigation, "history": history,
            "business": business, "pids": {"gateway": gateway.process.pid, "backend": backend.process.pid},
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert backend.process.returncode in (0, -signal.SIGTERM)
    assert gateway.process.returncode in (0, -signal.SIGTERM)


def test_supported_legacy_cli_migration_restarts_with_original_history_and_view(legacy_input: LegacyInput) -> None:
    case = legacy_input
    database = case.home / "state/gateway/gateway.sqlite"
    original = _dump(database)
    first = _migrate(case, "migrate")
    recovery = json.loads((case.artifacts / "client-recovery.json").read_text(encoding="utf-8"))
    assert recovery == {"format_version": 1, "migration_id": first["migration_id"],
                        "registry_gateway_id": case.before["gateway_id"], "mappings": [{
                            "legacy_workspace_id": case.before["legacy_alias"], "gateway_id": case.before["gateway_id"],
                            "workspace_id": case.before["workspace_id"],
                        }]}
    assert _dump(Path(first["backup_root"]) / "gateway.before.sqlite") == original
    migrated = _dump(database)
    assert _migrate(case, "reenter-before-start") == first
    assert _dump(database) == migrated
    _assert_running_history(case, "first-start")
    after_start = _dump(database)
    assert _migrate(case, "reenter-after-stop") == first
    assert _dump(database) == after_start
    _assert_running_history(case, "second-start")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        target = json.loads(connection.execute("SELECT payload_json FROM gateway_workspace_registry").fetchone()[0])
    assert target["owner"] == case.before["target_record"]["owner"]
