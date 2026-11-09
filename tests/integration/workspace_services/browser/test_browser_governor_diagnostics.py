"""真实Browser健康接口的治理状态与原始日志边界；只替换Linux内核文件读取。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_plugin import (
    TerminalPluginGateway,
    browser_plugin_gateway_for_workspace,
)
from tests.support.workspaces import prepare_default_test_workspace


@pytest.fixture
async def browser_service(
    request: pytest.FixtureRequest, unused_tcp_port: int,
    monkeypatch: pytest.MonkeyPatch,
    prepared_developer_tools_package: Path,
) -> AsyncIterator[tuple[TerminalPluginGateway, TestRunContext, Path, Path, str]]:
    with TestRunContext.from_test_file(Path(__file__)) as run:
        runtime = run.runtime_root_for_node(request.node.nodeid)
        runtime.mkdir(parents=True, exist_ok=True)
        for name in ("TMPDIR", "TMP", "TEMP"):
            monkeypatch.setenv(name, str(runtime))
        root = prepare_default_test_workspace(
            workspace_root=run.workspace_root / uuid4().hex,
            template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        )
        marker = runtime / "fail-memory-read"
        native_path = str(request.param)
        preload = runtime / "kernel-memory-boundary.mjs"
        preload.write_text(
            f"const marker = {json.dumps(str(marker))};\n"
            f"const nativePath = {json.dumps(native_path)};\n" + """
import fs from "node:fs";
import promises from "node:fs/promises";
import { syncBuiltinESMExports } from "node:module";

const pathExists = fs.existsSync;
const readFile = promises.readFile;
const directory = "/sys/fs/cgroup/boxteam-governor-proof";
const files = new Map([
  ["/proc/meminfo", "MemTotal: 1000000 kB\\nMemAvailable: 600000 kB\\n"],
  ["/proc/self/cgroup", "0::/boxteam-governor-proof\\n"],
  [`${directory}/memory.max`, "1000"],
  [`${directory}/memory.current`, "400"],
  [`${directory}/memory.events`, "high 0\\noom 0\\noom_kill 0\\n"],
]);
fs.existsSync = (file) => {
  if (file === "/sys/fs/cgroup/cgroup.controllers" || file === `${directory}/memory.max`) return true;
  if (file === `${directory}/memory.pressure`) return false;
  return pathExists(file);
};
promises.readFile = async (file, ...args) => {
  if (file === "/proc/meminfo" && pathExists(marker)) {
    throw Object.assign(new Error(`EACCES: ${nativePath}`, {
      cause: new Error(`原始采样来源: ${nativePath}`),
    }), { code: "EACCES" });
  }
  return files.has(file) ? files.get(file) : await readFile(file, ...args);
};
syncBuiltinESMExports();
""", encoding="utf-8",
        )
        node_options = os.environ.get("NODE_OPTIONS", "")
        monkeypatch.setenv("NODE_OPTIONS", f"{node_options} --import={preload.as_uri()}".strip())
        home = run.boxteam_home_for_node(request.node.nodeid)
        monkeypatch.setenv("BOXTEAM_HOME", str(home))
        async with browser_plugin_gateway_for_workspace(
            root, run.artifacts_dir / "original", unused_tcp_port, home,
            prepared_package_root=prepared_developer_tools_package,
        ) as gateway:
            yield gateway, run, root, marker, native_path


@pytest.mark.skipif(sys.platform != "linux", reason="明确替换Linux内核文件边界，不代表其他平台实机")
@pytest.mark.parametrize("browser_service", [
    "/private/browser/memory",
    r"C:\private\browser\memory",
    r"\\host\share\browser\memory",
], indirect=True, ids=["posix", "drive", "unc"])
async def test_health_keeps_memory_locations_in_raw_diagnostics(
    browser_service: tuple[TerminalPluginGateway, TestRunContext, Path, Path, str],
) -> None:
    gateway, run, root, marker, native_path = browser_service
    def health_url() -> str:
        slot = gateway.runtime.slots[(gateway.binding.package_id, gateway.binding.plugin_id, gateway.workspace_id)]
        if slot.base_url is None:
            raise RuntimeError("Browser 插件当前没有运行实例")
        return f"{slot.base_url}/health"

    url = health_url()
    async with httpx.AsyncClient(timeout=2) as http:
        async with asyncio.timeout(5):
            while True:
                response = await http.get(url)
                response.raise_for_status()
                before = response.json()
                if before["resource_governor"]["last_sample"] is not None:
                    break
                await asyncio.sleep(0.05)
        marker.write_text("fail", encoding="utf-8")
        async with asyncio.timeout(8):
            while True:
                response = await http.get(url)
                response.raise_for_status()
                failed = response.json()
                if failed["resource_governor"]["error"] is not None:
                    break
                await asyncio.sleep(0.05)
        for name, payload in (("before", before), ("failed", failed)):
            (run.artifacts_dir / f"{name}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
            )
        # 同一次真实响应必须保留已知采样，不能将读取失败替换为零压力。
        state = failed["resource_governor"]
        assert before["resource_governor"]["running"] is True
        assert before["resource_governor"]["last_sample"]["cgroup"] == {
            "currentBytes": 400, "maxBytes": 1000, "availableBytes": 600,
            "usedRatio": 0.4, "events": {"high": 0, "oom": 0, "oom_kill": 0}, "someAvg10": 0,
        }
        assert state["running"] is False
        assert state["last_sample"] == before["resource_governor"]["last_sample"]
        assert "stage=resource_cycle" in state["error"] and "EACCES" in state["error"]
        for payload in (before, failed):
            serialized = json.dumps(payload, ensure_ascii=False)
            for private in (native_path, "/sys/fs/cgroup", str(root), str(Path.cwd())):
                assert json.dumps(private)[1:-1] not in serialized
            assert ".mjs:" not in serialized and ".js:" not in serialized
        diagnostics = "\n".join(
            log.read_text(encoding="utf-8")
            for log in gateway.logs_dir.glob("plugins/plugin-*.log")
        )
        assert f"EACCES: {native_path}" in diagnostics
        assert f"原始采样来源: {native_path}" in diagnostics
        assert "[cause]:" in diagnostics and "kernel-memory-boundary.mjs:" in diagnostics
        assert diagnostics.count("[browser-resource-governor] 资源调度停止") == 1
        assert (await http.get(url)).json()["resource_governor"] == state

        marker.unlink()
        await gateway.restart()
        url = health_url()
        async with asyncio.timeout(5):
            while True:
                recovered = (await http.get(url)).json()["resource_governor"]
                if recovered["last_sample"] is not None:
                    break
                await asyncio.sleep(0.05)
        assert recovered["running"] is True and recovered["error"] is None
        assert recovered["last_sample"]["cgroup"] == before["resource_governor"]["last_sample"]["cgroup"]
        run.write_result("passed", {
            "scope": "真实Gateway托管Browser插件、Node/HTTP及内存采样调度器；仅替换Linux内核读取",
            "native_path_form": native_path, "governor_before": before["resource_governor"],
            "governor_failed": state, "governor_restarted": recovered,
        })
