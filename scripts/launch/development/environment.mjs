import { createHash } from "node:crypto";
import { createServer } from "node:net";
import path from "node:path";
import { parseArgs } from "node:util";

export const DEVELOPMENT_BASE_PORTS = Object.freeze({
  backend: 8010,
  frontend: 8011,
  gateway: 8014,
  backendDebug: 8002,
});

export function parseDevelopmentPortOffset(rawValue) {
  const value = rawValue?.trim() ?? "";
  if (value === "") return 0;
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 0 || parsed > 57000) {
    throw new Error(
      `BOXTEAM_DEV_PORT_OFFSET 必须是 0 到 57000 的整数，实际为 ${value}`,
    );
  }
  return parsed;
}

export function resolveDevelopmentLayout({
  environment = process.env,
  cwd = process.cwd(),
  args = [],
} = {}) {
  const { values } = parseArgs({ args, strict: false, allowPositionals: true,
    options: {
      "backend-root": { type: "string" },
    } });
  if (values["backend-root"] !== undefined && !values["backend-root"].trim()) {
    throw new Error("--backend-root 必须指定后端源码目录");
  }
  const frontendRoot = path.resolve(cwd,
    environment.BOXTEAM_FRONTEND_ROOT?.trim() || environment.BOXTEAM_PROJECT_ROOT?.trim() || cwd);
  const projectRoot = path.resolve(
    cwd, values["backend-root"] ?? (environment.BOXTEAM_PROJECT_ROOT?.trim() || cwd),
  );
  const portOffset = parseDevelopmentPortOffset(
    environment.BOXTEAM_DEV_PORT_OFFSET,
  );
  const configuredBoxteamHome = environment.BOXTEAM_HOME?.trim();
  const boxteamHome = path.resolve(
    configuredBoxteamHome ||
      path.join(projectRoot, "out", "development-runtime", "boxteam-home"),
  );
  const configuredWorkspaceRoot =
    environment.BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT?.trim();
  const defaultWorkspaceRoot = path.resolve(
    configuredWorkspaceRoot || path.join(boxteamHome, "boxteam_workspace"),
  );
  const ports = Object.fromEntries(
    Object.entries(DEVELOPMENT_BASE_PORTS).map(([name, port]) => [
      name,
      port + portOffset,
    ]),
  );
  return {
    projectRoot,
    frontendRoot,
    boxteamHome,
    defaultWorkspaceRoot,
    portOffset,
    ports,
  };
}

export function developmentSystemdUnitName(layout) {
  const digest = createHash("sha256")
    .update(layout.projectRoot)
    .update("\0")
    .update(layout.boxteamHome)
    .update("\0")
    .update(String(layout.portOffset))
    .digest("hex")
    .slice(0, 16);
  return `boxteam-dev-${digest}`;
}

/**
 * 计算当前仍被占用的开发端口。启动前必须据此响亮失败，禁止将未经确认
 * 归属的监听进程当作本实例回收。listenerPids 返回给定端口的监听 PID 列表。
 */
export function remainingOccupiedPorts(targetPorts, listenerPids) {
  const occupied = [];
  for (const port of targetPorts) {
    const pids = listenerPids(port);
    if (pids.length > 0) occupied.push({ port, pids });
  }
  return occupied;
}

export async function assertDevelopmentPortsBindable(targetPorts, host = "127.0.0.1") {
  const failures = [];
  for (const port of targetPorts) {
    const server = createServer();
    let failure = null;
    try {
      await new Promise((resolve, reject) => {
        server.once("error", reject);
        server.listen({ host, port, exclusive: true }, () => {
          server.removeAllListeners("error");
          server.close((error) => error ? reject(error) : resolve());
        });
      });
    } catch (error) {
      failure = error;
      server.removeAllListeners("error");
      if (server.listening) {
        await new Promise((resolve, reject) => server.close((closeError) => closeError ? reject(closeError) : resolve()));
      }
    }
    if (failure) {
      failures.push({ port, code: failure.code ?? "UNKNOWN", message: failure.message });
    }
  }
  if (failures.length) {
    throw new Error(
      `开发服务无法绑定监听地址 host=${host}: ` +
        failures.map(({ port, code, message }) => `port=${port} code=${code} detail=${message}`).join("; "),
    );
  }
}

/** 直接消费 Workspace 唯一 CLI 的一行 UUID；不要另设 JSON 输出链路。 */
export function resolveBackendWorkspaceId({
  environment = process.env, projectRoot, workspaceRoot, pythonExecutable,
}) {
  const result = Bun.spawnSync([
    pythonExecutable, "-m", "app.core.workspace_identity",
    "--workspace-root", workspaceRoot,
  ], { cwd: projectRoot, env: environment, stdout: "pipe", stderr: "pipe" });
  const stdout = result.stdout.toString();
  const stderr = result.stderr.toString();
  if (result.exitCode !== 0) {
    throw new Error(
      `Workspace backend UUID CLI 初始化失败: exit=${String(result.exitCode)} stderr=${stderr}`,
    );
  }
  if (stderr !== "") {
    throw new Error(`Workspace backend UUID CLI 成功时 stderr 必须为空: ${stderr}`);
  }
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\r?\n$/.test(stdout)) {
    throw new Error(`Workspace backend UUID CLI 必须只输出一行标准 UUID: ${stdout}`);
  }
  return stdout.replace(/\r?\n$/, "");
}
