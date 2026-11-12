import { existsSync, mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { assertName, assertNoLinkedParents, atomicJson, commitFor, git, gitText, metadataPath, readMetadata, registeredWorktrees } from "./repository.mjs";
import { copyLocalConfiguration, setupWorktree } from "./configuration.mjs";
import { developmentSystemdUnitName, parseDevelopmentPortOffset, resolveDevelopmentLayout } from "../dev-environment.mjs";
import { readUnitState } from "../dev-systemd.mjs";

export function assertSystemdUnitIdle(target, metadata, { readState = readUnitState, platform = process.platform } = {}) {
  if (platform !== "linux") return null;
  const home = path.join(target, "out", "development-runtime", "boxteam-home");
  const layout = resolveDevelopmentLayout({
    environment: {
      BOXTEAM_PROJECT_ROOT: target,
      BOXTEAM_HOME: home,
      BOXTEAM_DEV_PORT_OFFSET: String(Number.isSafeInteger(metadata.port_offset) ? metadata.port_offset : 0),
    },
    cwd: target,
  });
  const unit = developmentSystemdUnitName(layout);
  const state = readState(unit);
  if (state.loadState === "not-found") return { unit, state };
  if (state.loadState !== "loaded" || !["inactive", "failed"].includes(state.activeState)) {
    throw new Error(`worktree systemd 实例仍活动或状态未知: unit=${unit} load=${state.loadState} active=${state.activeState} sub=${state.subState}`);
  }
  return { unit, state };
}

export function createWorktree(context, role, ref, name, settings, { setup = true, scopes = settings.setup_scopes } = {}) {
  if (!["dev", "release", "experiment"].includes(role)) throw new Error(`未知 worktree 角色: ${role}`);
  const sha = commitFor(context.checkout, ref);
  const created = new Date().toISOString();
  const target = role === "experiment"
    ? path.join(context.pool, created.slice(0, 10).replaceAll("-", "/"), assertName(name))
    : path.join(context.pool, role);
  assertNoLinkedParents(context.root, target);
  if (existsSync(target)) throw new Error(`目标已存在，使用 setup 重试初始化或先检查归档: ${target}`);
  const offsets = registeredWorktrees(context.root).map((directory) => readMetadata(directory)).filter(Boolean).filter((record) => !record.archived).map((record) => record.port_offset);
  let offset = role === "dev" ? settings.dev_port_offset : settings.experiment_port_offset_start;
  if (role === "experiment") while (offsets.some((value) => Math.abs(value - offset) < 20)) offset += settings.experiment_port_stride;
  if (role !== "release" && (offset > 57000 || offsets.some((value) => Math.abs(value - offset) < 20))) throw new Error("端口偏移无法分配，请检查 worktree_local.jsonc");
  mkdirSync(path.dirname(target), { recursive: true });
  git(context.root, ["worktree", "add", ...(role === "release" ? ["--detach"] : ["-b", `codex/${role}-${name || role}-${randomUUID().slice(0, 8)}`]), target, sha]);
  atomicJson(metadataPath(target), { schema_version: 1, role, primary_root: context.root, base_commit: sha, created_at: created, port_offset: role === "release" ? null : offset });
  const copied = role === "release" ? [] : copyLocalConfiguration(context, target, settings);
  if (setup) setupWorktree(target, scopes);
  return { target, role, commit: sha, port_offset: role === "release" ? null : offset, copied, setup };
}

export function inventory(context) {
  return registeredWorktrees(context.root).map((target) => {
    const metadata = readMetadata(target);
    const layout = metadata?.role === "release" || (!metadata && target !== context.root) ? null : resolveDevelopmentLayout({
      environment: {
        BOXTEAM_PROJECT_ROOT: target,
        BOXTEAM_HOME: path.join(target, "out", "development-runtime", "boxteam-home"),
        BOXTEAM_DEV_PORT_OFFSET: String(metadata?.port_offset ?? parseDevelopmentPortOffset(process.env.BOXTEAM_DEV_PORT_OFFSET)),
      }, cwd: target,
    });
    return { target, metadata,
    commit: gitText(target, ["rev-parse", "HEAD"]), dirty: !!gitText(target, ["status", "--porcelain"]),
    systemd_unit: layout ? developmentSystemdUnitName(layout) : null,
    runtime_records: ["out/development-runtime/detached-ready.json", "out/development-runtime/manager-operation.lock", "out/development-runtime/boxteam-home/state/launcher.lock"].filter((file) => existsSync(path.join(target, file))),
  }; });
}

export function archiveWorktree(context, target, metadata, name, { assertIdle = assertSystemdUnitIdle } = {}) {
  if (metadata.archived) throw new Error("该 worktree 已归档");
  // 未知进程与其它任务引用无法靠 Git 判断；必须先由调用者明确核验，再通过 --checked-references。
  const runtime = path.join(target, "out/development-runtime");
  for (const file of ["detached-ready.json", "manager-operation.lock", "boxteam-home/state/launcher.lock"]) {
    const recordPath = path.join(runtime, file);
    if (!existsSync(recordPath)) continue;
    const record = JSON.parse(readFileSync(recordPath, "utf8"));
    if (!Number.isInteger(record.pid) || record.pid <= 0) throw new Error(`无法核对运行实例: ${recordPath}`);
    try { process.kill(record.pid, 0); }
    catch (error) { if (error.code === "ESRCH") continue; throw error; }
    throw new Error(`worktree 仍有活动实例: pid=${record.pid}`);
  }
  assertIdle(target, metadata);
  const date = new Date().toISOString().slice(0, 10).replaceAll("-", "/");
  const destination = path.join(context.pool, date, `archive-${assertName(name)}-${randomUUID().slice(0, 8)}`);
  assertNoLinkedParents(context.root, destination);
  mkdirSync(path.dirname(destination), { recursive: true });
  git(context.root, ["worktree", "move", target, destination]);
  atomicJson(metadataPath(destination), { ...metadata, archived: true, archived_at: new Date().toISOString(), previous_path: target });
  return { target: destination, retained: "全部提交、暂存、未提交与忽略文件；未删除分支或数据", note: "启动前须显式恢复路径及核对含绝对路径的本地配置" };
}
