import { expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { buildPluginSdk } from "./build-sdk.mjs";

const projectRoot = process.cwd();
const runRoot = path.join(projectRoot, "out/tests/scripts/plugins/build-sdk", "workspace", randomUUID());

for (const language of ["node", "python"]) {
  test(`${language} SDK 独立重建可复现，来源更新保留旧归档且只读检查不改清单`, () => {
    const root = path.join(runRoot, language);
    const artifacts = path.join(projectRoot, "out/tests/scripts/plugins/build-sdk/artifacts", path.basename(runRoot), language);
    mkdirSync(artifacts, { recursive: true });
    mkdirSync(path.join(root, "scripts/plugins"), { recursive: true });
    mkdirSync(path.join(root, "resources/logging"), { recursive: true });
    cpSync(path.join(projectRoot, "sdk", language), path.join(root, "sdk", language), { recursive: true });
    for (const file of ["build-sdk.mjs", "prepare-product-sdk.mjs"]) {
      cpSync(path.join(projectRoot, "scripts/plugins", file), path.join(root, "scripts/plugins", file));
    }
    cpSync(path.join(projectRoot, "resources/logging/runtime-contract.json"), path.join(root, "resources/logging/runtime-contract.json"));
    writeFileSync(path.join(root, "package.json"), JSON.stringify({ version: "1.2.3", dependencies: { "@boxteam/plugin-sdk": "file:stale.tgz" } }));
    writeFileSync(path.join(root, "pyproject.toml"), '[project]\nname = "sdk-consumer"\nversion = "1.2.3"\ndependencies = ["boxteam-plugin-sdk==1.2.3"]\n[tool.uv.sources]\nboxteam-plugin-sdk = { path = "stale.whl" }\n');
    const options = { projectRoot: root, language };
    const first = buildPluginSdk(options);
    const original = readFileSync(first.archive);
    // 删除输入缓存后再次真正构建，不能以缓存命中证明可复现。
    rmSync(path.join(root, "out/sdk/inputs"), { recursive: true });
    const second = buildPluginSdk(options);
    expect(second.digest).toBe(first.digest);
    const observations = [];
    const command = [path.join(root, "scripts/plugins/prepare-product-sdk.mjs")];
    function prepare(args) {
      const result = spawnSync(process.execPath, [...command, ...args, language], { cwd: root,
        env: { ...process.env, BOXTEAM_PROJECT_ROOT: root }, encoding: "utf8", timeout: 30_000 });
      writeFileSync(path.join(artifacts, `${args.includes("--check") ? "check" : "prepare"}-${observations.length}.json`), JSON.stringify({ command: [...command, ...args, language], ...result }));
      observations.push(result.status);
      return result;
    }
    function install(label) {
      const args = language === "node" ? [process.execPath, "install", "--ignore-scripts"] : ["uv", "sync", "--no-dev"];
      const result = spawnSync(args[0], args.slice(1), { cwd: root, env: process.env, encoding: "utf8", timeout: 30_000 });
      writeFileSync(path.join(artifacts, `install-${label}.json`), JSON.stringify({ command: args, ...result }));
      expect(result.status).toBe(0);
      const installed = language === "node" ? path.join(root, "node_modules/@boxteam/plugin-sdk/src/index.mjs")
        : path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
      const probe = language === "node"
        ? spawnSync(process.execPath, ["-e", "import('@boxteam/plugin-sdk').then(()=>console.log('sdk-loaded'))"], { cwd: root, encoding: "utf8" })
        : spawnSync(installed, ["-c", "import boxteam_plugin_sdk; from pathlib import Path; print(Path(boxteam_plugin_sdk.__file__).read_text())"], { cwd: root, encoding: "utf8" });
      writeFileSync(path.join(artifacts, `loaded-${label}.json`), JSON.stringify(probe));
      expect(probe.status).toBe(0);
      return language === "node" ? readFileSync(installed, "utf8") : probe.stdout;
    }
    expect(prepare([]).status).toBe(0);
    const beforeInstall = install("original");
    const source = path.join(root, "sdk", language, "src", language === "node" ? "index.mjs" : "boxteam_plugin_sdk/__init__.py");
    writeFileSync(source, readFileSync(source, "utf8") + (language === "node" ? "\n// 新的公开包内容\n" : "\n# 新的公开包内容\n"));
    const changed = buildPluginSdk(options);
    expect(changed.version).toBe(first.version);
    expect(changed.digest).not.toBe(first.digest);
    expect(readFileSync(first.archive)).toEqual(original);
    const manifest = path.join(root, language === "node" ? "package.json" : "pyproject.toml");
    const before = readFileSync(manifest);
    expect(prepare(["--check"]).status).toBe(1);
    expect(readFileSync(manifest)).toEqual(before);
    expect(prepare([]).status).toBe(0);
    const prepared = readFileSync(manifest);
    expect(prepare(["--check"]).status).toBe(0);
    expect(readFileSync(manifest)).toEqual(prepared);
    const afterInstall = install("changed");
    expect(afterInstall).not.toBe(beforeInstall);
    expect(afterInstall).toContain("新的公开包内容");
    const lock = readFileSync(path.join(root, language === "node" ? "bun.lock" : "uv.lock"), "utf8");
    expect(lock).toContain(changed.digest);
    expect(lock).not.toContain(first.digest);
    writeFileSync(path.join(artifacts, "archives.json"), JSON.stringify({ first, second, changed }, null, 2));
  }, 60_000);
}
