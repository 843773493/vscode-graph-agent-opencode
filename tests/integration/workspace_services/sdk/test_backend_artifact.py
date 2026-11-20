"""正式 application 复制清单与锁定依赖组成的 Backend 制品初始化。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import commentjson
import httpx
import pytest

from app.core.mounted_workspaces import MountedWorkspace
from configs.installer import install_user_configuration
from tests.harness.python.run_context import TestRunContext
from tests.support.api_helpers import create_session
from tests.support.processes.workspace import terminate_process
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)


@pytest.fixture
def backend_artifact(request: pytest.FixtureRequest) -> tuple[Path, Path, Path, TestRunContext]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    root = context.run_root / "runtime"
    application, dependencies = root / "application", root / "python/site-packages"
    application.mkdir(parents=True)
    records = []

    def run(label: str, command: list[str]) -> None:
        result = subprocess.run(command, cwd=Path.cwd(), capture_output=True, text=True,
            timeout=180, check=False)
        (context.artifacts_dir / f"{label}.stdout.log").write_text(result.stdout)
        (context.artifacts_dir / f"{label}.stderr.log").write_text(result.stderr)
        records.append({"command": command, "exit_code": result.returncode})
        (context.artifacts_dir / "build-commands.json").write_text(json.dumps(records, indent=2))
        assert result.returncode == 0, result.stderr

    run("application", ["bun", "--eval", ("import {copyApplicationSources} from './packaging/runtime/application-sources.mjs'; "
        f"copyApplicationSources(process.cwd(), {json.dumps(str(application))});")])
    requirements = root / "requirements.txt"
    run("export", ["uv", "export", "--locked", "--no-dev", "--no-emit-project", "--format", "requirements-txt",
        "--output-file", str(requirements)])
    run("dependencies", ["uv", "pip", "install", "--python", sys.executable, "--target", str(dependencies),
        "--offline", "--no-deps", "--require-hashes", "--requirements", str(requirements)])
    workspace = prepare_test_workspace(workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", template_items=())
    config_path = install_test_workspace_config(workspace_root=workspace,
        config_path=Path.cwd() / "configs/tests/workspace/default.jsonc",
        schema_path=Path.cwd() / "configs/workspace_schema.jsonc")
    config = commentjson.loads(config_path.read_text())
    config.setdefault("plugins", {})["runtime"] = {"entries": []}
    config_path.write_text(json.dumps(config))
    home = context.boxteam_home_for_node(request.node.nodeid)
    install_user_configuration(config_root=home / "config", profile="default", project_root=application)
    return application, dependencies, home, context


async def test_complete_backend_application_initializes_with_installed_sdk(
    backend_artifact, unused_tcp_port: int, runtime_manifest_path: Path,
) -> None:
    application, dependencies, home, context = backend_artifact
    workspace_id = str(uuid4())
    # -S 只允许正式 application 与随制品安装的依赖，不能回到根源码或开发 .venv。
    environment = {key: value for key, value in os.environ.items()
        if not key.startswith(("BOXTEAM_", "PYTHON", "UV_", "PIP_"))}
    environment.update({"PYTHONPATH": os.pathsep.join((str(application), str(dependencies))),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "BOXTEAM_PROJECT_ROOT": str(application),
        "BOXTEAM_HOME": str(home), "BOXTEAM_RUNTIME_CONTROL_TOKEN": "artifact-control",
        "BOXTEAM_RUNTIME_MANIFEST": str(runtime_manifest_path),
        "BOXTEAM_BACKEND_GENERATION": "artifact-generation", "BOXTEAM_MOUNTED_WORKSPACES": json.dumps([
            MountedWorkspace(workspace_id, context.workspace_root).to_payload()])})
    command = [sys.executable, "-S", "-m", "app.runtime.http_server", "app.main:app",
        "--host", "127.0.0.1", "--port", str(unused_tcp_port), "--log-level", "warning", "--shutdown-on-stdin-eof"]
    log = context.artifacts_dir / "backend.log"
    evidence = {"command": command, "cwd": str(application)}
    with log.open("w") as output:
        process = await asyncio.to_thread(subprocess.Popen, command, cwd=application, env=environment,
            stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT, text=True)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{unused_tcp_port}", trust_env=False,
                timeout=10, headers={"X-BoxTeam-Workspace-Id": workspace_id, "X-Request-ID": "artifact-request",
                    "X-Local-Token": "local-dev-token"}) as client:
                async with asyncio.timeout(60):
                    while True:
                        assert process.poll() is None, log.read_text()
                        try:
                            response = await client.get("/api/v1/workspace")
                        except httpx.ConnectError:
                            await asyncio.sleep(0.05)
                            continue
                        response.raise_for_status()
                        break
                assert response.headers["X-Request-ID"] == response.json()["request_id"] == "artifact-request"
                assert response.json()["data"]["workspace_id"] == workspace_id
                session_id = await create_session(client, "完整 Backend SDK 制品验收")
                detail = (await client.get(f"/api/v1/sessions/{session_id}")).raise_for_status().json()
                assert detail["data"]["session_id"] == session_id
                evidence.update({"workspace": response.json(), "session": detail})
            probe = await asyncio.to_thread(subprocess.run, [sys.executable, "-S", "-c", ("import boxteam_plugin_sdk, app; "
                "from boxteam_plugin_sdk.maintenance import start_maintenance; "
                "print(boxteam_plugin_sdk.__file__); print(app.__file__)")], cwd=application, env=environment,
                text=True, capture_output=True, timeout=10, check=False)
            assert probe.returncode == 0, probe.stderr
            sdk_path, app_path = probe.stdout.splitlines()
            assert Path(sdk_path).is_relative_to(dependencies) and Path(app_path).is_relative_to(application)
            evidence["imports"] = probe.stdout
        finally:
            await asyncio.to_thread(terminate_process, process, stdin_eof=True)
            evidence["exit_code"] = process.returncode
            (context.artifacts_dir / "backend-evidence.json").write_text(json.dumps(evidence, indent=2))
    assert process.returncode == 0
