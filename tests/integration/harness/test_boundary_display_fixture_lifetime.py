"""验证三个历史边界展示装具在真实 Chromium 准备与清理失败时的资源归属。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.support.paths import chromium_temporary_environment


@pytest.fixture
def boundary_display_failure_probe(tmp_path: Path) -> Path:
    probe = tmp_path / "boundary-display-failure.mjs"
    probe.write_text(
        r'''
import { mock } from "bun:test";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { chromium } from "playwright";

const [scriptName, failureStage] = process.argv.slice(2);
const originalLaunch = chromium.launch.bind(chromium);
const startupMessage = `边界展示准备失败: ${failureStage}`;
const cleanupMessage = `边界展示清理失败: ${failureStage}`;
const cleanupOrder = [];
let browser;
let context;
let contextClosed = false;

mock.module("playwright", () => ({
  chromium: {
    async launch(options) {
      if (failureStage === "launch") throw new Error(startupMessage);
      browser = await originalLaunch(options);
      return {
        async newContext(options) {
          if (failureStage === "context") throw new Error(startupMessage);
          context = await browser.newContext(options);
          context.on("close", () => { contextClosed = true; });
          return {
            async newPage() {
              if (failureStage === "page") throw new Error(startupMessage);
              const page = await context.newPage();
              return {
                on(...args) {
                  if (failureStage === "event") throw new Error(startupMessage);
                  return page.on(...args);
                },
                async goto() { throw new Error(startupMessage); },
                screenshot(...args) { return page.screenshot(...args); },
              };
            },
            async close() {
              cleanupOrder.push("context");
              if (failureStage === "context-close") throw new Error(cleanupMessage);
              await context.close();
            },
          };
        },
        async close() {
          cleanupOrder.push("browser");
          await browser.close();
          if (failureStage === "browser-close") throw new Error(cleanupMessage);
        },
      };
    },
  },
}));

function errorMessages(error) {
  if (!error) return [];
  return [error.message, ...errorMessages(error.cause), ...(error.errors ?? []).flatMap(errorMessages)];
}

try {
  let failure;
  try {
    await import(pathToFileURL(path.join(process.cwd(), `tests/integration/clients/web/${scriptName}.mjs`)).href);
  } catch (error) {
    failure = error;
  }
  console.log("BOUNDARY_DISPLAY_LIFETIME " + JSON.stringify({
    errors: errorMessages(failure), cleanupOrder, contextClosed,
    contextAcquired: context !== undefined,
    browserConnected: browser?.isConnected() ?? false,
    temporaryDirectory: process.env.TMPDIR,
  }));
} finally {
  // 先记录装具终态，再回收遗留句柄，不能让探针兜底掩盖装具泄漏。
  if (browser?.isConnected()) await browser.close();
}
''',
        encoding="utf-8",
    )
    return probe


@pytest.mark.parametrize(
    "script_name",
    ["boundary_cases_display", "partial_text_boundary_display", "unknown_tool_outcome_display"],
)
@pytest.mark.parametrize(
    "failure_stage",
    ["launch", "context", "page", "event", "goto", "context-close", "browser-close"],
)
def test_boundary_display_failure_releases_resources_and_preserves_errors(
    script_name: str,
    failure_stage: str,
    boundary_display_failure_probe: Path,
    tmp_path: Path,
) -> None:
    environment = {
        **os.environ,
        **{name: str(tmp_path) for name in ("TMPDIR", "TMP", "TEMP")},
        # 所有故障均在业务访问前触发，不连接用户服务或构造历史业务数据。
        "BOXTEAM_BROWSER_BASE_URL": "http://127.0.0.1:1",
        "BOXTEAM_BROWSER_WORKSPACE_ID": "unused",
        "BOXTEAM_BROWSER_SESSION_ID": "unused",
        "BOXTEAM_BROWSER_TURN_ID": "unused",
        "BOXTEAM_BROWSER_RESULT_PATH": str(tmp_path / "result.json"),
        "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(tmp_path / "failure.png"),
    }
    with chromium_temporary_environment(tmp_path / "chromium-tmp") as temporary_environment:
        environment.update(temporary_environment)
        result = subprocess.run(
            ["bun", str(boundary_display_failure_probe), script_name, failure_stage],
            cwd=Path.cwd(),
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    (tmp_path / "stdout.raw").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "stderr.raw").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    observations = [
        json.loads(line.removeprefix("BOUNDARY_DISPLAY_LIFETIME "))
        for line in result.stdout.splitlines()
        if line.startswith("BOUNDARY_DISPLAY_LIFETIME ")
    ]
    assert len(observations) == 1, result.stdout
    observation = observations[0]
    assert f"边界展示准备失败: {failure_stage}" in observation["errors"], observation
    if failure_stage.endswith("-close"):
        assert f"边界展示清理失败: {failure_stage}" in observation["errors"], observation
    assert observation["browserConnected"] is False, observation
    assert observation["contextClosed"] is observation["contextAcquired"], observation
    expected_order = (
        [] if failure_stage == "launch"
        else ["browser"] if failure_stage == "context"
        else ["context", "browser"]
    )
    assert observation["cleanupOrder"] == expected_order, observation
