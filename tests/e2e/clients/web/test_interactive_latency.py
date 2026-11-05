"""普通交互的总延迟门；进程 timeout 只防挂死，不能当作性能预算。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import e2e_port_block_for_file
from tests.support.processes.gateway import close_gateway_process, start_gateway_process
from tests.support.workspaces import prepare_test_workspace


@pytest.fixture(scope="module")
def e2e_workspace_root_path(request: pytest.FixtureRequest) -> str:
    scope = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = prepare_test_workspace(
        workspace_root=scope.output_root / "workspace",
        template_root=scope.project_root / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(".boxteam/workspace-identity.json",),
    )
    for index in range(128):
        (workspace / f"latency-{index:03d}.txt").write_text(
            f"LATENCY FILE {index:03d}\n" + ("x" * 120 + "\n") * 1084, encoding="utf-8",
        )
    return str(workspace)


@pytest.fixture
def latency_gateway_port(request: pytest.FixtureRequest) -> Iterator[int]:
    scope = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    block = e2e_port_block_for_file(Path(request.node.path))
    lease, probes = E2EProcessLease.acquire_available(
        scope.runtime_root_for_node(request.node.nodeid) / "gateway-port.json",
        range(block.port(20), block.end + 1),
    )
    try:
        (scope.artifacts_dir / "ports.json").write_text(json.dumps({
            "port": lease.ports[0], "probes": probes,
        }) + "\n", encoding="utf-8")
        yield lease.ports[0]
    finally:
        lease.release()


async def test_navigation_session_and_file_have_total_latency_budgets(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
    latency_gateway_port: int,
    web_workbench_assets: Path,
) -> None:
    """无模型、无响应替身；真实正文可用才停表，不测占位加载框。"""
    assert Path(e2e_workspace_config_path).is_file()
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "交互延迟 E2E 需要真实 Chromium"
    scope = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    result_path = scope.artifacts_dir / "latency.json"
    # 证据记录放在测量前，不能让源码指纹计算混入普通交互热路径。
    source_files = [
        str(Path(request.node.path).relative_to(scope.project_root)),
        "tests/e2e/clients/web/interactive_latency.mjs",
        "src/clients/workbench/src/react/chatView.tsx",
        "src/clients/workbench/src/agentApi.ts",
        "src/clients/workbench/src/workspaceFiles.ts",
    ]
    head = await asyncio.to_thread(subprocess.check_output, ["git", "rev-parse", "HEAD"], cwd=scope.project_root, text=True)
    (scope.artifacts_dir / "source.json").write_text(json.dumps({
        "head": head.strip(),
        "sha256": {path: hashlib.sha256((scope.project_root / path).read_bytes()).hexdigest() for path in source_files},
    }, indent=2) + "\n", encoding="utf-8")
    gateway = await asyncio.to_thread(
        start_gateway_process,
        workspace_root=Path(e2e_workspace_root_path), default_backend_url=None,
        port=latency_gateway_port,
        boxteam_home=scope.boxteam_home_for_node(request.node.nodeid),
        artifacts_dir=scope.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)},
    )
    command = ["node", "tests/e2e/clients/web/interactive_latency.mjs"]
    try:
        with chromium_temporary_environment(scope.runtime_root_for_node(request.node.nodeid) / "chromium") as temporary:
            process = await asyncio.to_thread(
                subprocess.run, command, cwd=scope.project_root,
                env={**os.environ, **temporary,
                     "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                capture_output=True, text=True, timeout=120, check=False,
            )
        (scope.artifacts_dir / "browser.stdout.raw").write_text(process.stdout, encoding="utf-8")
        (scope.artifacts_dir / "browser.stderr.raw").write_text(process.stderr, encoding="utf-8")
        (scope.artifacts_dir / "browser-process.json").write_text(json.dumps({
            "command": command, "exit_code": process.returncode,
        }) + "\n", encoding="utf-8")
        assert process.returncode == 0, f"{process.stdout}\n{process.stderr}\n原样本: {result_path}"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["phase"] == "passed"
        assert result["pageErrors"] == []
        assert result["forbiddenExecutions"] == []
        assert len(result["navigationMs"]) == 20
        assert len(result["sessionOpenMs"]) == 6
        assert len(result["fileOpenMs"]) == 6
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
