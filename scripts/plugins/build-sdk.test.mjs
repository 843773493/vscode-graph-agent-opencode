import { expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { createRequire } from "node:module";
import { chmodSync, cpSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { buildPluginSdk } from "./build-sdk.mjs";
import { writeApplicationNodeManifest } from "../../packaging/runtime/application-sources.mjs";

const projectRoot = process.cwd();
const runRoot = path.join(projectRoot, "out/tests/scripts/plugins/build-sdk", "workspace", randomUUID());

for (const language of ["node", "python"]) {
  test(`${language} SDK 源码更新可用，独立构建可复现且发行脱离源码`, () => {
    const root = path.join(runRoot, language);
    const artifacts = path.join(projectRoot, "out/tests/scripts/plugins/build-sdk/artifacts", path.basename(runRoot), language);
    mkdirSync(artifacts, { recursive: true });
    mkdirSync(path.join(root, "scripts/plugins"), { recursive: true });
    mkdirSync(path.join(root, "resources/logging"), { recursive: true });
    cpSync(path.join(projectRoot, "sdk", language), path.join(root, "sdk", language), { recursive: true, filter: file => !["node_modules", "dist", "build"].includes(path.basename(file)) });
    for (const file of ["build-sdk.mjs", "prepare-product-sdk.mjs", "export-python-requirements.mjs"]) {
      cpSync(path.join(projectRoot, "scripts/plugins", file), path.join(root, "scripts/plugins", file));
    }
    cpSync(path.join(projectRoot, "resources/logging/runtime-contract.json"), path.join(root, "resources/logging/runtime-contract.json"));
    writeFileSync(path.join(root, "package.json"), JSON.stringify({ version: "1.2.3", dependencies: { "@boxteam/plugin-sdk": "file:stale.tgz" } }));
    writeFileSync(path.join(root, "pyproject.toml"), '[project]\nname = "sdk-consumer"\nversion = "1.2.3"\ndependencies = ["boxteam-plugin-sdk==1.2.3"]\n[tool.uv.sources]\nboxteam-plugin-sdk = { path = "stale.whl" }\n');
    const options = { projectRoot: root, language, outputRoot: path.join(runRoot, `${language}-archives`) };
    let first, original, second;
    const source = path.join(root, "sdk", language, "src", language === "node" ? "index.ts" : "boxteam_plugin_sdk/__init__.py");
    // checkout 的权限受 umask 影响；内容相同的 SDK 不能因权限位不同而拒绝冻结安装。
    if (process.platform !== "win32") chmodSync(source, 0o600);
    // 删除输入缓存后再次真正构建，不能以缓存命中证明可复现。

    const observations = [];
    const command = [path.join(root, "scripts/plugins/prepare-product-sdk.mjs")];
    const nodeProbe = path.join(root, "workspace-probe.mjs");
    if (language === "node") writeFileSync(nodeProbe, `
      import assert from 'node:assert/strict';
      import { createRequire } from 'node:module';
      import { readFileSync, realpathSync } from 'node:fs';
      import { fileURLToPath, pathToFileURL } from 'node:url';
      import { loadRuntimeLogContract } from '@boxteam/plugin-sdk/logging';
      const applicationRequire = createRequire(new URL('./package.json', import.meta.url));
      for (const name of ['logging', 'control']) {
        const specifier = '@boxteam/plugin-sdk/' + name;
        assert.strictEqual(await import(pathToFileURL(applicationRequire.resolve(specifier)).href), await import(specifier));
      }
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
    function buildLocal() {
      const result = spawnSync(process.env.BOXTEAM_NODE_BIN ?? "node", [path.join(root, "sdk/node/build.mjs")],
        { cwd: root, encoding: "utf8", timeout: 30_000 });
      expect(result.status, result.stderr).toBe(0);
    }
    function install(label) {
      const args = language === "node" ? [process.execPath, "install", "--ignore-scripts"] : ["uv", "sync", "--no-dev", "--offline"];
      const result = spawnSync(args[0], args.slice(1), { cwd: root, env: process.env, encoding: "utf8", timeout: 30_000 });
      writeFileSync(path.join(artifacts, `install-${label}.json`), JSON.stringify({ command: args, ...result }));
      expect(result.status).toBe(0);
      if (language === "node") buildLocal();
      const installed = path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
      const probe = language === "node"
        ? spawnSync(process.env.BOXTEAM_NODE_BIN ?? "node", [nodeProbe], { cwd: artifacts, encoding: "utf8" })
        : spawnSync(installed, ["-I", "-c", "import boxteam_plugin_sdk; from pathlib import Path; from boxteam_plugin_sdk.logging_contract import load_runtime_log_contract; assert load_runtime_log_contract().root_parts; print(Path(boxteam_plugin_sdk.__file__).read_text())"], { cwd: artifacts, encoding: "utf8" });
      writeFileSync(path.join(artifacts, `loaded-${label}.json`), JSON.stringify(probe));
      expect(probe.status).toBe(0);
      if (language === "node") {
        const loaded = JSON.parse(probe.stdout);
        expect(loaded.source).toBe(path.join(root, "sdk/node/dist/index.js"));
        expect(loaded.version).toBe(1);
        return loaded.text;
      }
      return probe.stdout;
    }
    expect(prepare([]).status).toBe(0);
    const beforeInstall = install("original");
    if (language === "node") {
      const originalSource = readFileSync(source, "utf8");
      writeFileSync(source, originalSource + '\nexport const invalidType: string = 42;\n');
      buildLocal();
      const checked = spawnSync(process.env.BOXTEAM_NODE_BIN ?? "node", [path.join(root, "sdk/node/build.mjs"), "--check"],
        { cwd: root, encoding: "utf8", timeout: 30_000 });
      writeFileSync(path.join(artifacts, "type-error.json"), JSON.stringify(checked));
      expect(checked.status).toBe(1);
      expect(checked.stderr).toContain("TS2322");
      writeFileSync(source, originalSource);
      buildLocal();
      const snapshotProbe = path.join(root, "snapshot-probe.mjs");
      writeFileSync(snapshotProbe, `
        import assert from 'node:assert/strict';
        import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
        import { fileURLToPath } from 'node:url';
        import { createRequire } from 'node:module';
        import { buildNodeSdk } from './sdk/node/build.mjs';
        const ts = createRequire(new URL('./sdk/node/package.json', import.meta.url))('typescript');
        const source = fileURLToPath(new URL('./sdk/node/src/index.ts', import.meta.url));
        const original = readFileSync(source, 'utf8');
        const old = original + '\\nexport function snapshotProbe(): number { return 1; }\\n';
        const updated = original + '\\nexport function snapshotProbe(): string { return "new"; }\\n';
        writeFileSync(source, old);
        mkdirSync('out', { recursive: true });
        writeFileSync('out/package.json', '{"type":"module"}');
        const read = ts.sys.readFile;
        let edited = false;
        ts.sys.readFile = (file, ...args) => {
          const content = read(file, ...args);
          if (file === source && !edited) { edited = true; writeFileSync(source, updated); }
          return content;
        };
        try {
          await buildNodeSdk({ projectRoot: process.cwd(), release: true, outputDirectory: 'out/snapshot-dist' });
          const value = (await import('./out/snapshot-dist/index.js')).snapshotProbe();
          const declaration = readFileSync('out/snapshot-dist/index.d.ts', 'utf8');
          assert.equal(edited, true);
          assert.equal(value, 1);
          assert.match(declaration, /snapshotProbe\\(\\): number/);
          console.log(JSON.stringify({ edited, value, declaration }));
        } finally { ts.sys.readFile = read; writeFileSync(source, original); }
      `);
      const snapshot = spawnSync(process.env.BOXTEAM_NODE_BIN ?? "node", [snapshotProbe],
        { cwd: root, encoding: "utf8", timeout: 30_000 });
      writeFileSync(path.join(artifacts, "release-snapshot.json"), JSON.stringify(snapshot));
      expect(snapshot.status, snapshot.stdout + snapshot.stderr).toBe(0);
    }
    first = buildPluginSdk(options);
    original = readFileSync(first.archive);
    rmSync(path.join(options.outputRoot, "inputs"), { recursive: true });
    const beforeConcurrentEdit = readFileSync(source);
    const originalSpawn = Bun.spawnSync;
    let concurrentEdit = false;
    try {
      if (language === "node") Bun.spawnSync = (command, spawnOptions) => {
        if (command[1]?.endsWith("build.mjs")) {
          writeFileSync(source, beforeConcurrentEdit + '\nexport const concurrentProbe = "不应进入本次归档";\n');
          concurrentEdit = true;
        }
        return originalSpawn(command, spawnOptions);
      };
      second = buildPluginSdk(options);
    } finally {
      Bun.spawnSync = originalSpawn;
      writeFileSync(source, beforeConcurrentEdit);
    }
    if (language === "node") expect(concurrentEdit).toBe(true);
    expect(second.digest).toBe(first.digest);
    writeFileSync(source, readFileSync(source, "utf8") + (language === "node" ? "\nexport const sdkBuildProbe = \"updated SDK content\";\n" : "\n# updated SDK content\n"));
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
    if (language === "node") buildLocal();
    const changedProbe = language === "python" ? spawnSync(python, ["-I", "-c",
      "import boxteam_plugin_sdk; from pathlib import Path; print(Path(boxteam_plugin_sdk.__file__).read_text())"],
      { cwd: artifacts, encoding: "utf8" })
      : spawnSync(process.env.BOXTEAM_NODE_BIN ?? "node", [nodeProbe], { cwd: artifacts, encoding: "utf8" });
    writeFileSync(path.join(artifacts, "loaded-without-reinstall.json"), JSON.stringify(changedProbe));
    expect(changedProbe.status).toBe(0);
    const afterInstall = language === "node" ? JSON.parse(changedProbe.stdout).text : changedProbe.stdout;
    expect(afterInstall).not.toBe(beforeInstall);
    expect(afterInstall).toContain("updated SDK content");
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
      const typescript = createRequire(path.join(root, "sdk/node/package.json")).resolve("typescript/bin/tsc");
      const development = buildPluginSdk({ ...options, development: true });
      expect(development.digest).not.toBe(changed.digest);
      const archive = spawnSync("tar", ["-tzf", development.archive], { encoding: "utf8" });
      expect(archive.status).toBe(0);
      expect(archive.stdout).toContain("package/dist/index.js");
      expect(archive.stdout).not.toContain("package/dist/index.d.ts");
      expect(archive.stdout).not.toContain("package/src/");
      renameSync(path.join(root, "sdk/node"), path.join(root, "sdk/node-hidden"));
      const releaseProbe = path.join(application, "release-probe.mjs");
      cpSync(nodeProbe, releaseProbe);
      const probe = spawnSync(process.env.BOXTEAM_NODE_BIN ?? "node", [releaseProbe], { cwd: artifacts, encoding: "utf8" });
      writeFileSync(path.join(artifacts, "release-without-source.json"), JSON.stringify(probe));
      expect(probe.status).toBe(0);
      const loaded = JSON.parse(probe.stdout);
      expect(loaded.source.startsWith(path.join(application, "node_modules") + path.sep)).toBe(true);
      expect(loaded.text).toContain("updated SDK content");
      expect(loaded.version).toBe(1);
      const typesProbe = path.join(application, "public-types.mts");
      writeFileSync(typesProbe, `import { startMaintenance } from '@boxteam/plugin-sdk';
        import { processExists } from '@boxteam/plugin-sdk/process-lifecycle';
        import { createPluginRuntimeManagement } from '@boxteam/plugin-sdk/runtime';
        import type { PluginCommandHandlers } from '@boxteam/plugin-sdk/commands';
        import { createRuntimeLogger } from '@boxteam/plugin-sdk/logging';
        import { createJsonRequestReader } from '@boxteam/plugin-sdk/http';
        import { verifyRuntimeControlToken } from '@boxteam/plugin-sdk/control';
        import type { RuntimeObservationStatus } from '@boxteam/plugin-sdk/observation';
        import { PluginInstanceStatusSchema } from '@boxteam/plugin-sdk/generated/boxteam/gateway/v2/service_plugin_pb.js';
        import type { PluginInstanceStatus } from '@boxteam/plugin-sdk/generated-json/boxteam/gateway/v2/service_plugin.js';
        const logger = createRuntimeLogger("probe", "types");
        const commands: PluginCommandHandlers = { async probe(args, context) {
          context.signal.throwIfAborted(); await context.progress(1, 1); return args;
        }};
        const runtime = createPluginRuntimeManagement({service: "probe", logger,
          readJson: createJsonRequestReader(logger), async shutdown() {}, commands});
        const version: number = runtime.identity.management_protocol_version;
        const state: Pick<PluginInstanceStatus, "instance_id"> = runtime.identity;
        const observationId = (status: RuntimeObservationStatus): string => status.instance_id;
        verifyRuntimeControlToken("token", runtime.token);
        void version; void state; void observationId; void PluginInstanceStatusSchema;
        const exists: boolean = processExists(process.pid);
        const scheduler: {close(): Promise<void>} = startMaintenance({tasks: [{id: "probe",
          interval_ms: 1000, run_immediately: false, async run(signal) { signal.throwIfAborted(); }}],
          identity: {}, reportError(_id, _identity, _duration, error) { console.error(error); }});
        void exists; void scheduler;`);
      const types = spawnSync(process.env.BOXTEAM_NODE_BIN ?? "node", [typescript, "--strict", "--noEmit",
        "--target", "es2022", "--module", "nodenext", "--moduleResolution", "nodenext",
        "--typeRoots", path.join(root, "sdk/node-hidden/node_modules/@types"), typesProbe],
        { cwd: application, encoding: "utf8", timeout: 30_000 });
      writeFileSync(path.join(artifacts, "release-types.json"), JSON.stringify(types));
      expect(types.status, types.stdout + types.stderr).toBe(0);
      const developmentPath = `vendor/sdk/${development.digest}/${path.basename(development.archive)}`;
      mkdirSync(path.dirname(path.join(application, developmentPath)), { recursive: true });
      cpSync(development.archive, path.join(application, developmentPath));
      manifest.dependencies["@boxteam/plugin-sdk"] = `file:${developmentPath}`;
      writeFileSync(path.join(application, "package.json"), JSON.stringify(manifest));
      const developmentInstall = spawnSync(process.execPath, ["install", "--cwd", application, "--production", "--ignore-scripts"],
        { env: { ...process.env, PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: "1" }, encoding: "utf8", timeout: 30_000 });
      writeFileSync(path.join(artifacts, "development-installed.json"), JSON.stringify(developmentInstall));
      expect(developmentInstall.status).toBe(0);
      const developmentProbe = spawnSync(process.env.BOXTEAM_NODE_BIN ?? "node", [releaseProbe], { cwd: artifacts, encoding: "utf8" });
      writeFileSync(path.join(artifacts, "development-without-source.json"), JSON.stringify(developmentProbe));
      expect(developmentProbe.status).toBe(0);
      expect(JSON.parse(developmentProbe.stdout).text).toContain("updated SDK content");
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
      expect(probe.stdout).toContain("updated SDK content");
    }
    writeFileSync(path.join(artifacts, "archives.json"), JSON.stringify({ first, second, changed }, null, 2));
  }, 120_000);
}
