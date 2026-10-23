import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { spawn } from "node:child_process";
import path from "node:path";

import { buildTransientDevelopmentUnit } from "./dev-systemd.mjs";

const projectRoot = process.cwd();
const runId = `${new Date().toISOString().replaceAll(/[-:.TZ]/g, "")}-${process.pid}-${randomUUID()}`;
const fixtureRoot = path.resolve(projectRoot, "out/tests/scripts/launch/dev-systemd-state");
const workspaceRunRoot = path.join(fixtureRoot, "workspace", runId);
const artifactsRunRoot = path.join(fixtureRoot, "artifacts", runId);

async function withUnitStateProbe(action, scenario, response, exercise) {
  const workspace = path.join(workspaceRunRoot, scenario);
  const artifacts = path.join(artifactsRunRoot, scenario);
  const boxteamHome = path.join(workspace, "boxteam-home");
  const bin = path.join(workspace, "empty-bin");
  const callsPath = path.join(workspace, "spawn-calls.json");
  mkdirSync(workspaceRunRoot, { recursive: true });
  mkdirSync(artifactsRunRoot, { recursive: true });
  mkdirSync(workspace);
  mkdirSync(artifacts);
  mkdirSync(path.join(boxteamHome, "state"), { recursive: true });
  mkdirSync(bin);

  const unitEnvironment = {
    BOXTEAM_PROJECT_ROOT: projectRoot,
    BOXTEAM_HOME: boxteamHome,
    BOXTEAM_DEV_PORT_OFFSET: "0",
    BOXTEAM_DEV_LOCAL_ONLY: "1",
  };
  const unit = buildTransientDevelopmentUnit({
    cwd: projectRoot,
    environment: unitEnvironment,
    bunExecutable: process.execPath,
  });
  writeFileSync(unit.readyFile, JSON.stringify({ pid: 4141 }), { flag: "wx" });

  const expectedCommand = [
    "systemctl",
    "--user",
    "show",
    unit.serviceName,
    "--property=LoadState",
    "--property=ActiveState",
    "--property=SubState",
    "--property=MainPID",
    "--no-pager",
  ];
  const preloadPath = path.join(workspace, "systemctl-preload.mjs");
  writeFileSync(
    preloadPath,
    `import { writeFileSync } from "node:fs";
const calls = [];
const expected = JSON.parse(process.env.DEV_SYSTEMD_STATE_EXPECTED_COMMAND);
const response = JSON.parse(process.env.DEV_SYSTEMD_STATE_RESPONSE);
Bun.spawnSync = (command) => {
  calls.push(command);
  writeFileSync(process.env.DEV_SYSTEMD_STATE_CALLS, JSON.stringify(calls));
  if (JSON.stringify(command) !== JSON.stringify(expected)) {
    throw new Error("unexpected command vector: " + JSON.stringify(command));
  }
  return {
    exitCode: response.exitCode,
    signalCode: response.signalCode ?? null,
    stdout: Buffer.from(response.stdout ?? ""),
    stderr: Buffer.from(response.stderr ?? ""),
  };
};
`,
    { flag: "wx" },
  );

  const raw = {
    scenario,
    action,
    expectedCommand,
    response,
  };
  try {
    const child = spawn(
      process.execPath,
      ["--preload", preloadPath, path.join(projectRoot, "scripts/launch/dev-systemd.mjs"), action],
      {
        cwd: projectRoot,
        env: {
          ...process.env,
          ...unitEnvironment,
          HOME: path.join(workspace, "home"),
          PATH: bin,
          DEV_SYSTEMD_STATE_EXPECTED_COMMAND: JSON.stringify(expectedCommand),
          DEV_SYSTEMD_STATE_RESPONSE: JSON.stringify(response),
          DEV_SYSTEMD_STATE_CALLS: callsPath,
        },
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8").on("data", (chunk) => { stdout += chunk; });
    child.stderr.setEncoding("utf8").on("data", (chunk) => { stderr += chunk; });
    const childClosed = new Promise((resolve, reject) => {
      child.once("error", reject);
      child.once("close", (code, signal) => resolve({ code, signal }));
    });
    let closed = await Promise.race([
      childClosed,
      Bun.sleep(5_000).then(() => null),
    ]);
    let timedOut = false;
    if (closed === null) {
      timedOut = true;
      child.kill("SIGTERM");
      closed = await Promise.race([
        childClosed,
        Bun.sleep(500).then(() => null),
      ]);
      if (closed === null) {
        child.kill("SIGKILL");
        closed = await childClosed;
      }
    }
    raw.exitCode = closed.code;
    raw.signal = closed.signal;
    raw.timedOut = timedOut;
    raw.stdout = stdout;
    raw.stderr = stderr;
    raw.commands = existsSync(callsPath)
      ? JSON.parse(await Bun.file(callsPath).text())
      : [];
    raw.readyMarkerExists = existsSync(unit.readyFile);
    await exercise({ raw, unit });
  } catch (error) {
    raw.error = error instanceof Error ? error.message : String(error);
    throw error;
  } finally {
    raw.readyMarkerExists ??= existsSync(unit.readyFile);
    raw.commands ??= existsSync(callsPath)
      ? JSON.parse(await Bun.file(callsPath).text())
      : [];
    writeFileSync(
      path.join(artifacts, "raw.json"),
      `${JSON.stringify(raw, null, 2)}\n`,
      { flag: "wx" },
    );
    writeFileSync(
      path.join(artifacts, "result.json"),
      `${JSON.stringify({ scenario, completed: raw.error === undefined, error: raw.error ?? null }, null, 2)}\n`,
      { flag: "wx" },
    );
  }
}

describe("transient systemd unit 状态读取错误", () => {
  for (const action of ["start", "stop", "status"]) {
    test.skipIf(process.platform !== "linux")(`${action} 保留 systemctl bus 错误并中止后续操作`, async () => {
      const serviceError = "Failed to connect to bus: state test bus unavailable";
      await withUnitStateProbe(
        action,
        `bus-error-${action}`,
        {
          exitCode: 5,
          stdout: action === "status"
            ? "LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\n"
            : "",
          stderr: serviceError,
        },
        async ({ raw, unit }) => {
          expect(raw.timedOut).toBe(false);
          expect(raw.exitCode).toBe(1);
          expect(raw.signal).toBeNull();
          expect(raw.commands).toEqual([raw.expectedCommand]);
          expect(raw.stdout).toBe("");
          expect(raw.stderr).toContain(serviceError);
          expect(raw.stderr).toContain("systemctl --user show");
          expect(raw.stderr).toContain(unit.serviceName);
          expect(raw.stderr).toContain("exit=5");
          expect(raw.stderr).toContain("signal=none");
          expect(raw.readyMarkerExists).toBe(true);
        },
      );
    });
  }

  for (const action of ["stop", "status"]) {
    test.skipIf(process.platform !== "linux")(`${action} 仅对明确的 LoadState=not-found 保持既有行为`, async () => {
      await withUnitStateProbe(
        action,
        `explicit-not-found-${action}`,
        {
          exitCode: 1,
          stdout: "LoadState=not-found\nActiveState=inactive\nSubState=dead\nMainPID=0\n",
          stderr: "",
        },
        async ({ raw }) => {
          expect(raw.timedOut).toBe(false);
          expect(raw.commands).toEqual([raw.expectedCommand]);
          expect(raw.readyMarkerExists).toBe(true);
          if (action === "stop") {
            expect(raw.exitCode).toBe(0);
            expect(raw.stdout).toContain("transient unit 未运行");
            expect(raw.stderr).toBe("");
          } else {
            expect(raw.exitCode).toBe(1);
            expect(JSON.parse(raw.stdout)).toMatchObject({
              loadState: "not-found",
              activeState: "inactive",
              subState: "dead",
            });
            expect(raw.stderr).toBe("");
          }
        },
      );
    });
  }

  test.skipIf(process.platform !== "linux")("signal 中断的状态查询不会伪装成 unit 不存在", async () => {
    await withUnitStateProbe(
      "status",
      "signal-interrupted-show",
      {
        exitCode: null,
        signalCode: 15,
        stdout: "LoadState=not-found\nActiveState=inactive\nSubState=dead\nMainPID=0\n",
        stderr: "state query terminated by signal",
      },
      async ({ raw, unit }) => {
        expect(raw.timedOut).toBe(false);
        expect(raw.exitCode).toBe(1);
        expect(raw.commands).toEqual([raw.expectedCommand]);
        expect(raw.stdout).toBe("");
        expect(raw.stderr).toContain(unit.serviceName);
        expect(raw.stderr).toContain("exit=null");
        expect(raw.stderr).toContain("signal=15");
        expect(raw.stderr).toContain("state query terminated by signal");
      },
    );
  });
});
