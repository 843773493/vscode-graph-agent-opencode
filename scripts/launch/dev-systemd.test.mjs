import { describe, expect, test } from "bun:test";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import path from "node:path";

import {
  developmentSystemdUnitName,
  resolveDevelopmentLayout,
} from "./dev-environment.mjs";
import { buildTransientDevelopmentUnit } from "./dev-systemd.mjs";

// 布局用例只计算；状态用例仅在测试输出目录使用本地服务与 systemctl 替身。
const fixtureRoot = path.resolve(process.cwd(), "out/tests/scripts/launch/dev-systemd/workspace");
const fixtureProjectRoot = path.join(fixtureRoot, "源码 工作区", "feature-a");
const fixtureDefaultBoxteamHome = path.join(fixtureProjectRoot, "out", "development-runtime", "boxteam-home");
const fixtureExplicitBoxteamHome = path.join(fixtureRoot, "显式 控制目录");

async function withStatusProbe(respond, exercise, { preloadSource } = {}) {
  mkdirSync(fixtureRoot, { recursive: true });
  const runtime = mkdtempSync(path.join(fixtureRoot, "status-"));
  const home = path.join(runtime, "boxteam-home");
  const stateDir = path.join(home, "state");
  const bin = path.join(runtime, "bin");
  mkdirSync(stateDir, { recursive: true });
  mkdirSync(bin, { recursive: true });
  writeFileSync(path.join(stateDir, "development-ready.json"), JSON.stringify({ pid: 4141 }));
  const systemctl = path.join(bin, "systemctl");
  writeFileSync(
    systemctl,
    "#!/bin/sh\nprintf 'LoadState=loaded\\nActiveState=active\\nSubState=running\\nMainPID=4141\\n'\n",
  );
  chmodSync(systemctl, 0o755);
  const statusEnvironment = {
    STATUS_TEST_ABORT_MARKER: path.join(runtime, "abort.marker"),
    STATUS_TEST_CANCEL_MARKER: path.join(runtime, "cancel.marker"),
  };
  const arguments_ = [path.resolve(process.cwd(), "scripts/launch/dev-systemd.mjs"), "status"];
  if (preloadSource) {
    const preloadPath = path.join(runtime, "status-preload.mjs");
    writeFileSync(preloadPath, preloadSource);
    arguments_.unshift("--preload", preloadPath);
  }

  const observed = { requests: 0, closedResponses: 0, closedSockets: 0 };
  let resolveResponseClosed;
  const responseClosed = new Promise((resolve) => { resolveResponseClosed = resolve; });
  let resolveSocketClosed;
  const socketClosed = new Promise((resolve) => { resolveSocketClosed = resolve; });
  const server = createServer((request, response) => {
    observed.requests += 1;
    request.socket.once("close", () => {
      observed.closedSockets += 1;
      resolveSocketClosed();
    });
    response.once("close", () => {
      observed.closedResponses += 1;
      resolveResponseClosed();
    });
    respond(request, response, observed);
  });
  let child;
  let childClosed;
  try {
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    const portOffset = server.address().port - 8014;
    expect(portOffset).toBeGreaterThanOrEqual(0);
    expect(portOffset).toBeLessThanOrEqual(57000);

    child = spawn(
      process.execPath,
      arguments_,
      {
        cwd: process.cwd(),
        env: {
          ...process.env,
          BOXTEAM_PROJECT_ROOT: process.cwd(),
          BOXTEAM_HOME: home,
          BOXTEAM_DEV_PORT_OFFSET: String(portOffset),
          BOXTEAM_DEV_LOCAL_ONLY: "1",
          ...statusEnvironment,
          PATH: `${bin}${path.delimiter}${process.env.PATH ?? ""}`,
        },
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8").on("data", (chunk) => { stdout += chunk; });
    child.stderr.setEncoding("utf8").on("data", (chunk) => { stderr += chunk; });
    childClosed = new Promise((resolve) => child.once("close", (code, signal) => resolve({ code, signal })));
    await exercise({
      child,
      childClosed,
      observed,
      responseClosed,
      socketClosed,
      runtime,
      readOutput: () => ({ stdout, stderr }),
    });
  } finally {
    if (child && child.exitCode === null) child.kill("SIGTERM");
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    if (child && child.exitCode === null) await childClosed;
    rmSync(runtime, { recursive: true, force: true });
  }
}

function waitForChild(childClosed, timeoutMs) {
  return Promise.race([
    childClosed.then((result) => ({ timedOut: false, ...result })),
    Bun.sleep(timeoutMs).then(() => ({ timedOut: true })),
  ]);
}

describe("源码开发运行目录", () => {
  test("默认把 BOXTEAM_HOME 放入当前 worktree 的 out", () => {
    const layout = resolveDevelopmentLayout({
      cwd: fixtureProjectRoot,
      environment: {},
    });

    expect(layout.boxteamHome).toBe(
      fixtureDefaultBoxteamHome,
    );
    expect(layout.defaultWorkspaceRoot).toBe(
      path.join(fixtureDefaultBoxteamHome, "boxteam_workspace"),
    );
    expect(layout.ports.frontend).toBe(8011);
  });

  test("显式 BOXTEAM_HOME 和端口偏移保持优先", () => {
    const layout = resolveDevelopmentLayout({
      cwd: fixtureProjectRoot,
      environment: {
        BOXTEAM_HOME: fixtureExplicitBoxteamHome,
        BOXTEAM_DEV_PORT_OFFSET: "16",
      },
    });

    expect(layout.boxteamHome).toBe(fixtureExplicitBoxteamHome);
    expect(layout.ports.frontend).toBe(8027);
    expect(layout.ports.gateway).toBe(8030);
  });

  test("不同 worktree 使用不同 transient unit", () => {
    const first = resolveDevelopmentLayout({ cwd: path.join(fixtureRoot, "worktrees", "a"), environment: {} });
    const second = resolveDevelopmentLayout({ cwd: path.join(fixtureRoot, "worktrees", "b"), environment: {} });

    expect(developmentSystemdUnitName(first)).not.toBe(
      developmentSystemdUnitName(second),
    );
  });
});

describe("transient systemd 启动", () => {
  test("使用 collect、Restart=no 和仓库内 BOXTEAM_HOME", () => {
    const unit = buildTransientDevelopmentUnit({
      cwd: fixtureProjectRoot,
      bunExecutable: path.join(fixtureRoot, "bin", "bun"),
      environment: {
        HOME: path.join(fixtureRoot, "用户 home"),
        PATH: [path.join(fixtureRoot, "local", "bin"), path.join(fixtureRoot, "bin")].join(path.delimiter),
        SECRET_SHOULD_NOT_LEAK: "secret",
      },
    });

    expect(unit.systemdRunArguments).toContain("--user");
    expect(unit.systemdRunArguments).toContain("--collect");
    expect(unit.systemdRunArguments).toContain("--property=Restart=no");
    expect(unit.systemdRunArguments).toContain(
      `--setenv=BOXTEAM_HOME=${fixtureDefaultBoxteamHome}`,
    );
    expect(unit.systemdRunArguments).toContain(
      "--setenv=BOXTEAM_DEV_LOCAL_ONLY=1",
    );
    expect(unit.readyFile).toBe(
      path.join(fixtureDefaultBoxteamHome, "state", "development-ready.json"),
    );
    expect(unit.systemdRunArguments).toContain(
      `--setenv=BOXTEAM_DEV_READY_FILE=${unit.readyFile}`,
    );
    expect(unit.systemdRunArguments.join("\n")).not.toContain(
      "SECRET_SHOULD_NOT_LEAK",
    );
  });
});

describe("transient systemd 状态观测", () => {
  test.skipIf(process.platform !== "linux")("Gateway 无响应头时在观测预算内返回并释放连接", async () => {
    await withStatusProbe(() => {}, async ({ child, childClosed, observed, socketClosed, readOutput }) => {
      const result = await waitForChild(childClosed, 5_000);
      expect(result.timedOut).toBe(false);
      expect(result.code).toBe(1);
      expect(observed.requests).toBe(1);
      await Promise.race([socketClosed, Bun.sleep(500)]);
      expect(observed.closedSockets).toBe(1);
      const output = readOutput();
      expect(output.stderr).toBe("");
      expect(JSON.parse(output.stdout)).toMatchObject({
        activeState: "active",
        manager_ready: true,
        gateway_ready: false,
      });
      expect(child.exitCode).toBe(1);
    });
  });

  test.skipIf(process.platform !== "linux")("status 取消健康响应中未消费的正文", async () => {
    await withStatusProbe((_request, response) => {
      response.writeHead(200, { "content-type": "application/json" });
      response.write("{\"ok\":true}");
    }, async ({ childClosed, observed, responseClosed, socketClosed, readOutput }) => {
      const result = await waitForChild(childClosed, 5_000);
      expect(result.timedOut).toBe(false);
      expect(result.code).toBe(0);
      expect(observed.requests).toBe(1);
      await Promise.race([responseClosed, socketClosed, Bun.sleep(500)]);
      expect(observed.closedSockets).toBe(1);
      expect(JSON.parse(readOutput().stdout)).toMatchObject({ gateway_ready: true });
    });
  });

  test.skipIf(process.platform !== "linux")("观察期限后到达的成功响应不报告 Gateway ready", async () => {
    const preloadSource = `import { writeFileSync } from "node:fs";
globalThis.fetch = async (_url, { signal }) => {
  signal.addEventListener("abort", () => writeFileSync(process.env.STATUS_TEST_ABORT_MARKER, "aborted"), { once: true });
  await new Promise((resolve) => setTimeout(resolve, 2_100));
  return { ok: true, body: { async cancel() { writeFileSync(process.env.STATUS_TEST_CANCEL_MARKER, "cancelled"); } } };
};
`;
    await withStatusProbe(() => {}, async ({ childClosed, readOutput, runtime }) => {
      const result = await waitForChild(childClosed, 5_000);
      expect(result.timedOut).toBe(false);
      expect(result.code).toBe(1);
      expect(existsSync(path.join(runtime, "abort.marker"))).toBe(true);
      expect(existsSync(path.join(runtime, "cancel.marker"))).toBe(true);
      expect(JSON.parse(readOutput().stdout)).toMatchObject({ manager_ready: true, gateway_ready: false });
    }, { preloadSource });
  });
});
