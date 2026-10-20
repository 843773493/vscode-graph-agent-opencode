"""注入准备阶段失败，检查真实 Vite/Chromium 句柄由组件装具释放。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def startup_failure_probe(tmp_path: Path) -> Path:
    probe = tmp_path / "component-startup-failure.mjs"
    probe.write_text(
        r'''
import { mock } from "bun:test";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { chromium } from "playwright";

const [fixtureName, failureStage] = process.argv.slice(2);
const root = process.cwd();
const viteUrl = pathToFileURL(path.join(root, "src/clients/web/node_modules/vite/dist/node/index.js")).href;
const originalCreateServer = (await import(viteUrl)).createServer;
const originalLaunch = chromium.launch.bind(chromium);
const failureMessage = `组件准备阶段失败: ${failureStage}`;
let server;
let browser;
let viteCloseCalls = 0;
let browserCloseCalls = 0;
let configuredCacheDir;

mock.module(viteUrl, () => ({
  createServer: async options => {
    configuredCacheDir = options.cacheDir ?? null;
    // 旧装具也不能让本次故障探针写入源码树。
    server = await originalCreateServer({ ...options, cacheDir: options.cacheDir ?? path.join(process.env.TMPDIR, "vite-cache") });
    return {
      httpServer: server.httpServer,
      async listen() {
        await server.listen();
        if (failureStage === "listen") throw new Error(failureMessage);
      },
      async close() {
        viteCloseCalls += 1;
        await server.close();
      },
    };
  },
}));
mock.module("playwright", () => ({
  chromium: {
    async launch(options) {
      if (failureStage === "launch") throw new Error(failureMessage);
      browser = await originalLaunch(options);
      return {
        async newContext() { throw new Error(failureMessage); },
        async close() {
          browserCloseCalls += 1;
          await browser.close();
        },
      };
    },
  },
}));

try {
  let failure;
  try {
    await import(pathToFileURL(path.join(root, "tests/integration/clients/web/components", `${fixtureName}.mjs`)).href);
  } catch (error) {
    failure = error;
  }
  console.log("FIXTURE_LIFETIME " + JSON.stringify({
    error: failure?.message ?? null,
    viteListening: server?.httpServer?.listening ?? false,
    browserConnected: browser?.isConnected() ?? false,
    viteCloseCalls, browserCloseCalls, configuredCacheDir,
    temporaryDirectory: process.env.TMPDIR,
  }));
} finally {
  // 核验前不替装具补清理；核验后仍由探针兜底回收本次真实句柄。
  try {
    if (browser?.isConnected()) await browser.close();
  } finally {
    if (server) await server.close();
  }
}
''',
        encoding="utf-8",
    )
    return probe


@pytest.mark.parametrize(
    "fixture_name",
    ["anchored_overlay_positioning", "structured_user_message_attachments"],
)
@pytest.mark.parametrize("failure_stage", ["listen", "launch", "context"])
def test_component_setup_failure_releases_acquired_resources(
    fixture_name: str,
    failure_stage: str,
    startup_failure_probe: Path,
    tmp_path: Path,
) -> None:
    root = Path.cwd()
    environment = {
        **os.environ,
        **{name: str(tmp_path) for name in ("TMPDIR", "TMP", "TEMP")},
    }
    result = subprocess.run(
        ["bun", str(startup_failure_probe), fixture_name, failure_stage],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    (tmp_path / "stdout.raw").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "stderr.raw").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    lines = [line.removeprefix("FIXTURE_LIFETIME ") for line in result.stdout.splitlines() if line.startswith("FIXTURE_LIFETIME ")]
    assert len(lines) == 1, result.stdout
    observation = json.loads(lines[0])
    assert observation["error"] == f"组件准备阶段失败: {failure_stage}", observation
    assert observation["viteListening"] is False, observation
    assert observation["browserConnected"] is False, observation
    assert observation["viteCloseCalls"] == 1, observation
    assert observation["browserCloseCalls"] == (1 if failure_stage == "context" else 0), observation
    runtime = root / "out/tests/integration/clients/web/components" / fixture_name / "runtime"
    assert observation["temporaryDirectory"] == str(runtime), observation
    assert observation["configuredCacheDir"] == str(runtime / "vite-cache"), observation
