"""通过真实 Gateway 注册与重启核验工作区启动失败的公开诊断。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path

import httpx
import pytest

from app.gateway.runtime.workspace_process import resolve_backend_workspace_id
from app.gateway.workspace_ids import build_managed_local_workspace_id
from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)


@pytest.fixture
def denied_workspace_roots(integration_workspace_root_path: str):
    root = Path(integration_workspace_root_path)
    cases = []
    with ExitStack() as permissions:
        for scope in ("workspace", "metadata"):
            workspace = root / f"denied-{scope}"
            workspace.mkdir()
            backend_id = resolve_backend_workspace_id(Path.cwd(), workspace)
            denied = workspace if scope == "workspace" else workspace / ".boxteam"
            permissions.callback(denied.chmod, denied.stat().st_mode)
            denied.chmod(0o500)
            if os.access(denied, os.W_OK):
                pytest.skip("当前平台或用户不支持真实目录写权限拒绝")
            cases.append((scope, workspace, backend_id))
        yield cases


@pytest.mark.asyncio
async def test_managed_startup_access_failure_keeps_root_out_of_public_and_persisted_errors(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    denied_workspace_roots: list[tuple[str, Path, str]],
) -> None:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    primary = Path(integration_workspace_root_path)
    reset_gateway_persistent_state(workspace_root=primary)
    ports = integration_port_block_for_file(Path(request.node.fspath))
    evidence = {}

    async def inspect_public_errors(client: httpx.AsyncClient, stage: str) -> None:
        for check_health in (False, True):
            response = await client.get("/api/gateway/workspaces", params={"check_health": check_health})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["request_id"] == response.headers["X-Request-ID"]
            for scope, workspace, backend_id in denied_workspace_roots:
                assert str(workspace) not in response.text
                workspace_id = build_managed_local_workspace_id(str(workspace))
                item = next(item for item in body["data"]["items"] if item["workspace_id"] == workspace_id)
                assert item["backend_workspace_id"] == backend_id
                assert item["status"] == "offline"
                if stage == "registered":
                    expected = (
                        "工作区运行时目录不可写，无法保存会话和服务状态: "
                        if scope == "metadata" else "工作区目录不可写，无法创建 .boxteam 运行时数据: "
                    ) + "请将目录所有者或写权限授予当前用户"
                    assert item["connection_error"] == expected
                else:
                    assert item["connection_error"].startswith(
                        f"{workspace_id}/workspace_api: managed Backend 未运行: owner=backend_owner_",
                    )
                assert item["services"]["workspace_api"]["error"] == item["connection_error"]
                assert item["services"]["workspace_api"]["status"] == "offline"
                evidence[f"{stage}-{scope}-{check_health}"] = item
            evidence[f"{stage}-{check_health}"] = body

    with ExitStack() as processes:
        backend = start_backend_process(
            workspace_root=str(primary), port=ports.port(0), log_name="primary-backend",
        )
        processes.callback(close_backend_process, backend)

        def start_gateway():
            gateway = start_gateway_process(
                workspace_root=primary, default_backend_url=f"http://127.0.0.1:{backend.port}",
                port=ports.port(1), artifacts_dir=context.artifacts_dir,
            )
            processes.callback(close_gateway_process, gateway)
            return gateway

        gateway = start_gateway()
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=60,
        ) as client:
            await acquire_gateway_guest(client)
            for scope, workspace, _ in denied_workspace_roots:
                response = await client.post(
                    "/api/gateway/workspaces/local", json={"root_path": str(workspace), "name": scope},
                )
                assert response.status_code == 500, response.text
                assert str(workspace) not in response.text
                evidence[f"register-{scope}"] = {"status": response.status_code, "body": response.text}
            await inspect_public_errors(client, "registered")

        close_gateway_process(gateway)
        gateway = start_gateway()
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=60,
        ) as client:
            await acquire_gateway_guest(client)
            await inspect_public_errors(client, "restarted")

        state_path = context.boxteam_home / "state/gateway/gateway.sqlite"
        with closing(sqlite3.connect(f"{state_path.as_uri()}?mode=ro", uri=True)) as connection:
            for scope, workspace, backend_id in denied_workspace_roots:
                row, = connection.execute(
                    "SELECT payload_json FROM gateway_workspace_registry WHERE workspace_id=?",
                    (build_managed_local_workspace_id(str(workspace)),),
                ).fetchone()
                record = json.loads(row)
                assert record["root_path"] == str(workspace)
                assert record["backend_workspace_id"] == backend_id
                assert record["connection_error"] == evidence[f"registered-{scope}-False"]["connection_error"]
                assert str(workspace) not in record["connection_error"]
                evidence[f"record-{scope}"] = record
        logs = (context.artifacts_dir / "gateway.stderr.log").read_text()
        for _, workspace, _ in denied_workspace_roots:
            assert str(workspace) in logs, "真实位置只保留在 Gateway 自有诊断日志"
        (context.artifacts_dir / "managed-startup-access-errors.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["log_directory", "executable", "entrypoint"])
async def test_managed_process_failure_stays_explicit_without_persisting_native_location(
    request: pytest.FixtureRequest, integration_workspace_root_path: str, failure: str,
    tmp_path: Path,
) -> None:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    primary = Path(integration_workspace_root_path)
    reset_gateway_persistent_state(workspace_root=primary)
    ports = integration_port_block_for_file(Path(request.node.fspath))
    workspace = primary / f"managed-{failure}-failure"
    workspace.mkdir()
    workspace_id = build_managed_local_workspace_id(str(workspace))
    gateway_root = context.boxteam_home / "state/gateway"
    log_root = gateway_root / "logs"
    missing_executable = primary / "private-missing-node"
    native_location = str(log_root if failure == "log_directory" else missing_executable)
    extra_env = {"BOXTEAM_NODE_BIN": str(missing_executable)} if failure == "executable" else {}
    if failure == "entrypoint":
        # 模拟缺失辅助服务资产的安装目录；Python owner 使用本次完整源码，不改源目录。
        installation = tmp_path / "private-installation"
        for directory in ("app", "configs"):
            shutil.copytree(Path.cwd() / directory, installation / directory,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("pyproject.toml", "package.json"):
            shutil.copyfile(Path.cwd() / name, installation / name)
        extra_env["BOXTEAM_PROJECT_ROOT"] = str(installation)
        native_location = str(installation / "src/workspace-services/terminal/server/backend.js")
    evidence = {}
    with ExitStack() as processes:
        backend = start_backend_process(workspace_root=str(primary), port=ports.port(0), log_name="primary-backend")
        processes.callback(close_backend_process, backend)

        def start_gateway():
            gateway = start_gateway_process(
                workspace_root=primary, default_backend_url=f"http://127.0.0.1:{backend.port}",
                port=ports.port(1), artifacts_dir=context.artifacts_dir,
                extra_env=extra_env,
            )
            processes.callback(close_gateway_process, gateway)
            return gateway

        gateway = start_gateway()
        assert not log_root.exists()
        if failure == "log_directory":
            log_root.write_text("not a directory", encoding="utf-8")
        for stage in ("registered", "restarted"):
            if stage == "restarted":
                close_gateway_process(gateway)
                gateway = start_gateway()
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=60,
            ) as client:
                await acquire_gateway_guest(client)
                if stage == "registered":
                    response = await client.post("/api/gateway/workspaces/local", json={"root_path": str(workspace)})
                    assert response.status_code == 500, response.text
                    assert response.json()["request_id"] == response.headers["X-Request-ID"]
                    assert native_location not in response.text
                    evidence["registration"] = response.json()
                for check_health in (False, True):
                    response = await client.get("/api/gateway/workspaces", params={"check_health": check_health})
                    assert response.status_code == 200, response.text
                    body = response.json()
                    assert body["request_id"] == response.headers["X-Request-ID"]
                    assert native_location not in response.text
                    assert str(workspace) not in response.text
                    item = next(item for item in body["data"]["items"] if item["workspace_id"] == workspace_id)
                    assert item["status"] == "offline"
                    assert item["connection_error"]
                    assert item["services"]["workspace_api"]["status"] == "offline"
                    if stage == "registered" and not check_health:
                        expected_stage = {"log_directory": "create_directory", "executable": "spawn", "entrypoint": "entrypoint"}[failure]
                        expected_type = "FileExistsError" if failure == "log_directory" else "FileNotFoundError"
                        assert f"stage={expected_stage}" in item["connection_error"]
                        assert expected_type in item["connection_error"]
                    evidence[f"{stage}-{check_health}"] = item
            with closing(sqlite3.connect(f"{(gateway_root / 'gateway.sqlite').as_uri()}?mode=ro", uri=True)) as connection:
                row, = connection.execute(
                    "SELECT payload_json FROM gateway_workspace_registry WHERE workspace_id=?", (workspace_id,),
                ).fetchone()
            record = json.loads(row)
            assert record["root_path"] == str(workspace)
            assert record["connection_error"]
            assert native_location not in record["connection_error"]
            evidence[f"persisted-{stage}"] = record["connection_error"]
        logs = (context.artifacts_dir / "gateway.stderr.log").read_text()
        assert native_location in logs
        assert ("FileExistsError" if failure == "log_directory" else "FileNotFoundError") in logs
        if failure == "log_directory":
            assert log_root.read_text() == "not a directory"
        else:
            assert not missing_executable.exists()
        (context.artifacts_dir / f"managed-{failure}-errors.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
