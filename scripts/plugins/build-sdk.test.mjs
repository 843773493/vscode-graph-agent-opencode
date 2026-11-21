import { expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { chmodSync, cpSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { buildPluginSdk } from "./build-sdk.mjs";
import { writeApplicationNodeManifest } from "../../packaging/runtime/application-sources.mjs";

const projectRoot = process.cwd();
const runRoot = path.join(projectRoot, "out/tests/scripts/plugins/build-sdk", "workspace", randomUUID());

for (const language of ["node", "python"]) {
  test(`${language} SDK 开发源码即时可用，独立构建可复现且发行脱离源码`, () => {
    const root = path.join(runRoot, language);
    const artifacts = path.join(projectRoot, "out/tests/scripts/plugins/build-sdk/artifacts", path.basename(runRoot), language);
    mkdirSync(artifacts, { recursive: true });
    mkdirSync(path.join(root, "scripts/plugins"), { recursive: true });
    mkdirSync(path.join(root, "resources/logging"), { recursive: true });
    cpSync(path.join(projectRoot, "sdk", language), path.join(root, "sdk", language), { recursive: true });
    for (const file of ["build-sdk.mjs", "prepare-product-sdk.mjs", "export-python-requirements.mjs"]) {
      cpSync(path.join(projectRoot, "scripts/plugins", file), path.join(root, "scripts/plugins", file));
    }
    cpSync(path.join(projectRoot, "resources/logging/runtime-contract.json"), path.join(root, "resources/logging/runtime-contract.json"));
    writeFileSync(path.join(root, "package.json"), JSON.stringify({ version: "1.2.3", dependencies: { "@boxteam/plugin-sdk": "file:stale.tgz" } }));
    writeFileSync(path.join(root, "pyproject.toml"), '[project]\nname = "sdk-consumer"\nversion = "1.2.3"\ndependencies = ["boxteam-plugin-sdk==1.2.3"]\n[tool.uv.sources]\nboxteam-plugin-sdk = { path = "stale.whl" }\n');
    const options = { projectRoot: root, language };
    const first = buildPluginSdk(options);
    const original = readFileSync(first.archive);
    const source = path.join(root, "sdk", language, "src", language === "node" ? "index.mjs" : "boxteam_plugin_sdk/__init__.py");
    // checkout 的权限受 umask 影响；内容相同的 SDK 不能因权限位不同而拒绝冻结安装。
    if (process.platform !== "win32") chmodSync(source, 0o600);
    // 删除输入缓存后再次真正构建，不能以缓存命中证明可复现。
    rmSync(path.join(root, "out/sdk/inputs"), { recursive: true });
    const second = buildPluginSdk(options);
    expect(second.digest).toBe(first.digest);
    const observations = [];
    const command = [path.join(root, "scripts/plugins/prepare-product-sdk.mjs")];
    const nodeProbe = path.join(root, "workspace-probe.mjs");
    if (language === "node") writeFileSync(nodeProbe, `
      import { readFileSync, realpathSync } from 'node:fs';
      import { fileURLToPath } from 'node:url';
      import { loadRuntimeLogContract } from '@boxteam/plugin-sdk/logging';
      const source = realpathSync(fileURLToPath(import.meta.resolve('@boxteam/plugin-sdk')));
      console.log(JSON.stringify({ source, text: readFileSync(source, 'utf8'), version: loadRuntimeLogContract().version }));
    `);
    function prepare(args) {
      const result = spawnSync(process.execPath, [...command, ...args, language], { cwd: root,
        env: { ...process.env, BOXTEAM_PROJECT_ROOT: root }, encoding: "utf8", timeout: 30_000 });
      writeFileSync(path.join(artifacts, `${args.includes("--check") ? "check" : "prepare"}-${observations.length}.json`), JSON.stringify({ command: [...command, ...args, language], ...result }));
      observations.push(result.status);
      return result;
    }
    function install(label) {
      const args = language === "node" ? [process.execPath, "install", "--ignore-scripts"] : ["uv", "sync", "--no-dev", "--offline"];
      const result = spawnSync(args[0], args.slice(1), { cwd: root, env: process.env, encoding: "utf8", timeout: 30_000 });
      writeFileSync(path.join(artifacts, `install-${label}.json`), JSON.stringify({ command: args, ...result }));
      expect(result.status).toBe(0);
      const installed = path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
      const probe = language === "node"
        ? spawnSync(process.env.BOXTEAM_NODE_BIN ?? "node", [nodeProbe], { cwd: artifacts, encoding: "utf8" })
        : spawnSync(installed, ["-I", "-c", "import boxteam_plugin_sdk; from pathlib import Path; from boxteam_plugin_sdk.logging_contract import load_runtime_log_contract; assert load_runtime_log_contract().root_parts; print(Path(boxteam_plugin_sdk.__file__).read_text())"], { cwd: artifacts, encoding: "utf8" });
      writeFileSync(path.join(artifacts, `loaded-${label}.json`), JSON.stringify(probe));
      expect(probe.status).toBe(0);
      if (language === "node") {
        const loaded = JSON.parse(probe.stdout);
        expect(loaded.source).toBe(source);
        expect(loaded.version).toBe(1);
        return loaded.text;
      }
      return probe.stdout;
    }
    expect(prepare([]).status).toBe(0);
    const beforeInstall = install("original");
    expect(buildPluginSdk(options).digest).toBe(first.digest);
    writeFileSync(source, readFileSync(source, "utf8") + (language === "node" ? "\n// 新的公开包内容\n" : "\n# 新的公开包内容\n"));
    const changed = buildPluginSdk(options);
    expect(changed.version).toBe(first.version);
    expect(changed.digest).not.toBe(first.digest);
    expect(readFileSync(first.archive)).toEqual(original);
    const manifest = path.join(root, language === "node" ? "package.json" : "pyproject.toml");
    const before = readFileSync(manifest);
    expect(prepare(["--check"]).status).toBe(0);
    expect(readFileSync(manifest)).toEqual(before);
    expect(prepare([]).status).toBe(0);
    const prepared = readFileSync(manifest);
    expect(prepare(["--check"]).status).toBe(0);
    expect(readFileSync(manifest)).toEqual(prepared);
    const python = path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
    const changedProbe = language === "python" ? spawnSync(python, ["-I", "-c",
      "import boxteam_plugin_sdk; from pathlib import Path; print(Path(boxteam_plugin_sdk.__file__).read_text())"],
      { cwd: artifacts, encoding: "utf8" })
      : spawnSync(process.env.BOXTEAM_NODE_BIN ?? "node", [nodeProbe], { cwd: artifacts, encoding: "utf8" });
    writeFileSync(path.join(artifacts, "loaded-without-reinstall.json"), JSON.stringify(changedProbe));
    expect(changedProbe.status).toBe(0);
    const afterInstall = language === "node" ? JSON.parse(changedProbe.stdout).text : changedProbe.stdout;
    expect(afterInstall).not.toBe(beforeInstall);
    expect(afterInstall).toContain("新的公开包内容");
    const lock = readFileSync(path.join(root, language === "node" ? "bun.lock" : "uv.lock"), "utf8");
    const resource = path.join(root, language === "node" ? "sdk/node/src/resources/logging/runtime-contract.json"
      : "sdk/python/src/boxteam_plugin_sdk/resources/runtime-contract.json");
    writeFileSync(resource, "stale generated resource\n");
    expect(prepare(["--check"]).status).toBe(1);
    expect(readFileSync(resource, "utf8")).toBe("stale generated resource\n");
    expect(prepare([]).status).toBe(0);
    if (language === "node") {
      expect(lock).toContain('"@boxteam/plugin-sdk@workspace:sdk/node"');
      expect(lock).not.toContain("out/sdk/node/");
      mkdirSync(path.join(root, "packaging/runtime"), { recursive: true });
      cpSync(path.join(projectRoot, "packaging/runtime/node-package.json"), path.join(root, "packaging/runtime/node-package.json"));
      const application = path.join(runRoot, "node-application");
      writeApplicationNodeManifest(root, application);
      const manifest = JSON.parse(readFileSync(path.join(application, "package.json"), "utf8"));
      expect(manifest.workspaces).toBeUndefined();
      expect(manifest.dependencies["@boxteam/plugin-sdk"]).toContain(`file:vendor/sdk/${changed.digest}/`);
      const installed = spawnSync(process.execPath, ["install", "--cwd", application, "--production", "--ignore-scripts"],
        { env: { ...process.env, PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: "1" }, encoding: "utf8", timeout: 30_000 });
      writeFileSync(path.join(artifacts, "release-installed.json"), JSON.stringify(installed));
      expect(installed.status).toBe(0);
      renameSync(path.join(root, "sdk/node"), path.join(root, "sdk/node-hidden"));
      const releaseProbe = path.join(application, "release-probe.mjs");
      cpSync(nodeProbe, releaseProbe);
      const probe = spawnSync(process.env.BOXTEAM_NODE_BIN ?? "node", [releaseProbe], { cwd: artifacts, encoding: "utf8" });
      writeFileSync(path.join(artifacts, "release-without-source.json"), JSON.stringify(probe));
      expect(probe.status).toBe(0);
      const loaded = JSON.parse(probe.stdout);
      expect(loaded.source.startsWith(path.join(application, "node_modules") + path.sep)).toBe(true);
      expect(loaded.text).toContain("新的公开包内容");
      expect(loaded.version).toBe(1);
    } else {
      expect(lock).toContain('editable = "sdk/python"');
      expect(lock).not.toContain("out/sdk/python/");
      const requirements = path.join(root, "requirements.txt");
      const exported = spawnSync(process.execPath, [path.join(root, "scripts/plugins/export-python-requirements.mjs"), requirements],
        { cwd: root, env: { ...process.env, BOXTEAM_PROJECT_ROOT: root }, encoding: "utf8", timeout: 30_000 });
      writeFileSync(path.join(artifacts, "exported.json"), JSON.stringify(exported));
      expect(exported.status).toBe(0);
      expect(readFileSync(requirements, "utf8")).toContain(`--hash=sha256:${changed.digest}`);
      expect(readFileSync(requirements, "utf8")).not.toContain("-e ");
      const dependencies = path.join(root, "release-dependencies");
      const installed = spawnSync("uv", ["pip", "install", "--python", python, "--target", dependencies,
        "--require-hashes", "--no-deps", "--offline", "--requirements", requirements],
        { cwd: root, encoding: "utf8", timeout: 30_000 });
      writeFileSync(path.join(artifacts, "release-installed.json"), JSON.stringify(installed));
      expect(installed.status).toBe(0);
      renameSync(path.join(root, "sdk/python"), path.join(root, "sdk/python-hidden"));
      const probe = spawnSync(python, ["-S", "-c", "import boxteam_plugin_sdk; from pathlib import Path; from boxteam_plugin_sdk.logging_contract import load_runtime_log_contract; assert load_runtime_log_contract().root_parts; print(Path(boxteam_plugin_sdk.__file__).read_text())"],
        { cwd: artifacts, env: { ...process.env, PYTHONPATH: dependencies }, encoding: "utf8" });
      writeFileSync(path.join(artifacts, "release-without-source.json"), JSON.stringify(probe));
      expect(probe.status).toBe(0);
      expect(probe.stdout).toContain("新的公开包内容");
    }
    writeFileSync(path.join(artifacts, "archives.json"), JSON.stringify({ first, second, changed }, null, 2));
  }, 60_000);
}
