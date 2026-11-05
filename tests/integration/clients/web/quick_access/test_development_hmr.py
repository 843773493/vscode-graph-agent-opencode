"""完整源码快照的真实 Vite 热更新，不修改开发工作树或预写浏览器状态。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest

from app.gateway.runtime.process_lifecycle import ManagedProcess
from tests.harness.python.run_context import TestRunContext
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_case as metadata_case,  # noqa: PLC0414
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_port_reservations as metadata_port_reservations,  # noqa: PLC0414
)
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.scope import capture_test_processes, register_test_process
from tests.support.processes.workspace import wait_for_http_ok


@pytest.fixture
def development_snapshot(request: pytest.FixtureRequest) -> Path:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime = context.runtime_root_for_node(request.node.nodeid)
    snapshot = runtime / "source"
    snapshot.mkdir()
    archive = runtime / "source.tar"
    subprocess.run(["git", "archive", "--format=tar", "-o", str(archive), "HEAD"],
                   cwd=context.project_root, check=True)
    with tarfile.open(archive) as files:
        files.extractall(snapshot, filter="data")
    archive.unlink()
    # 完整带入当前差异，避免测试与生产源码错配；随后只改独占副本中的观测属性。
    difference = subprocess.check_output(["git", "diff", "--binary", "HEAD"], cwd=context.project_root)
    subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], input=difference,
                   cwd=snapshot, check=True)
    for name in ("node_modules", "src/clients/web/node_modules", "src/clients/workbench/node_modules"):
        destination = snapshot / name
        if destination.is_dir():
            destination.rmdir()
        destination.symlink_to(context.project_root / name, target_is_directory=True)
    # 转换锚点依赖上游在此快照中的真实路径，不能将上游源码链接回另一工作树。
    upstream = context.project_root / "src/clients/workbench/upstream"
    upstream_snapshot = snapshot / "src/clients/workbench/upstream"
    upstream_snapshot.mkdir(exist_ok=True)
    subprocess.run(["git", "archive", "--format=tar", "-o", str(archive), "HEAD"], cwd=upstream, check=True)
    with tarfile.open(archive) as files:
        files.extractall(upstream_snapshot, filter="data")
    archive.unlink()
    upstream_git = subprocess.check_output(["git", "rev-parse", "--absolute-git-dir"], cwd=upstream, text=True).strip()
    (upstream_snapshot / ".git").write_text(f"gitdir: {upstream_git}\n", encoding="utf-8")
    source = snapshot / "src/clients/workbench/src/react/navigationView.tsx"
    original = source.read_text(encoding="utf-8")
    anchor = '<div className="native-session-navigation"'
    assert original.count(anchor) == 1
    source.write_text(original.replace(anchor, anchor + ' data-hmr-acceptance="version-1"'), encoding="utf-8")
    (context.artifacts_dir / "source-snapshot.json").write_text(json.dumps({
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=context.project_root, text=True).strip(),
        "diff_sha256": hashlib.sha256(difference).hexdigest(),
        "source": str(source), "original_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "upstream_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=upstream, text=True).strip(),
        "boundary": "完整 git archive 和当前 diff，上游也是固定 HEAD 的完整源码快照；仅依赖只读链接，观测属性只改独占副本",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return snapshot


@pytest.mark.asyncio
async def test_source_update_reaches_real_workbench_without_manual_reload(
    request: pytest.FixtureRequest, development_snapshot: Path, metadata_port_reservations: ExitStack,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
) -> None:
    client, context, _workspace, provider = metadata_case
    runtime = context.runtime_root_for_node(request.node.nodeid)
    node = shutil.which("node")
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert node and chromium, "真实开发入口需要 Node 和 Chromium"
    ports = integration_port_block_for_file(Path(request.node.path))
    lease, probes = E2EProcessLease.acquire_available(runtime / "frontend-port.json", range(ports.port(20), ports.end + 1))
    metadata_port_reservations.callback(lease.release)
    port = lease.ports[0]
    base = f"http://127.0.0.1:{port}"
    (context.artifacts_dir / "frontend-port.json").write_text(json.dumps({
        "port": port, "probes": probes,
    }, indent=2) + "\n", encoding="utf-8")
    command = [node, "scripts/dev.mjs", "--port", str(port), "--gateway", str(client.base_url).rstrip("/"),
               "--outdir", str(runtime / "assets/dist"), "--cache", str(runtime / "vite-cache")]
    with (context.artifacts_dir / "vite.log").open("x", encoding="utf-8") as log, capture_test_processes():
        process = await asyncio.to_thread(subprocess.Popen, command,
            cwd=development_snapshot / "src/clients/workbench", stdout=log, stderr=subprocess.STDOUT,
            text=True, start_new_session=True)
        managed = ManagedProcess(process=process, log_file=log)
        register_test_process(managed, managed.close)
        await asyncio.to_thread(wait_for_http_ok, f"{base}/health", process, timeout_seconds=180)
        driver = ["bun", "tests/integration/clients/web/quick_access/development_hmr.mjs"]
        supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
        protected = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
                     "--timeout-seconds", "90", "--", *driver]
        with chromium_temporary_environment(runtime / "chromium-tmp") as temporary:
            browser = await asyncio.to_thread(subprocess.run, protected, cwd=context.project_root,
                env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": base,
                     "BOXTEAM_HMR_SOURCE": str(development_snapshot / "src/clients/workbench/src/react/navigationView.tsx"),
                     "BOXTEAM_BROWSER_RESULT_PATH": str(context.artifacts_dir / "development-hmr.json"),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                capture_output=True, text=True, timeout=105, check=False)
        (context.artifacts_dir / "browser.stdout.raw").write_text(browser.stdout, encoding="utf-8")
        (context.artifacts_dir / "browser.stderr.raw").write_text(browser.stderr, encoding="utf-8")
        (context.artifacts_dir / "processes.json").write_text(json.dumps({
            "frontend_command": command, "frontend_pid": process.pid,
            "browser_command": protected, "browser_exit_code": browser.returncode,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        assert browser.returncode == 0, browser.stdout + browser.stderr
    with provider.control._condition:
        calls = {key: count for key, count in provider.control._counters.items() if key.startswith("model_call/")}
    assert calls == {}, "开发静态资源更新不应触发模型请求"
