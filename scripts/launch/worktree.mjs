import { parseArgs } from "node:util";
import { artifactDirectory, repositoryContext, requireManaged, withPoolLock } from "./worktree/repository.mjs";
import { copyLocalConfiguration, loadWorktreeSettings, setupWorktree } from "./worktree/configuration.mjs";
import { archiveWorktree, createWorktree, inventory } from "./worktree/lifecycle.mjs";
import { synchronize } from "./worktree/synchronization.mjs";

export async function main(args = process.argv.slice(2), cwd = process.cwd()) {
  const { values, positionals } = parseArgs({ args, allowPositionals: true, options: {
    "no-setup": { type: "boolean" }, scopes: { type: "string" }, "checked-references": { type: "boolean" },
  } });
  const [command, ...rest] = positionals;
  if (!command || command === "help") return { usage: [
    "create dev|release <base-ref> [--no-setup] [--scopes=python,root,web,workbench]",
    "create experiment <base-ref> <task> [--no-setup]", "setup <dev|path> [--scopes=...]",
    "sync <dev|path> <file...>", "list", "artifact <task>", "archive <dev|release|path> <task> --checked-references",
  ] };
  const context = repositoryContext(cwd);
  if (command === "list") { if (rest.length) throw new Error("list 不接受路径"); return inventory(context); }
  if (command === "artifact") { if (rest.length !== 1) throw new Error("artifact 需要任务名"); return { artifacts: artifactDirectory(context, rest[0]) }; }
  return withPoolLock(context, () => {
    const settings = loadWorktreeSettings(context.root);
    const scopes = values.scopes === undefined ? settings.setup_scopes : values.scopes.split(",").filter(Boolean);
    if (scopes.some((scope) => !["python", "root", "web", "workbench"].includes(scope))) throw new Error("未知依赖范围");
    if (command === "create") {
      const [role, ref, name] = rest;
      if (!ref || rest.length !== (role === "experiment" ? 3 : 2)) throw new Error("create 需要角色、明确 base-ref，实验还需要任务名");
      return createWorktree(context, role, ref, name, settings, { setup: !values["no-setup"], scopes });
    }
    const { target, metadata } = requireManaged(context, rest[0]);
    if (metadata.archived) throw new Error("归档 worktree 不接受开发操作");
    if (command === "sync") return synchronize(context, target, metadata, rest.slice(1));
    if (command === "setup") {
      if (rest.length !== 1) throw new Error("setup 只接受一个目标");
      const copied = metadata.role === "release" ? [] : copyLocalConfiguration(context, target, settings);
      setupWorktree(target, scopes);
      return { target, copied, scopes };
    }
    if (command === "archive") {
      if (rest.length !== 2 || !values["checked-references"]) throw new Error("归档前核对运行进程、脏状态及其它 Agent 引用，然后传入 --checked-references");
      return archiveWorktree(context, target, metadata, rest[1]);
    }
    throw new Error(`未知 worktree 命令: ${command}`);
  });
}

if (import.meta.main) {
  try { console.log(JSON.stringify(await main(), null, 2)); }
  catch (error) { console.error(error.stack); process.exitCode = 1; }
}
