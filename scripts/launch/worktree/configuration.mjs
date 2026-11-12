import { spawnSync } from "node:child_process";
import { chmodSync, copyFileSync, existsSync, lstatSync, mkdirSync, readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import Ajv from "ajv";
import { parse, printParseErrorCode } from "jsonc-parser";
import { assertNoLinkedParents, git, gitText, relativeFile } from "./repository.mjs";

function readJsonc(file) {
  const errors = [];
  const value = parse(readFileSync(file, "utf8"), errors, { allowTrailingComma: true });
  if (errors.length) throw new Error(`JSONC 非法: ${file}: ${errors.map((error) => printParseErrorCode(error.error)).join(",")}`);
  return value;
}

function merge(base, override) {
  if (!override || typeof override !== "object" || Array.isArray(override)) return override;
  return Object.fromEntries(Object.entries({ ...base, ...override }).map(([key, value]) => [key,
    Object.hasOwn(override, key) && value && typeof value === "object" && !Array.isArray(value)
      ? merge(base?.[key], value) : value,
  ]));
}

export function loadWorktreeSettings(root) {
  const defaults = readJsonc(path.join(root, "configs/worktree_inline.jsonc"));
  const local = path.join(root, "configs/worktree_local.jsonc");
  const value = existsSync(local) ? merge(defaults, readJsonc(local)) : defaults;
  const validate = new Ajv({ allErrors: true }).compile(readJsonc(path.join(root, "configs/worktree_schema.jsonc")));
  if (!validate(value)) throw new Error(`worktree 配置校验失败: ${JSON.stringify(validate.errors)}`);
  for (const pattern of value.runtime.worktrees.local_config_patterns) relativeFile(pattern);
  return value.runtime.worktrees;
}

export function copyLocalConfiguration(context, target, settings) {
  const files = new Set();
  for (const pattern of settings.local_config_patterns) {
    for (const file of new Bun.Glob(pattern).scanSync({ cwd: context.root, dot: true, onlyFiles: true, followSymlinks: false })) files.add(file);
  }
  const selected = [];
  for (const file of [...files].sort()) {
    relativeFile(file);
    if (!/^\.env(?:\..+)?$/.test(path.basename(file)) && !file.endsWith("_local.jsonc")) throw new Error(`本地配置复制仅接受 .env、.env.* 或 *_local.jsonc: ${file}`);
    if (file.split("/").some((part) => [".boxteam", "out", ".venv", "node_modules"].includes(part))) throw new Error(`本地配置清单不得复制运行数据: ${file}`);
    if (git(context.root, ["ls-files", "--error-unmatch", "--", file], { allowFailure: true }).status === 0) continue;
    const source = path.join(context.root, file);
    assertNoLinkedParents(context.root, source);
    if (!lstatSync(source).isFile()) throw new Error(`配置必须是普通文件: ${file}`);
    const destination = path.join(target, file);
    assertNoLinkedParents(target, destination);
    if (existsSync(destination)) {
      if (!readFileSync(destination).equals(readFileSync(source))) throw new Error(`目标本地配置已存在且内容不同: ${file}`);
      continue;
    }
    selected.push({ file, source, destination });
  }
  for (const { source, destination } of selected) {
    mkdirSync(path.dirname(destination), { recursive: true });
    copyFileSync(source, destination, 1);
    chmodSync(destination, 0o600);
  }
  return selected.map(({ file }) => file);
}

export function setupWorktree(target, scopes, run = spawnSync) {
  const commands = [];
  if (scopes.includes("workbench")) commands.push(["git", ["submodule", "update", "--init", "--recursive"]]);
  if (scopes.includes("python")) commands.push(["uv", ["sync", "--frozen"]]);
  for (const [scope, directory] of [["root", "."], ["web", "src/clients/web"], ["workbench", "src/clients/workbench"]]) {
    if (scopes.includes(scope)) commands.push(["bun", ["install", "--cwd", directory, "--frozen-lockfile"]]);
  }
  for (const [command, args] of commands) {
    const result = run(command, args, { cwd: target, stdio: "inherit", windowsHide: true });
    if (result.error) throw result.error;
    if (result.status !== 0 || result.signal) throw new Error(`worktree 初始化失败，可重跑 setup: ${command} ${args.join(" ")} exit=${result.status} signal=${result.signal}`);
  }
  return commands;
}

export function assertCleanCheckout(target) {
  if (gitText(target, ["status", "--porcelain", "--untracked-files=all"]) !== "") throw new Error(`发布 checkout 存在未提交源码: ${target}`);
  if (gitText(target, ["submodule", "status", "--recursive"]).split("\n").some((line) => line && /^[+U-]/.test(line))) throw new Error(`发布子模块不匹配或未初始化: ${target}`);
  const candidates = readdirSync(target).filter((file) => /^\.env(?:\..+)?$/.test(file));
  const configDirectory = path.join(target, "configs");
  if (existsSync(configDirectory)) candidates.push(...readdirSync(configDirectory).filter((file) => file.endsWith("_local.jsonc")).map((file) => `configs/${file}`));
  if (candidates.some((file) => git(target, ["ls-files", "--error-unmatch", "--", file], { allowFailure: true }).status !== 0)) throw new Error(`发布 checkout 不得含开发本地配置: ${target}`);
}
