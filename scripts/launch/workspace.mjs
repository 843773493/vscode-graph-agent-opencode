import path from "node:path";
import { spawnSync } from "node:child_process";
import { parseArgs } from "node:util";
import { parseDevelopmentPortOffset } from "./dev-environment.mjs";
import { repositoryContext, requireManaged } from "./worktree/repository.mjs";

function parseArguments(args) {
  const { values, positionals } = parseArgs({ args, allowPositionals: true,
    options: {
      home: { type: "string" },
      "backend-root": { type: "string" },
    } });
  const [target, action, ...rest] = positionals;
  const homeOverride = values.home;
  const backendRoot = values["backend-root"];
  if (!new Set(["root", "dev", "experiment"]).has(target) ||
      !new Set(["start", "stop", "status"]).has(action) || rest.length > 1) {
    throw new Error("用法: bun scripts/launch/workspace.mjs <root|dev|experiment> <start|stop|status> [experiment-path]");
  }
  if ((target === "experiment") !== (rest.length === 1)) {
    throw new Error("experiment 必须提供已登记的实验 worktree 路径；root/dev 不接受路径");
  }
  if (homeOverride !== undefined && (!homeOverride || target !== "experiment")) {
    throw new Error("--home 只允许为实验 worktree 显式指定非空 Home");
  }
  if (backendRoot !== undefined && (!backendRoot.trim() || target === "experiment")) {
    throw new Error("--backend-root 需要非空源码路径；experiment 使用已指定的实验源码");
  }
  return { target, action, experimentPath: rest[0], homeOverride, backendRoot };
}

export function resolveWorkspaceTarget({ target, experimentPath, homeOverride, backendRoot, cwd = process.cwd(), environment = process.env }) {
  const context = repositoryContext(cwd);
  let checkout;
  let role;
  let portOffset = 0;
  if (target === "root" || backendRoot) {
    checkout = backendRoot ? path.resolve(cwd, backendRoot) : context.root;
    role = backendRoot ? "custom" : "root";
    portOffset = parseDevelopmentPortOffset(environment.BOXTEAM_DEV_PORT_OFFSET);
  } else {
    const selected = target === "dev" ? "dev" : experimentPath;
    const managed = requireManaged(context, selected);
    checkout = managed.target;
    role = managed.metadata.role;
    if (managed.metadata.archived) throw new Error(`归档 worktree 不可启动: ${checkout}`);
    portOffset = managed.metadata.port_offset;
    if (target === "dev" && role !== "dev") throw new Error(`固定 dev 角色不匹配: ${checkout}`);
    if (target === "experiment" && role !== "experiment") throw new Error(`目标不是实验 worktree: ${checkout}`);
    if (!Number.isSafeInteger(portOffset) || portOffset < 0 || portOffset > 57000) {
      throw new Error(`worktree port_offset 非法: ${String(portOffset)}`);
    }
  }

  if (homeOverride && role !== "experiment") throw new Error("只有实验 worktree 支持 --home");
  const isolatedHome = path.resolve(homeOverride ?? path.join(checkout, "out", "development-runtime", "boxteam-home"));
  if (environment.BOXTEAM_HOME?.trim() && path.resolve(environment.BOXTEAM_HOME) !== isolatedHome) {
    throw new Error(`${role} 实例拒绝外部 BOXTEAM_HOME: expected=${isolatedHome}`);
  }
  const expectedWorkspace = path.join(isolatedHome, "boxteam_workspace");
  if (environment.BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT?.trim() &&
      path.resolve(environment.BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT) !== expectedWorkspace) {
    throw new Error(`${role} 实例拒绝外部 BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT: expected=${expectedWorkspace}`);
  }
  const environmentForTarget = {
    ...environment,
    BOXTEAM_PROJECT_ROOT: checkout,
    BOXTEAM_FRONTEND_ROOT: role === "experiment" ? checkout : context.root,
    BOXTEAM_HOME: isolatedHome,
    BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT: expectedWorkspace,
    BOXTEAM_DEV_PORT_OFFSET: String(portOffset),
  };
  return { context, checkout, frontendRoot: environmentForTarget.BOXTEAM_FRONTEND_ROOT,
    role, portOffset, environment: environmentForTarget };
}

export function main(args = process.argv.slice(2), cwd = process.cwd(), environment = process.env, run = spawnSync) {
  const parsed = parseArguments(args);
  const selected = resolveWorkspaceTarget({ ...parsed, cwd, environment });
  const command = [
    process.execPath,
    path.join(selected.frontendRoot, "scripts/launch/dev-manager.mjs"),
    parsed.action,
  ];
  const result = run(command[0], command.slice(1), {
    cwd: selected.frontendRoot,
    env: selected.environment,
    stdio: "inherit",
    windowsHide: true,
  });
  if (result.error) throw result.error;
  if (result.signal) throw new Error(`开发实例命令被信号终止: ${result.signal}`);
  if (result.status !== 0) process.exitCode = result.status ?? 1;
  return { ...selected, command, exitCode: result.status };
}

if (import.meta.main) main();
