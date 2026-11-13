"""完整源码快照的真实 Vite 热更新，不修改开发工作树或预写浏览器状态。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest

from app.gateway.runtime.process_lifecycle import ManagedProcess
from tests.harness.python.run_context import TestRunContext
from tests.integration.clients.web.quick_access.source_snapshot import (
    snapshot_workbench_sources,
)
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


@pytest.fixture(autouse=True)
def metadata_web_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_WEB_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


@pytest.fixture
def development_snapshot(request: pytest.FixtureRequest) -> Path:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime = context.runtime_root_for_node(request.node.nodeid)
    reused = os.getenv("BOXTEAM_HMR_SOURCE_SNAPSHOT")
    if reused:
        snapshot = Path(reused).resolve(strict=True)
        old_run, old_node = snapshot.parent.parent.name, snapshot.parent.name
        prior_receipt = context.output_root / "runs" / old_run / "nodes" / old_node / "artifacts" / "source-snapshot.json"
        receipt = json.loads(prior_receipt.read_text(encoding="utf-8"))
        assert receipt["upstream_head"] == "fc8954c90fe6dea1f4c411dd73fdfe5f1913b5ca"
        source = snapshot / "src/clients/workbench/src/react/navigationView.tsx"
        original = source.read_text(encoding="utf-8")
        version_one = 'data-hmr-acceptance="version-1"'
        version_two = 'data-hmr-acceptance="version-2"'
        if original.count(version_one) == 0 and original.count(version_two) == 1:
            source.write_text(original.replace(version_two, version_one), encoding="utf-8")
        elif original.count(version_one) == 0:
            anchor = '<div className="native-session-navigation"'
            assert original.count(anchor) == 1
            source.write_text(original.replace(anchor, anchor + " " + version_one), encoding="utf-8")
        assert source.read_text(encoding="utf-8").count('data-hmr-acceptance="version-1"') == 1
        project_translations = snapshot / "src/clients/workbench/resources/localization/zh-cn-project.json"
        project_messages = json.loads(project_translations.read_text(encoding="utf-8"))
        project_messages["contents"].get("vs/workbench/contrib/localization/common/localizationsActions", {}).pop(
            "chooseLocale", None,
        )
        project_translations.write_text(json.dumps(project_messages, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        current_config = context.project_root / "src/clients/workbench/scripts/devConfig.mjs"
        config_copy = snapshot / "src/clients/workbench/scripts/devConfig.mjs"
        shutil.copy2(current_config, config_copy)
        receipt["reused_from"] = str(snapshot)
        receipt["reused_dev_config_sha256"] = hashlib.sha256(config_copy.read_bytes()).hexdigest()
        receipt["boundary"] = "复用完整独占源码快照和已构建静态产物，仅同步并记录本轮 devConfig SHA"
        original_sha256 = receipt["original_sha256"]
    else:
        snapshot, receipt = snapshot_workbench_sources(context.project_root, runtime)
        source = snapshot / "src/clients/workbench/src/react/navigationView.tsx"
        original = source.read_text(encoding="utf-8")
        anchor = '<div className="native-session-navigation"'
        assert original.count(anchor) == 1
        source.write_text(original.replace(anchor, anchor + ' data-hmr-acceptance="version-1"'), encoding="utf-8")
        original_sha256 = hashlib.sha256(original.encode()).hexdigest()
    (context.artifacts_dir / "source-snapshot.json").write_text(json.dumps({
        **receipt,
        "source": str(source), "original_sha256": original_sha256,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return snapshot


@pytest.mark.asyncio
async def test_source_update_reaches_real_workbench_without_manual_reload(
    request: pytest.FixtureRequest, development_snapshot: Path, metadata_port_reservations: ExitStack,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
) -> None:
    client, context, workspace, provider = metadata_case
    (workspace / 'development-locale.md').write_text(
        '# Development locale preview\n\n![blocked](http://127.0.0.1:9/blocked.png)\n', encoding='utf-8',
    )
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
        await asyncio.to_thread(wait_for_http_ok, f"{base}/health", process, timeout_seconds=300)
        driver = ["node", "tests/integration/clients/web/quick_access/development_hmr.mjs"]
        supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
        protected = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
                     "--timeout-seconds", "180", "--", *driver]
        with chromium_temporary_environment(runtime / "chromium-tmp") as temporary:
            browser = await asyncio.to_thread(subprocess.run, protected, cwd=context.project_root,
                env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": base,
                     "BOXTEAM_HMR_SOURCE": str(development_snapshot / "src/clients/workbench/src/react/navigationView.tsx"),
                         "BOXTEAM_HMR_TRANSLATIONS": str(development_snapshot / "src/clients/workbench/resources/localization/zh-cn-upstream-main.json"),
                     "BOXTEAM_BROWSER_RESULT_PATH": str(context.artifacts_dir / "development-hmr.json"),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                capture_output=True, text=True, timeout=195, check=False)
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
