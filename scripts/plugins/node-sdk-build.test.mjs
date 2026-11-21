import { expect, test } from "bun:test";
import { spawn, spawnSync } from "node:child_process";
import { once } from "node:events";
import { randomUUID } from "node:crypto";
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { buildNodeSdk } from "../../sdk/node/build.mjs";

async function until(observe) {
  const deadline = performance.now() + 8000;
  while (!observe()) {
    if (performance.now() >= deadline) throw new Error("等待 SDK watch 产物超时");
    await Bun.sleep(30);
  }
}

test("SDK watch 发布真实 JS、资源及新增删除入口，语法错误保留可用产物", async () => {
  const run = randomUUID();
  const base = path.resolve("out/tests/scripts/plugins/node-sdk-build");
  const root = path.join(base, "workspace", run);
  const artifacts = path.join(base, "artifacts", run);
  mkdirSync(artifacts, { recursive: true });
  cpSync(path.resolve("sdk/node"), path.join(root, "sdk/node"), { recursive: true,
    filter: file => !["node_modules", "dist", "build"].includes(path.basename(file)) });
  let builds = 0;
  const compiler = await buildNodeSdk({ projectRoot: root, watch: true, onBuild() { builds++; } });
  const source = path.join(root, "sdk/node/src/index.ts");
  const output = path.join(root, "sdk/node/dist/index.js");
  const original = readFileSync(source, "utf8");
  try {
    expect(builds).toBe(1);
    writeFileSync(source, original + '\nexport const watchProbe = "已更新";\n');
    await until(() => readFileSync(output, "utf8").includes("watchProbe"));
    const probe = spawnSync(process.env.BOXTEAM_NODE_BIN ?? "node", ["--input-type=module", "-e",
      `const sdk = await import(${JSON.stringify(pathToFileURL(output).href)}); console.log(sdk.watchProbe);`],
      { cwd: artifacts, encoding: "utf8", timeout: 5000 });
    writeFileSync(path.join(artifacts, "consumer.json"), JSON.stringify(probe));
    expect(probe.status).toBe(0);
    expect(probe.stdout.trim()).toBe("已更新");
    const lastGood = readFileSync(output);
    writeFileSync(source, "export const invalid = ;");
    await expect(compiler.rebuild()).rejects.toThrow();
    expect(readFileSync(output)).toEqual(lastGood);
    const beforeRepair = builds;
    writeFileSync(source, original);
    await until(() => builds > beforeRepair && !readFileSync(output, "utf8").includes("watchProbe"));
    const added = path.join(root, "sdk/node/src/added.ts");
    const addedOutput = path.join(root, "sdk/node/dist/added.js");
    writeFileSync(added, "export const value: number = 42;\n");
    await until(() => existsSync(addedOutput));
    rmSync(added);
    await until(() => !existsSync(addedOutput));
    const generated = path.join(root, "sdk/node/src/generated/boxteam/gateway/v3/added.js");
    const generatedOutput = path.join(root, "sdk/node/dist/generated/boxteam/gateway/v3/added.js");
    mkdirSync(path.dirname(generated), { recursive: true });
    writeFileSync(generated, "export const value = 3;\n");
    await until(() => existsSync(generatedOutput));
    rmSync(path.dirname(generated), { recursive: true });
    await until(() => !existsSync(generatedOutput));
    const emptyDirectory = path.join(root, "sdk/node/src/resources/empty/nested");
    mkdirSync(emptyDirectory, { recursive: true });
    // 等父目录登记后，再向原本为空的深层目录写入文件。
    await compiler.rebuild();
    writeFileSync(path.join(emptyDirectory, "added.json"), "{}\n");
    await until(() => existsSync(path.join(root, "sdk/node/dist/resources/empty/nested/added.json")));
    const resource = path.join(root, "sdk/node/src/resources/logging/runtime-contract.json");
    const resourceOutput = path.join(root, "sdk/node/dist/resources/logging/runtime-contract.json");
    writeFileSync(resource, readFileSync(resource, "utf8") + "\n");
    await until(() => readFileSync(resource).equals(readFileSync(resourceOutput)));
    const beforeUnchanged = builds;
    const modified = statSync(output).mtimeMs;
    await compiler.rebuild();
    expect(builds).toBe(beforeUnchanged);
    expect(statSync(output).mtimeMs).toBe(modified);
    expect(existsSync(path.join(root, "sdk/node/dist/index.d.ts"))).toBe(false);
    writeFileSync(path.join(artifacts, "builds.json"), JSON.stringify({ builds, output, modified }));
  } finally { await compiler.dispose(); }
  rmSync(output);
  const child = spawn(process.env.BOXTEAM_NODE_BIN ?? "node", [path.resolve("sdk/node/build.mjs"), "--watch"],
    { cwd: root, stdio: ["ignore", "pipe", "pipe"] });
  let diagnostic = "";
  child.stdout.on("data", bytes => { diagnostic += bytes; });
  child.stderr.on("data", bytes => { diagnostic += bytes; });
  const exited = once(child, "exit");
  try {
    await until(() => {
      if (child.exitCode !== null) throw new Error(diagnostic);
      return existsSync(output);
    });
    child.kill("SIGTERM");
    const result = await exited;
    // Windows 的 SIGTERM 是系统终止；Unix 由 CLI 释放编译器后正常退出。
    expect(result).toEqual(process.platform === "win32" ? [null, "SIGTERM"] : [0, null]);
    writeFileSync(path.join(artifacts, "watch-cli.json"), JSON.stringify({ result, diagnostic }));
  } finally {
    if (child.exitCode === null && child.signalCode === null) { child.kill("SIGKILL"); await exited; }
  }
}, 25_000);
