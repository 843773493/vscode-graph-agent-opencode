import { spawnSync } from "node:child_process";
import { existsSync, lstatSync, mkdirSync, readFileSync, realpathSync, renameSync, writeFileSync } from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { Lock, openLock } from "@lickle/lock";

export function git(cwd, args, { input, allowFailure = false, environment = {} } = {}) {
  const env = { ...process.env, ...environment };
  for (const name of ["GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"]) delete env[name];
  Object.assign(env, environment);
  const result = spawnSync("git", ["--literal-pathspecs", "-C", cwd, ...args], {
    env, input, maxBuffer: 64 * 1024 * 1024, windowsHide: true, timeout: 120_000,
  });
  if (result.error) throw result.error;
  if (!allowFailure && (result.status !== 0 || result.signal)) {
    throw new Error(`Git 失败: cwd=${cwd} args=${JSON.stringify(args)} exit=${result.status} signal=${result.signal}\n${result.stderr}`);
  }
  return result;
}

export function gitText(cwd, args) {
  return git(cwd, args).stdout.toString("utf8").trimEnd();
}

export function registeredWorktrees(cwd) {
  const records = gitText(cwd, ["worktree", "list", "--porcelain", "-z"]).split("\0");
  return records.filter((record) => record.startsWith("worktree ")).map((record) => record.slice(9));
}

export function repositoryContext(cwd = process.cwd()) {
  const checkout = realpathSync(gitText(cwd, ["rev-parse", "--show-toplevel"]));
  const root = realpathSync(registeredWorktrees(checkout)[0]);
  return { root, checkout, pool: path.join(root, "out", "worktrees") };
}

export function commitFor(checkout, ref) {
  return gitText(checkout, ["rev-parse", "--verify", "--end-of-options", `${ref}^{commit}`]);
}

export function assertName(name) {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(name ?? "")) throw new Error(`任务名非法: ${name}`);
  return name;
}

export function relativeFile(name) {
  if (typeof name !== "string" || !name || name.includes("\\") || /[\0\r\n]/.test(name) ||
      path.isAbsolute(name) || name.split("/").some((part) => !part || part === "." || part === ".." || part === ".git")) {
    throw new Error(`必须明确选择仓库内文件: ${name}`);
  }
  return name;
}

export function assertNoLinkedParents(root, target) {
  const relative = path.relative(root, target);
  if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) throw new Error(`路径越界: ${target}`);
  let current = root;
  for (const segment of relative.split(path.sep).filter(Boolean)) {
    current = path.join(current, segment);
    const entry = lstatSync(current, { throwIfNoEntry: false });
    if (entry?.isSymbolicLink()) throw new Error(`拒绝通过符号链接写入: ${current}`);
  }
}

export function atomicJson(file, value) {
  mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.${randomUUID()}.tmp`;
  writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, { flag: "wx", mode: 0o600 });
  renameSync(temporary, file);
}

export function metadataPath(checkout) {
  return path.join(gitText(checkout, ["rev-parse", "--absolute-git-dir"]), "boxteam-worktree.json");
}

export function readMetadata(checkout) {
  const file = metadataPath(checkout);
  if (!existsSync(file)) return null;
  const record = JSON.parse(readFileSync(file, "utf8"));
  if (record.schema_version !== 1 || !["dev", "release", "experiment"].includes(record.role)) throw new Error(`worktree metadata 非法: ${file}`);
  return record;
}

export function requireManaged(context, target) {
  if (typeof target !== "string" || !target) throw new Error("必须指定受管 worktree 目标");
  const selected = target === "dev" || target === "release" ? path.join(context.pool, target) : path.resolve(target);
  assertNoLinkedParents(context.root, selected);
  if (selected === context.root || !registeredWorktrees(context.root).includes(selected)) throw new Error(`不是本仓库的受管 worktree: ${selected}`);
  const metadata = readMetadata(selected);
  if (!metadata || metadata.primary_root !== context.root) throw new Error(`worktree 缺少匹配的管理记录: ${selected}`);
  return { target: selected, metadata };
}

export async function withPoolLock(context, action) {
  assertNoLinkedParents(context.root, context.pool);
  mkdirSync(context.pool, { recursive: true });
  const guard = await openLock(path.join(context.pool, ".operations.lock"), Lock.Exclusive, { timeout: 30_000 });
  try { return await action(); }
  finally { await guard.drop(); }
}

export function artifactDirectory(context, name, now = new Date()) {
  const date = now.toISOString().slice(0, 10).replaceAll("-", "/");
  const directory = path.join(context.root, "out/tests/temp", date, assertName(name), "artifacts", randomUUID());
  mkdirSync(directory, { recursive: true });
  return directory;
}
