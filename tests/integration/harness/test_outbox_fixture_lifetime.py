"""验证 IndexedDB 浏览器装具在真实资源准备和清理失败时回收已持有句柄。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def outbox_failure_probe(tmp_path: Path) -> Path:
    probe = tmp_path / "outbox-failure.mjs"
    probe.write_text(
        r'''
import { mock } from "bun:test";
import * as http from "node:http";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { chromium } from "playwright";

const [failureStage] = process.argv.slice(2);
const originalCreateServer = http.createServer;
const originalLaunch = chromium.launch.bind(chromium);
const startupMessage = `outbox准备失败: ${failureStage}`;
const cleanupMessage = `outbox清理失败: ${failureStage}`;
const cleanupOrder = [];
let server;
let browser;
let context;

mock.module("node:http", () => ({ ...http,
  createServer(...args) {
    server = originalCreateServer(...args);
    return {
      once(...values) { return server.once(...values); },
      listen(...values) {
        if (failureStage === "listen") {
          values.pop();
          return server.listen(...values, () => server.emit("error", new Error(startupMessage)));
        }
        return server.listen(...values);
      },
      address() { return server.address(); },
      get listening() { return server.listening; },
      close(callback) {
        cleanupOrder.push("server");
        return server.close(error => callback(error ?? (failureStage === "server-close" ? new Error(cleanupMessage) : undefined)));
      },
    };
  },
}));
mock.module("playwright", () => ({
  chromium: {
    async launch(options) {
      browser = await originalLaunch(options);
      return {
        async newContext(options) {
          if (failureStage === "context") throw new Error(startupMessage);
          context = await browser.newContext(options);
          return {
            async newPage() { throw new Error(startupMessage); },
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
    await import(pathToFileURL(path.join(process.cwd(), "tests/integration/clients/web/session_catalog_outbox_idb.mjs")).href);
  } catch (error) {
    failure = error;
  }
  console.log("OUTBOX_LIFETIME " + JSON.stringify({
    errors: errorMessages(failure), cleanupOrder,
    serverListening: server?.listening ?? false,
    browserConnected: browser?.isConnected() ?? false,
    temporaryDirectory: process.env.TMPDIR,
  }));
} finally {
  // 先观察装具实际释放结果，再兜底回收探针持有的真实资源。
  try {
    if (browser?.isConnected()) await browser.close();
  } finally {
    if (server?.listening) await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  }
}
''',
        encoding="utf-8",
    )
    return probe


@pytest.mark.parametrize(
    "failure_stage", ["listen", "context", "context-close", "browser-close", "server-close"]
)
def test_outbox_failure_releases_remaining_resources_and_preserves_errors(
    failure_stage: str,
    outbox_failure_probe: Path,
    tmp_path: Path,
) -> None:
    root = Path.cwd()
    environment = {
        **os.environ,
        **{name: str(tmp_path) for name in ("TMPDIR", "TMP", "TEMP")},
    }
    result = subprocess.run(
        ["bun", str(outbox_failure_probe), failure_stage],
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
    lines = [line.removeprefix("OUTBOX_LIFETIME ") for line in result.stdout.splitlines() if line.startswith("OUTBOX_LIFETIME ")]
    assert len(lines) == 1, result.stdout
    observation = json.loads(lines[0])
    assert f"outbox准备失败: {failure_stage}" in observation["errors"], observation
    if failure_stage.endswith("-close"):
        assert f"outbox清理失败: {failure_stage}" in observation["errors"], observation
    assert observation["serverListening"] is False, observation
    assert observation["browserConnected"] is False, observation
    expected_order = ["server"] if failure_stage == "listen" else ["browser", "server"] if failure_stage == "context" else ["context", "browser", "server"]
    assert observation["cleanupOrder"] == expected_order, observation
