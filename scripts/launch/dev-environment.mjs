import { createHash } from "node:crypto";
import path from "node:path";

export const DEVELOPMENT_BASE_PORTS = Object.freeze({
  backend: 8010,
  frontend: 8011,
  terminalFrontend: 8013,
  gateway: 8014,
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
