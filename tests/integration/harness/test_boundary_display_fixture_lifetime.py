"""验证历史边界展示及聊天装具在真实 Chromium 准备与清理失败时的资源归属。"""

from __future__ import annotations

import json
import os
import shutil
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
                waitForResponse() { return new Promise(() => {}); },
                locator(...args) { return page.locator(...args); },
                screenshot(...args) {
                  if (failureStage === "screenshot") throw new Error("边界展示诊断失败: screenshot");
                  return page.screenshot(...args);
                },
                url() { return page.url(); },
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
    ("script_name", "failure_stage"),
    [(script, stage)
     for script in ("boundary_cases_display", "partial_text_boundary_display", "unknown_tool_outcome_display", "basic_chat_tool_loop", "workspace_metadata_bootstrap")
     for stage in ("launch", "context", "page", "event", "goto", "context-close", "browser-close")]
    + [(script, stage) for script in ("basic_chat_tool_loop", "workspace_metadata_bootstrap") for stage in ("screenshot", "result-write")],
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
        "BOXTEAM_BROWSER_GATEWAY_ID": "unused",
        "BOXTEAM_BROWSER_SESSION_ID": "unused",
        "BOXTEAM_BROWSER_TURN_ID": "unused",
        "BOXTEAM_BROWSER_FIXTURE": json.dumps({"nativeDirectory": {"sessionId": "unused"}}),
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium") or shutil.which("chromium-browser"),
        "BOXTEAM_BROWSER_RESULT_PATH": str(
            tmp_path / "missing/result.json" if failure_stage == "result-write" else tmp_path / "result.json"
        ),
        "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(tmp_path / "failure.png"),
    }
    assert environment["PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"], "装具资源验收需要真实 Chromium"
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
    if script_name == "workspace_metadata_bootstrap":
        if failure_stage == "result-write":
            assert any("ENOENT" in message for message in observation["errors"]), observation
            assert not (tmp_path / "missing/result.json").exists()
            return
        if failure_stage == "screenshot":
            assert "边界展示诊断失败: screenshot" in observation["errors"], observation
        diagnostic = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
        assert diagnostic["error"] == f"Error: 边界展示准备失败: {failure_stage}"
        assert diagnostic["page_errors"] == []
        assert diagnostic["settings_reload_rounds"] == []
    if script_name == "basic_chat_tool_loop":
        if failure_stage == "result-write":
            assert any("ENOENT" in message for message in observation["errors"]), observation
            assert not (tmp_path / "missing/result.json").exists()
            return
        if failure_stage == "screenshot":
            assert "边界展示诊断失败: screenshot" in observation["errors"], observation
        diagnostic = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
        assert diagnostic["error"] == f"Error: 边界展示准备失败: {failure_stage}"
        assert diagnostic["phase"] == (
            "gateway-bootstrap" if failure_stage in {"goto", "context-close", "browser-close", "screenshot"}
            else "browser-setup"
        )
        assert diagnostic["pageErrors"] == []
        assert diagnostic["streamResponses"] == []
        assert diagnostic["traceStreams"] == []
        assert "composerSendRequest" not in diagnostic
