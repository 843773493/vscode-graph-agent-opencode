import path from "node:path";
import { existsSync, readFileSync, rmSync } from "node:fs";

import {
  developmentSystemdUnitName,
  resolveDevelopmentLayout,
} from "./dev-environment.mjs";

const FORWARDED_ENVIRONMENT_NAMES = Object.freeze([
  "HOME",
  "PATH",
  "BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT",
  "BOXTEAM_PYTHON_BIN",
  "NODE_BIN",
  "BOXTEAM_INSTALL_DEVELOPMENT_ASSETS",
]);

function normalizeLocalOnly(environment) {
  const value = environment.BOXTEAM_DEV_LOCAL_ONLY?.trim() || "1";
  if (!new Set(["0", "1"]).has(value)) {
    throw new Error(
      `BOXTEAM_DEV_LOCAL_ONLY 只允许 0 或 1，实际为 ${value}`,
    );
  }
  return value;
}

export function buildTransientDevelopmentUnit({
  environment = process.env,
  cwd = process.cwd(),
  bunExecutable = process.execPath,
} = {}) {
  const layout = resolveDevelopmentLayout({ environment, cwd });
  const unitName = developmentSystemdUnitName(layout);
  const serviceName = `${unitName}.service`;
  const readyFile = path.join(
    layout.boxteamHome,
    "state",
    "development-ready.json",
  );
  const serviceEnvironment = {
    BOXTEAM_PROJECT_ROOT: layout.projectRoot,
    BOXTEAM_HOME: layout.boxteamHome,
    BOXTEAM_DEV_PORT_OFFSET: String(layout.portOffset),
    BOXTEAM_DEV_LOCAL_ONLY: normalizeLocalOnly(environment),
    BOXTEAM_DEV_READY_FILE: readyFile,
  };
  for (const name of FORWARDED_ENVIRONMENT_NAMES) {
    const value = environment[name]?.trim();
    if (value) serviceEnvironment[name] = value;
  }
  const command = [
    path.resolve(bunExecutable),
    path.join(layout.projectRoot, "scripts", "launch", "dev.mjs"),
  ];
  const systemdRunArguments = [
    "--user",
    "--collect",
    `--unit=${unitName}`,
    `--description=BoxTeam transient development stack (${layout.projectRoot})`,
    "--service-type=exec",
    "--property=Restart=no",
    "--property=KillMode=control-group",
    "--property=TimeoutStopSec=20",
    `--working-directory=${layout.projectRoot}`,
    ...Object.entries(serviceEnvironment).map(
      ([name, value]) => `--setenv=${name}=${value}`,
    ),
    "--",
    ...command,
  ];
  return {
    layout,
    unitName,
    serviceName,
    serviceEnvironment,
    readyFile,
    systemdRunArguments,
    frontendUrl: `http://127.0.0.1:${layout.ports.frontend}`,
    gatewayHealthUrl: `http://127.0.0.1:${layout.ports.gateway}/api/gateway/health`,
  };
}

function runCommand(command, args, { allowFailure = false } = {}) {
  const result = Bun.spawnSync([command, ...args], {
    cwd: process.cwd(),
    env: process.env,
    stdout: "pipe",
    stderr: "pipe",
  });
  const stdout = result.stdout.toString().trim();
  const stderr = result.stderr.toString().trim();
  if (!allowFailure && result.exitCode !== 0) {
    throw new Error(
      `${command} ${args.join(" ")} 失败: exit=${String(result.exitCode)}${
        stderr ? `\n${stderr}` : ""
      }`,
    );
  }
  return { exitCode: result.exitCode, stdout, stderr };
}

function readUnitState(serviceName) {
  const result = runCommand(
    "systemctl",
    [
      "--user",
      "show",
      serviceName,
      "--property=LoadState",
      "--property=ActiveState",
      "--property=SubState",
      "--property=MainPID",
      "--no-pager",
    ],
    { allowFailure: true },
  );
  if (result.exitCode !== 0) {
    return { loadState: "not-found", activeState: "inactive", subState: "dead" };
  }
  const values = Object.fromEntries(
    result.stdout
      .split("\n")
      .map((line) => line.split("=", 2))
      .filter(([name, value]) => name && value !== undefined),
  );
  return {
    loadState: values.LoadState ?? "unknown",
    activeState: values.ActiveState ?? "unknown",
    subState: values.SubState ?? "unknown",
    mainPid: Number(values.MainPID ?? 0),
  };
}

function isManagerReady(unit, state) {
  if (!Number.isInteger(state.mainPid) || state.mainPid <= 0) return false;
  if (!existsSync(unit.readyFile)) return false;
  try {
    const payload = JSON.parse(readFileSync(unit.readyFile, "utf8"));
    return payload.pid === state.mainPid;
  } catch (error) {
    if (error instanceof SyntaxError) return false;
    throw error;
  }
}

async function waitForGateway(unit, timeoutMs = 120_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    const state = readUnitState(unit.serviceName);
    if (state.activeState === "failed") {
      throw new Error(
        `${unit.serviceName} 启动失败；查看日志: journalctl --user-unit=${unit.serviceName} -n 200 --no-pager`,
      );
    }
    if (!isManagerReady(unit, state)) {
      await Bun.sleep(250);
      continue;
    }
    try {
      const response = await fetch(unit.gatewayHealthUrl);
      if (response.ok) return;
      lastError = new Error(`HTTP ${response.status} ${response.statusText}`);
    } catch (error) {
      lastError = error;
    }
    await Bun.sleep(250);
  }
  throw new Error(
    `${unit.serviceName} 在 ${timeoutMs}ms 内未就绪: ${
      lastError instanceof Error ? lastError.message : String(lastError)
    }；查看日志: journalctl --user-unit=${unit.serviceName} -n 200 --no-pager`,
  );
}

async function start(unit) {
  const current = readUnitState(unit.serviceName);
  if (current.activeState === "active" || current.activeState === "activating") {
    await waitForGateway(unit);
    process.stdout.write(
      `[dev-systemd] transient unit 已在运行: unit=${unit.serviceName} ` +
        `frontend=${unit.frontendUrl} boxteam_home=${unit.layout.boxteamHome}\n`,
    );
    return;
  }
  if (current.activeState === "failed") {
    runCommand("systemctl", ["--user", "reset-failed", unit.serviceName]);
  }
  rmSync(unit.readyFile, { force: true });
  const launched = runCommand("systemd-run", unit.systemdRunArguments);
  if (launched.stdout) process.stdout.write(`${launched.stdout}\n`);
  await waitForGateway(unit);
  process.stdout.write(
    `[dev-systemd] transient unit ready: unit=${unit.serviceName} ` +
      `frontend=${unit.frontendUrl} boxteam_home=${unit.layout.boxteamHome}\n`,
  );
}

function stop(unit) {
  const current = readUnitState(unit.serviceName);
  if (current.loadState === "not-found" || current.activeState === "inactive") {
    process.stdout.write(
      `[dev-systemd] transient unit 未运行: unit=${unit.serviceName}\n`,
    );
    return;
  }
  runCommand("systemctl", ["--user", "stop", unit.serviceName]);
  rmSync(unit.readyFile, { force: true });
  process.stdout.write(
    `[dev-systemd] transient unit 已停止: unit=${unit.serviceName}\n`,
  );
}

async function status(unit) {
  const state = readUnitState(unit.serviceName);
  const managerReady = isManagerReady(unit, state);
  let gatewayReady = false;
  if (managerReady && state.activeState === "active") {
    try {
      gatewayReady = (await fetch(unit.gatewayHealthUrl)).ok;
    } catch {
      gatewayReady = false;
    }
  }
  process.stdout.write(
    `${JSON.stringify({
      unit: unit.serviceName,
      ...state,
      manager_ready: managerReady,
      gateway_ready: gatewayReady,
      frontend: unit.frontendUrl,
      boxteam_home: unit.layout.boxteamHome,
    })}\n`,
  );
  if (!gatewayReady) process.exitCode = 1;
}

export async function main(args = process.argv.slice(2)) {
  if (process.platform !== "linux") {
    throw new Error(
      "transient systemd 开发启动仅支持 Linux；其他平台请运行 bun run dev:foreground",
    );
  }
  const action = args[0] ?? "start";
  if (!new Set(["start", "stop", "status"]).has(action)) {
    throw new Error(`未知 dev systemd 操作: ${action}`);
  }
  const unit = buildTransientDevelopmentUnit();
  if (action === "start") await start(unit);
  if (action === "stop") stop(unit);
  if (action === "status") await status(unit);
}

if (import.meta.main) {
  await main();
}
