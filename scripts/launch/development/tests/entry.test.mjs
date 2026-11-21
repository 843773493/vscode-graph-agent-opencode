import { expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { cpSync, mkdirSync, readFileSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

for (const split of [false, true]) {
  test(`原开发入口在加载消费者前构建 SDK，同根=${!split}`, () => {
    const base = path.resolve("out/tests/scripts/launch/development/tests/entry");
    const run = randomUUID();
    const root = path.join(base, "workspace", run, "frontend");
    const backend = split ? path.join(base, "workspace", run, "backend") : root;
    const artifacts = path.join(base, "artifacts", run);
    mkdirSync(artifacts, { recursive: true });
    mkdirSync(path.join(root, "scripts/launch/development"), { recursive: true });
    cpSync("scripts/launch/dev.mjs", path.join(root, "scripts/launch/dev.mjs"));
    cpSync("scripts/launch/development/environment.mjs", path.join(root, "scripts/launch/development/environment.mjs"));
    symlinkSync(path.resolve("node_modules"), path.join(root, "node_modules"), process.platform === "win32" ? "junction" : "dir");
    const roots = [...new Set([root, backend])];
    const originals = new Map();
    for (const target of roots) {
      cpSync("sdk/node", path.join(target, "sdk/node"), { recursive: true,
        filter: file => !["node_modules", "dist", "build"].includes(path.basename(file)) });
      const source = path.join(target, "sdk/node/src/index.ts");
      originals.set(target, readFileSync(source, "utf8"));
      writeFileSync(source, originals.get(target) + '\nexport const bootstrapProbe = "first";\n');
    }
    // 系统边界替身读取真实编译产物，不启动任何服务或接触现有实例。
    const consume = `const values = await Promise.all(${JSON.stringify(roots.map(target =>
      pathToFileURL(path.join(target, "sdk/node/dist/index.js")).href))}.map(async file => (await import(file)).bootstrapProbe));`;
    writeFileSync(path.join(root, "scripts/launch/development/manager.mjs"),
      `export async function main(args) { ${consume} console.log(JSON.stringify({ role: "manager", action: args[0], values })); }`);
    writeFileSync(path.join(root, "scripts/launch/development/services.mjs"),
      `${consume} console.log(JSON.stringify({ role: "services", values }));`);
    function invoke(label, args) {
      const command = [process.execPath, path.join(root, "scripts/launch/dev.mjs"), ...args];
      const result = spawnSync(command[0], command.slice(1), { cwd: root, encoding: "utf8", timeout: 10_000,
        env: { ...process.env, BOXTEAM_FRONTEND_ROOT: root, BOXTEAM_PROJECT_ROOT: backend } });
      writeFileSync(path.join(artifacts, `${label}.json`), JSON.stringify({ command, ...result }));
      expect(result.error).toBeUndefined();
      return result;
    }
    const first = invoke("start", ["start"]);
    expect(first.status, first.stderr).toBe(0);
    expect(JSON.parse(first.stdout)).toEqual({ role: "manager", action: "start", values: roots.map(() => "first") });
    const output = path.join(root, "sdk/node/dist/index.js");
    const before = statSync(output).mtimeMs;
    expect(invoke("unchanged", ["start"]).status).toBe(0);
    expect(statSync(output).mtimeMs).toBe(before);
    for (const target of roots) writeFileSync(path.join(target, "sdk/node/src/index.ts"),
      originals.get(target) + '\nexport const bootstrapProbe = "updated";\n');
    const updated = invoke("updated", ["start"]);
    expect(updated.status, updated.stderr).toBe(0);
    expect(JSON.parse(updated.stdout).values).toEqual(roots.map(() => "updated"));
    writeFileSync(path.join(root, "sdk/node/src/index.ts"), "export const broken = ;");
    for (const action of ["stop", "status"]) {
      const observed = invoke(action, [action]);
      expect(observed.status, observed.stderr).toBe(0);
      expect(JSON.parse(observed.stdout).action).toBe(action);
    }
    const failed = invoke("syntax-error", ["start"]);
    expect(failed.status).not.toBe(0);
    expect(failed.stderr).toContain("开发 SDK 构建失败");
    expect(failed.stdout).toBe("");
    writeFileSync(path.join(root, "sdk/node/src/index.ts"), originals.get(root) + '\nexport const bootstrapProbe = "repaired";\n');
    const direct = invoke("direct", ["--service=all"]);
    expect(direct.status, direct.stderr).toBe(0);
    expect(JSON.parse(direct.stdout)).toEqual({ role: "services", values: roots.map(target => target === root ? "repaired" : "updated") });
  }, 30_000);
}
