import { createHash } from "node:crypto";
import path from "node:path";

export const DEVELOPMENT_BASE_PORTS = Object.freeze({
  backend: 8010,
  frontend: 8011,
  terminalBackend: 8012,
  terminalFrontend: 8013,
  gateway: 8014,
  browserBackend: 8015,
  browserFrontend: 8016,
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
} = {}) {
  const projectRoot = path.resolve(
    environment.BOXTEAM_PROJECT_ROOT?.trim() || cwd,
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
 * 计算清理后仍未释放的开发端口。启动前必须据此响亮失败，禁止把端口占用
 * 拖成服务就绪超时。listenerPids 返回给定端口的监听 PID 列表。
 */
export function remainingOccupiedPorts(targetPorts, listenerPids) {
  const occupied = [];
  for (const port of targetPorts) {
    const pids = listenerPids(port);
    if (pids.length > 0) occupied.push({ port, pids });
  }
  return occupied;
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
