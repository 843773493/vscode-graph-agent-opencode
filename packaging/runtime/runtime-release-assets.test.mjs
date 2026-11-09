import { describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import {
  mkdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";

import { BOXTEAM_VERSION } from "./versions.mjs";
import { stampReleasePackageManifest } from "./runtime-release-assets.mjs";

const projectRoot = process.cwd();
const fixtureRunId = randomUUID();
const outputRoot = path.join(projectRoot, "out/tests/packaging/runtime/runtime-release-assets");
const moduleUrl = new URL("./runtime-release-assets.mjs", import.meta.url).href;

function createScenarioDirectories(scenario) {
  return Object.fromEntries(["workspace", "artifacts"].map((kind) => {
    const directory = path.join(outputRoot, kind, fixtureRunId, scenario);
    mkdirSync(path.dirname(directory), { recursive: true });
    mkdirSync(directory);
    return [kind, directory];
  }));
}

function createManifestPackage(scenario, packageJson) {
  const root = path.join(createScenarioDirectories(scenario).workspace, "待发布 package");
  mkdirSync(root);
  writeFileSync(
    path.join(root, "package.json"),
    `${JSON.stringify(packageJson)}\n`,
    "utf8",
  );
  return root;
}

describe("发布包版本注入", () => {
  test("runtime 包使用根 package.json 版本", () => {
    const rootPackage = JSON.parse(
      readFileSync(path.join(process.cwd(), "package.json"), "utf8"),
    );
    expect(BOXTEAM_VERSION).toBe(rootPackage.version);
    const root = createManifestPackage("runtime-manifest-version", {
      name: "@boxteam/runtime-linux-x64",
      description: "runtime",
    });

    stampReleasePackageManifest(root);

    const packageJson = JSON.parse(
      readFileSync(path.join(root, "package.json"), "utf8"),
    );
    expect(packageJson.version).toBe(BOXTEAM_VERSION);
  });

  test("launcher 包使用根版本并绑定同版本平台 runtime", () => {
    const root = createManifestPackage("launcher-platform-dependencies", {
      name: "boxteam",
      description: "launcher",
      scripts: { test: "bun test src", doctor: "node bin/boxteam.mjs doctor" },
    });

    stampReleasePackageManifest(root, { launcher: true });

    const packageJson = JSON.parse(
      readFileSync(path.join(root, "package.json"), "utf8"),
    );
    expect(packageJson.version).toBe(BOXTEAM_VERSION);
    expect(packageJson.scripts).toEqual({ doctor: "node bin/boxteam.mjs doctor" });
    expect(packageJson.optionalDependencies).toEqual({
      "@boxteam/runtime-linux-x64": BOXTEAM_VERSION,
      "@boxteam/runtime-windows-x64": BOXTEAM_VERSION,
    });
  });
});

function observeSizeReport(scenario) {
  const directories = createScenarioDirectories(scenario);
  const script = `
    import fs from "node:fs";
    import path from "node:path";
    import { syncBuiltinESMExports } from "node:module";
    const [moduleUrl, workspace, scenario] = JSON.parse(process.argv[1]);
    const runtimePackageRoot = path.join(workspace, "runtime package");
    const outputRoot = path.join(workspace, "归档 输出");
    for (const [relative, content] of [
      ["python/core.bin", Buffer.from([0, 1, 254, 255])], ["node/bin/node", "fixed-node"],
      ["application/层 目录/main.py", "汉字\\n"], ["application/empty.txt", ""],
      ["web/index.html", "web bytes"], ["chromium/browser.exe", Buffer.alloc(17, 2)],
    ]) {
      const file = path.join(runtimePackageRoot, relative);
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, content);
    }
    fs.mkdirSync(path.join(runtimePackageRoot, "python/empty"));
    fs.mkdirSync(outputRoot);
    const archives = {};
    for (const [filename, length] of [["npm下载器.tgz", 5], ["launcher.tgz", 7], ["runtime.tgz", 29], ["standalone.zip", 31], ["installer.exe", 37]]) {
      const file = path.join(outputRoot, filename);
      fs.writeFileSync(file, Buffer.alloc(length));
      archives[filename] = { filename, path: file };
    }
    const assetGroups = { release_assets: [archives["runtime.tgz"]] };
    if (scenario === "size-windows") {
      assetGroups.standalone_assets = [archives["standalone.zip"]];
      assetGroups.installer_assets = [archives["installer.exe"]];
    }
    // 体积报告只需元数据，不允许为统计而读取组件正文。
    for (const method of ["readFileSync", "createReadStream"]) {
      const original = fs[method];
      fs[method] = (file, ...args) => {
        if (typeof file === "string" && file.startsWith(runtimePackageRoot + path.sep)) {
          throw new Error("体积统计不得读取组件正文: " + file);
        }
        return original(file, ...args);
      };
    }
    syncBuiltinESMExports();
    const { writeRuntimeSizeReport } = await import(moduleUrl);
    writeRuntimeSizeReport({ runtimePackageRoot, outputRoot,
      npmTarballs: [archives["npm下载器.tgz"].path, archives["launcher.tgz"].path], assetGroups });
  `;
  const args = ["--input-type=module", "--eval", script, JSON.stringify([moduleUrl, directories.workspace, scenario])];
  const result = spawnSync("node", args, { cwd: projectRoot, timeout: 5_000 });
  const stdout = result.stdout ?? Buffer.alloc(0);
  const stderr = result.stderr ?? Buffer.alloc(0);
  writeFileSync(path.join(directories.artifacts, "stdout.raw"), stdout, { flag: "wx" });
  writeFileSync(path.join(directories.artifacts, "stderr.raw"), stderr, { flag: "wx" });
  writeFileSync(path.join(directories.artifacts, "result.json"), `${JSON.stringify({
    command: ["node", ...args], cwd: projectRoot, status: result.status, signal: result.signal,
    error: result.error ? { name: result.error.name, message: result.error.message, code: result.error.code ?? null } : null,
  }, null, 2)}\n`, { flag: "wx" });
  if (result.error || result.status !== 0) {
    throw new Error(`发布体积报告 ${scenario} 失败: exit=${result.status} signal=${result.signal}；artifacts=${directories.artifacts}\n${stderr.toString("utf8")}`, { cause: result.error });
  }
  return { report: JSON.parse(stdout), stdout: stdout.toString("utf8"), workspace: directories.workspace };
}

describe("两平台共用发布体积报告", () => {
  for (const scenario of ["size-linux", "size-windows"]) {
    test(`${scenario}保留组件和平台产物字节，文件与stdout完全一致`, () => {
      const result = observeSizeReport(scenario);
      expect(result.report).toEqual({
        components: { python: 4, node: 10, application: 7, web: 9, chromium: 17 },
        npm_tarballs: { "npm下载器.tgz": 5, "launcher.tgz": 7 },
        release_assets: { "runtime.tgz": 29 },
        ...(scenario === "size-windows" ? {
          standalone_assets: { "standalone.zip": 31 }, installer_assets: { "installer.exe": 37 },
        } : {}),
      });
      expect(readFileSync(path.join(result.workspace, "归档 输出/size-report.json"), "utf8")).toBe(result.stdout);
    });
  }
});


async function observeBuilderPackageStaging(builderUrl, mode, launcherSourceRoot) {
  const { default: assert } = await import("node:assert/strict");
  const fs = await import("node:fs");
  const { default: path } = await import("node:path");
  const { spawnSync } = await import("node:child_process");
  const { createContext, SourceTextModule, SyntheticModule } = await import("node:vm");
  const fixtureRoot = process.cwd();
  const write = (relative, content) => {
    const file = path.join(fixtureRoot, relative);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, content);
    return file;
  };
  const target = mode === "linux" ? "linux-x64" : "windows-x64";
  const fixtureVersion = "0.0.0-staging-fixture";
  write("package.json", JSON.stringify({ version: fixtureVersion }));
  write(`packages/runtime-${target}/package.json`, JSON.stringify({ name: `@boxteam/runtime-${target}` }));
  write("packaging/runtime/node-package.json", "{}");
  const sourceRoot = path.join(fixtureRoot, "packages/launcher");
  fs.cpSync(launcherSourceRoot, sourceRoot, {
    recursive: true, filter: (source) => !source.split(path.sep).includes("node_modules"),
  });
  // 同名目录中的产品正文仍须复制；只有普通测试、说明文件被排除。
  write("packages/launcher/src/fixture.test.mjs/真实 模块.mjs", "export const value = '目录不是测试文件';\n");
  write("packages/launcher/src/fixture.test.mjs/AGENTS.md", "fixture说明\n");
  write("packages/launcher/src/node_modules/unused/index.mjs", "不得复制的依赖\n");
  write("packages/launcher/node_modules/unused/index.mjs", "不得复制的依赖\n");
  const outputRoot = path.join(fixtureRoot, "out/packaging", target);
  const runtimePackageRoot = path.join(outputRoot, "stage", `runtime-${target}`, "package");
  const launcherPackageRoot = path.join(outputRoot, "stage/launcher/package");
  const pythonExecutable = path.join(runtimePackageRoot, "python", mode === "linux" ? "bin/python3" : "python.exe");
  const toolchainRoot = path.join(fixtureRoot, "toolchain");
  const nodeExecutable = path.join(runtimePackageRoot, "node", mode === "linux" ? "bin/node" : "node.exe");
  const bunExecutable = path.join(toolchainRoot, "bun");
  const uvExecutable = path.join(toolchainRoot, "uv");
  const nodeGypEntrypoint = path.join(
    fixtureRoot,
    "node_modules/node-gyp/bin/node-gyp.js",
  );
  const expectedTarget = {
    os: mode === "linux" ? "linux" : "windows",
    architecture: "x64",
    system_baseline: mode === "linux" ? "glibc-2.35" : "windows-10.0.19041",
    node: { version: "22.17.0", abi: "127" },
    python: { version: "3.12.13", abi: "cp312" },
  };
  const stopBeforePack = new Error("fixture在第一次npm pack前结束");
  const packCalls = [];
  const pluginPackCalls = [];
  const buildEvents = [];
  const archiveExtractionCalls = [];
  const frontendBuilds = [];
  const downloadCalls = [];
  let buildStarts = 0;
  // 两份不同的旧产物用于识别构建、复制目录错配，不能由装具替构建入口选中来源。
  for (const client of ["web", "workbench"]) write(`src/clients/${client}/dist/index.html`, `stale-${client}`);
  process.env.BOXTEAM_PROJECT_ROOT = fixtureRoot;
  const context = createContext({ console, Buffer, process: {
    env: { BOXTEAM_PROJECT_ROOT: fixtureRoot }, cwd: () => fixtureRoot,
    execPath: process.execPath, arch: "x64",
    platform: mode === "windows-native" ? "win32" : "linux",
    argv: mode === "windows-cross" ? ["--cross"] : [],
  }, Bun: { which(name) {
    if (name === "bun") return bunExecutable;
    if (name === "uv") return uvExecutable;
    return null;
  } } });
  // 执行原builder全文；仅替换下载、依赖、应用复制和外部命令边界。
  const module = new SourceTextModule(fs.readFileSync(new URL(builderUrl), "utf8"), { identifier: builderUrl, context });
  await module.link(async (specifier) => {
    let exports;
    if (specifier === "./pinned-archive-download.mjs") exports = {
      downloadPinnedLinuxPython: async () => { downloadCalls.push("linux-python"); return "fixture-python.tgz"; },
      downloadPinnedLinuxNode: async () => { downloadCalls.push("linux-node"); return "fixture-node.tgz"; },
      downloadPinnedWindowsPython: async () => { downloadCalls.push("windows-python"); return "fixture-python.tgz"; },
      downloadPinnedWindowsNode: async () => { downloadCalls.push("windows-node"); return "fixture-node.zip"; },
    };
    else if (specifier === "./runtime-build-result.mjs") exports = {
      beginRuntimeBuild: () => { buildStarts++; return "fixture-fingerprint"; },
      publishRuntimeBuildResult: () => { throw new Error("fixture不得进入构建发布"); },
    };
    else if (specifier === "../../scripts/plugins/pack-developer-tools.mjs") exports = {
      async packDeveloperTools(options) {
        pluginPackCalls.push(["developer-tools", JSON.parse(JSON.stringify(options))]);
        buildEvents.push("developer-tools.zip");
        assert.deepEqual(JSON.parse(JSON.stringify(options.target)), expectedTarget);
        write(path.relative(fixtureRoot, options.output), `ZIP fixture\n${JSON.stringify(options.target)}\n`);
      },
    };
    else if (specifier === "../../scripts/plugins/pack-mcp-stdio.mjs") exports = {
      async packMcpStdio(options) {
        pluginPackCalls.push(["mcp-stdio", JSON.parse(JSON.stringify(options))]);
        buildEvents.push("mcp-stdio.zip");
        assert.deepEqual(JSON.parse(JSON.stringify(options.target)), expectedTarget);
        write(path.relative(fixtureRoot, options.output), `ZIP fixture\n${JSON.stringify(options.target)}\n`);
      },
    };
    else if (specifier === "./application-sources.mjs") exports = { copyApplicationSources() {} };
    else if (specifier === "./windows-python-dependencies.mjs") exports = { installWindowsPythonDependencies() {} };
    else {
      exports = { ...await import(specifier.startsWith(".") ? new URL(specifier, builderUrl).href : specifier) };
      if (specifier === "node:child_process") exports.spawnSync = (command, args, options) => {
        if (args[0] === "pack") {
          packCalls.push({ command, args: [...args], cwd: options.cwd, shell: options.shell ?? false });
          buildEvents.push("npm pack");
          return { error: stopBeforePack };
        }
        buildEvents.push(`${command} ${args.join(" ")}`);
        if (command === "tar" && args[0] === "-xzf") {
          archiveExtractionCalls.push({ command, args: [...args] });
          if (args[1] === "fixture-python.tgz") write(path.relative(fixtureRoot, pythonExecutable), "fixture-python");
          else {
            assert.equal(args[1], "fixture-node.tgz");
            assert.deepEqual(Array.from(args.slice(2)), ["-C", path.join(runtimePackageRoot, "node"), "--strip-components=1", "node-v22.17.0-linux-x64/bin/node", "node-v22.17.0-linux-x64/LICENSE"]);
            write(path.relative(fixtureRoot, nodeExecutable), "fixture-node");
            write(path.relative(fixtureRoot, path.join(runtimePackageRoot, "node/LICENSE")), "fixture-license");
          }
        }
        if (command === "tar" && args[0] === "-xf") {
          archiveExtractionCalls.push({ command, args: [...args] });
          write(path.relative(fixtureRoot, path.join(outputRoot, "stage/build-node/node-v22.17.0-win-x64/node.exe")), "fixture-node");
          write(path.relative(fixtureRoot, path.join(outputRoot, "stage/build-node/node-v22.17.0-win-x64/LICENSE")), "fixture-license");
        }
        if (command === "unzip") {
          archiveExtractionCalls.push({ command, args: [...args] });
          write(path.relative(fixtureRoot, nodeExecutable), "fixture-node");
        }
        if (command === nodeExecutable && args[0] === "--print") {
          return { status: 0, stdout: JSON.stringify({ version: "22.17.0", abi: "127", glibc: "2.35" }) };
        }
        if (command === "bun" && args[0] === "run" && args[1] === "build") {
          const relativeRoot = path.relative(fixtureRoot, options.cwd);
          const marker = `fixture-frontend:${relativeRoot}`;
          for (const [name, content] of [
            ["index.html", marker], ["out/worker.js", `${marker}:worker`],
            ["extensions/fixture/package.json", JSON.stringify({ name: marker })],
          ]) write(path.join(relativeRoot, "dist", name), content);
          frontendBuilds.push({ command, args: [...args], cwd: options.cwd });
        }
        if (command === nodeExecutable && args[0] !== "--print" && !args.includes("rebuild")) {
          const browserExecutable = path.join(options.env.PLAYWRIGHT_BROWSERS_PATH, "chromium-fixture", "browser.exe");
          if (args.includes("--eval")) return { status: 0, stdout: `${browserExecutable}\n` };
          write(path.relative(fixtureRoot, browserExecutable), "fixture-chromium");
        }
        return { status: 0, stdout: "" };
      };
    }
    return new SyntheticModule(Object.keys(exports), function () {
      for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
    }, { context });
  });
  if (mode === "windows-cross") {
    await assert.rejects(module.evaluate(), (error) => {
      assert.match(error.message, /windows-x64 构建器不支持当前模式: host=linux-x64 cross=true/);
      return true;
    });
    assert.equal(buildStarts, 0, "Linux --cross 必须在构建快照前拒绝");
    assert.deepEqual(downloadCalls, [], "Linux --cross 不得下载归档");
    assert.deepEqual(buildEvents, [], "Linux --cross 不得调用外部构建命令");
    assert.deepEqual(archiveExtractionCalls, []);
    assert.deepEqual(pluginPackCalls, []);
    assert.deepEqual(packCalls, []);
    assert.deepEqual(frontendBuilds, []);
    assert.equal(fs.existsSync(outputRoot), false, "Linux --cross 不得创建staging或发布输出");
    return { mode, rejectedBeforeWrites: true, downloads: 0, buildStarts };
  }
  await assert.rejects(module.evaluate(), (error) => error === stopBeforePack);
  const snapshot = (root, relative = "") => fs.readdirSync(path.join(root, relative), { withFileTypes: true }).flatMap((entry) => {
    const name = path.posix.join(relative, entry.name);
    return entry.isDirectory() ? snapshot(root, name) : [{ path: name, bytes: fs.statSync(path.join(root, name)).size }];
  }).sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  const sourceFiles = snapshot(sourceRoot);
  const isDevelopmentFile = (name) => name.split("/").includes("node_modules") || name.endsWith(".test.mjs") || path.posix.basename(name) === "AGENTS.md";
  const expectedFiles = sourceFiles.filter(({ path: name }) => !isDevelopmentFile(name));
  const stagedFiles = snapshot(launcherPackageRoot);
  assert.equal(frontendBuilds.length, 1, "平台构建必须只生成一份 Web 产物");
  const frontendSource = path.join(frontendBuilds[0].cwd, "dist");
  const stagedFrontend = path.join(runtimePackageRoot, "web");
  const builtFrontendFiles = snapshot(frontendSource);
  const stagedFrontendFiles = snapshot(stagedFrontend);
  const cliArgs = [path.join(launcherPackageRoot, "bin/boxteam.mjs"), "--version"];
  const cli = spawnSync(process.execPath, cliArgs, { cwd: fixtureRoot, timeout: 5000 });
  const summary = {
    mode, sourceFiles, stagedFiles, packCalls, pluginPackCalls, buildEvents,
    archiveExtractionCalls, frontendBuilds, builtFrontendFiles, stagedFrontendFiles,
    cli: { command: [process.execPath, ...cliArgs], status: cli.status, signal: cli.signal,
      error: cli.error ? { name: cli.error.name, message: cli.error.message } : null },
  };
  const artifacts = process.argv[3];
  fs.writeFileSync(path.join(artifacts, "stage-observation.json"), `${JSON.stringify(summary, null, 2)}\n`, { flag: "wx" });
  fs.writeFileSync(path.join(artifacts, "launcher-version.stdout.raw"), cli.stdout ?? "", { flag: "wx" });
  fs.writeFileSync(path.join(artifacts, "launcher-version.stderr.raw"), cli.stderr ?? "", { flag: "wx" });
  assert.equal(frontendBuilds[0].cwd, path.join(fixtureRoot, "src/clients/workbench"), "平台发行必须构建唯一 Workbench 入口");
  assert.deepEqual(stagedFrontendFiles, builtFrontendFiles, "发行资源必须完整来自本次实际构建目录");
  for (const { path: name } of builtFrontendFiles) {
    assert.deepEqual(fs.readFileSync(path.join(stagedFrontend, name)), fs.readFileSync(path.join(frontendSource, name)), name);
  }
  assert.equal(cli.status, 0, String(cli.stderr));
  assert.equal(String(cli.stdout), `${fixtureVersion}\n`);
  assert.deepEqual(stagedFiles.map(({ path }) => path), expectedFiles.map(({ path }) => path));
  for (const { path: name } of expectedFiles) {
    if (name !== "package.json") assert.deepEqual(fs.readFileSync(path.join(launcherPackageRoot, name)), fs.readFileSync(path.join(sourceRoot, name)), name);
  }
  const launcherManifest = JSON.parse(fs.readFileSync(path.join(launcherPackageRoot, "package.json"), "utf8"));
  assert.equal(launcherManifest.version, fixtureVersion);
  assert.equal(launcherManifest.scripts.test, undefined);
  assert.equal(JSON.parse(fs.readFileSync(path.join(sourceRoot, "package.json"), "utf8")).scripts.test, "bun test src");
  assert.deepEqual(launcherManifest.optionalDependencies, {
    "@boxteam/runtime-linux-x64": fixtureVersion, "@boxteam/runtime-windows-x64": fixtureVersion,
  });
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(runtimePackageRoot, "package.json"), "utf8")), {
    name: `@boxteam/runtime-${target}`, version: fixtureVersion,
  });
  const runtimeManifest = JSON.parse(fs.readFileSync(path.join(runtimePackageRoot, "runtime-manifest.json"), "utf8"));
  assert.deepEqual(runtimeManifest.node, { source: "bundled", executable: mode === "linux" ? "node/bin/node" : "node/node.exe" });
  assert.equal(fs.readFileSync(nodeExecutable, "utf8"), "fixture-node");
  assert.equal(fs.readFileSync(path.join(runtimePackageRoot, "node/LICENSE"), "utf8"), "fixture-license");
  assert.deepEqual(downloadCalls, mode === "linux" ? ["linux-python", "linux-node"] : ["windows-python", "windows-node"]);
  assert.equal(runtimeManifest.installation_tools, undefined, "构建工具不进入运行时manifest");
  const expectedPluginOptions = [
    ["developer-tools", path.join(runtimePackageRoot, "application/resources/plugins/developer-tools.zip")],
    ["mcp-stdio", path.join(runtimePackageRoot, "application/resources/plugins/mcp-stdio.zip")],
  ].map(([name, output]) => [name, {
    projectRoot: fixtureRoot,
    output,
    target: expectedTarget,
    nodeExecutable,
    bunExecutable,
    uvExecutable,
    pythonExecutable,
    nodeGypEntrypoint,
  }]);
  assert.deepEqual(pluginPackCalls, expectedPluginOptions, "预构建 ZIP 使用目标平台元数据及固定 Node");
  if (mode === "linux") {
    assert.equal(
      buildEvents.find((event) => event.startsWith("uv export ")),
      `uv export --locked --no-dev --format requirements-txt --output-file ${path.join(outputRoot, "requirements.locked.txt")}`,
      "Linux runtime export 不再向 requirements 导出器传入 uv 安装包",
    );
  }
  const nodeDependencyInstall = buildEvents.findIndex((event) =>
    event.startsWith("bun install --production"));
  assert(nodeDependencyInstall >= 0, "构建入口必须先准备产品 Node 依赖");
  assert(nodeDependencyInstall < buildEvents.indexOf("developer-tools.zip"));
  assert(buildEvents.indexOf("developer-tools.zip") < buildEvents.indexOf("mcp-stdio.zip"));
  assert(buildEvents.indexOf("mcp-stdio.zip") < buildEvents.indexOf("npm pack"));
  if (mode === "windows-native") {
    const nodeArchiveUnpackIndex = buildEvents.findIndex((event) =>
      event === `tar -xf fixture-node.zip -C ${path.join(outputRoot, "stage/build-node")}`);
    const dependencyInstallIndex = buildEvents.findIndex((event) =>
      event.startsWith("bun install --production"));
    assert(nodeArchiveUnpackIndex >= 0, "固定 Windows Node 必须在 main 中解包一次");
    assert(nodeArchiveUnpackIndex < dependencyInstallIndex, "Node 解包必须先于 node-gyp 和插件打包");
    assert.deepEqual(archiveExtractionCalls, [
      {
        command: "tar",
        args: ["-xzf", "fixture-python.tgz", "-C", runtimePackageRoot],
      },
      {
        command: "tar",
        args: ["-xf", "fixture-node.zip", "-C", path.join(outputRoot, "stage/build-node")],
      },
    ]);
    assert.equal(fs.readFileSync(nodeExecutable, "utf8"), "fixture-node");
    if (mode === "windows-native") {
      const nodeGypCall = buildEvents.findIndex((event) =>
        event.startsWith(nodeExecutable + " ") && event.endsWith(" rebuild"));
      assert(nodeGypCall > nodeArchiveUnpackIndex, "node-gyp 必须使用刚解包的固定 Node");
      assert(nodeGypCall < buildEvents.indexOf("developer-tools.zip"));
    }
  }
  for (const [, options] of pluginPackCalls) {
    assert.equal(path.isAbsolute(options.nodeExecutable), true);
    assert.equal(path.isAbsolute(options.bunExecutable), true);
    assert.equal(path.isAbsolute(options.uvExecutable), true);
    assert.equal(path.isAbsolute(options.pythonExecutable), true);
    assert.equal(path.isAbsolute(options.nodeGypEntrypoint), true);
    assert.equal(fs.readFileSync(options.output, "utf8"), `ZIP fixture\n${JSON.stringify(expectedTarget)}\n`);
  }
  assert.deepEqual(packCalls, [{ command: mode === "windows-native" ? "npm.cmd" : "npm",
    args: ["pack", "--silent", "--pack-destination", path.join(outputRoot, "release-assets")],
    cwd: runtimePackageRoot, shell: mode !== "linux" }]);
  return { mode, sourceFiles: sourceFiles.length, stagedFiles: stagedFiles.length,
    sourceBytes: sourceFiles.reduce((total, file) => total + file.bytes, 0),
    stagedBytes: stagedFiles.reduce((total, file) => total + file.bytes, 0), launcherVersion: String(cli.stdout).trim() };
}

const stagingObserverPath = path.join(outputRoot, "artifacts", fixtureRunId, "staging-observer.mjs");
mkdirSync(path.dirname(stagingObserverPath), { recursive: true });
writeFileSync(stagingObserverPath, `import { readFileSync } from "node:fs";\n${observeBuilderPackageStaging.toString()}\nconsole.log(JSON.stringify(await observeBuilderPackageStaging(...JSON.parse(readFileSync(process.argv[2], "utf8")))));\n`, { flag: "wx" });

describe("两平台构建入口共用Launcher发布内容", () => {
  for (const mode of ["linux", "windows-cross", "windows-native"]) {
    test(mode === "windows-cross"
      ? "Linux --cross 在创建staging前拒绝"
      : `${mode}排除开发文件，保留产品正文与真实Launcher入口`, () => {
      const directories = createScenarioDirectories(`package-staging-${mode}`);
      const builderUrl = new URL(mode === "linux" ? "./build-linux-x64.mjs" : "./build-windows-x64.mjs", import.meta.url).href;
      const inputPath = path.join(directories.artifacts, "input.json");
      writeFileSync(inputPath, `${JSON.stringify([builderUrl, mode, path.join(projectRoot, "packages/launcher")])}\n`, { flag: "wx" });
      const args = ["--experimental-vm-modules", stagingObserverPath, inputPath, directories.artifacts];
      const result = spawnSync("node", args, { cwd: directories.workspace, timeout: 10000 });
      writeFileSync(path.join(directories.artifacts, "stdout.raw"), result.stdout ?? "", { flag: "wx" });
      writeFileSync(path.join(directories.artifacts, "stderr.raw"), result.stderr ?? "", { flag: "wx" });
      writeFileSync(path.join(directories.artifacts, "result.json"), `${JSON.stringify({
        command: ["node", ...args], cwd: directories.workspace, timeout_ms: 10000,
        status: result.status, signal: result.signal,
        error: result.error ? { name: result.error.name, message: result.error.message, code: result.error.code ?? null } : null,
      }, null, 2)}\n`, { flag: "wx" });
      if (result.error || result.status !== 0) throw new Error(`包staging ${mode}失败；artifacts=${directories.artifacts}\n${result.stderr ?? ""}`, { cause: result.error });
      const observation = JSON.parse(result.stdout);
      expect(observation.mode).toBe(mode);
      if (mode === "windows-cross") {
        expect(observation).toEqual({ mode, rejectedBeforeWrites: true, downloads: 0, buildStarts: 0 });
      }
    });
  }
});
