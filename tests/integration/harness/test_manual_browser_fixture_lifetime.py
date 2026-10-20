"""在真实 Chromium 准备边界注入失败，核验手动连接装具释放与异常归属。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def manual_browser_failure_probe(tmp_path: Path) -> Path:
    probe = tmp_path / "manual-browser-failure.mjs"
    probe.write_text(
        r'''
import { mock } from "bun:test";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { chromium } from "playwright";

const [failureStage] = process.argv.slice(2);
const originalLaunch = chromium.launch.bind(chromium);
const startupMessage = `手动浏览器准备失败: ${failureStage}`;
const cleanupMessage = `手动浏览器清理失败: ${failureStage}`;
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
                async goto() { throw new Error(startupMessage); },
                screenshot(...args) { return page.screenshot(...args); },
              };
            },
            on(...args) { return context.on(...args); },
            async route(...args) {
              if (failureStage === "route") throw new Error(startupMessage);
              await context.route(...args);
            },
            pages() { return context.pages(); },
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
    await import(pathToFileURL(path.join(process.cwd(), "tests/integration/clients/web/manual_browser_connection.mjs")).href);
  } catch (error) {
    failure = error;
  }
  console.log("MANUAL_BROWSER_LIFETIME " + JSON.stringify({
    errors: errorMessages(failure), cleanupOrder, contextClosed,
    contextAcquired: context !== undefined,
    browserConnected: browser?.isConnected() ?? false,
    temporaryDirectory: process.env.TMPDIR,
  }));
} finally {
  // 观察装具终态后才兜底回收，不能用探针清理掩盖被测装具的泄漏。
  if (browser?.isConnected()) await browser.close();
}
''',
        encoding="utf-8",
    )
    return probe


@pytest.mark.parametrize(
    "failure_stage",
    ["launch", "context", "page", "route", "goto", "context-close", "browser-close"],
)
def test_manual_browser_failure_releases_resources_and_preserves_errors(
    failure_stage: str,
    manual_browser_failure_probe: Path,
    tmp_path: Path,
) -> None:
    root = Path.cwd()
    environment = {
        **os.environ,
        **{name: str(tmp_path) for name in ("TMPDIR", "TMP", "TEMP")},
        # 准备边界在访问此地址前明确失败；不连接任何用户或测试业务服务。
        "BOXTEAM_E2E_BASE_URL": "http://127.0.0.1:1",
        "BOXTEAM_E2E_FIXTURE": json.dumps({"sessionTitle": "未进入场景", "workspaceId": "unused"}),
        "BOXTEAM_E2E_RESULT_PATH": str(tmp_path / "result.json"),
        "BOXTEAM_E2E_SCREENSHOT_PATH": str(tmp_path / "failure.png"),
    }
    result = subprocess.run(
        ["bun", str(manual_browser_failure_probe), failure_stage],
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
    lines = [
        line.removeprefix("MANUAL_BROWSER_LIFETIME ")
        for line in result.stdout.splitlines()
        if line.startswith("MANUAL_BROWSER_LIFETIME ")
    ]
    assert len(lines) == 1, result.stdout
    observation = json.loads(lines[0])
    assert f"手动浏览器准备失败: {failure_stage}" in observation["errors"], observation
    if failure_stage.endswith("-close"):
        assert f"手动浏览器清理失败: {failure_stage}" in observation["errors"], observation
    assert observation["browserConnected"] is False, observation
    assert observation["contextClosed"] is observation["contextAcquired"], observation
    expected_order = (
        [] if failure_stage == "launch"
        else ["browser"] if failure_stage == "context"
        else ["context", "browser"]
    )
    assert observation["cleanupOrder"] == expected_order, observation
