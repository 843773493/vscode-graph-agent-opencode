import { describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, symlinkSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";

import { beginRuntimeBuild, publishRuntimeBuildResult } from "./runtime-build-result.mjs";
import { computeBuildInputFingerprint } from "./build-input-fingerprint.mjs";

const outputRoot = path.resolve("out/tests/packaging/runtime/build-input-fingerprint");
const runId = randomUUID();
const fixtureRunRoot = path.join(outputRoot, "workspace", runId);
const artifactsRunRoot = path.join(outputRoot, "artifacts", runId);
for (const kind of ["workspace", "artifacts"]) {
  mkdirSync(path.join(outputRoot, kind), { recursive: true });
  mkdirSync(path.join(outputRoot, kind, runId));
}
const moduleUrl = new URL("./build-input-fingerprint.mjs", import.meta.url).href;
const golden = {
  "linux-x64": "3b482d565584aecdea7cb97a5a95d0f45a21ff39ae79c53a61e8d9b74003a94c",
  "windows-x64": "6686eef680ffcd549d2d69ceb1cc60dd0cfc771bd195606e328bb461b0b01f07",
};

function sourceFixture(targetPlatform, scenario) {
  const platformRoot = path.join(fixtureRunRoot, targetPlatform);
  mkdirSync(platformRoot, { recursive: true });
  const scenarioRoot = path.join(platformRoot, scenario);
  mkdirSync(scenarioRoot);
  const root = path.join(scenarioRoot, "源码 工作区");
  const inputs = [
    ["package.json", "{}\n"], ["bun.lock", "锁\n"],
    ["uv.lock", "python-lock\n"], ["pyproject.toml", "[project]\n"],
    ["app/零 空.py", "print('你好')\n"], ["app/empty.dat", ""],
    ["app/payload.bin", Buffer.alloc(1024 * 1024 + 7, "a")],
    ["configs/input.jsonc", "{}\n"], ["resources/input.txt", "resource\n"],
    ["src/shared/sse.js", "export const stream = true;\n"],
    ["src/shared/sseRuntimeValidators.js", "export const validate = () => true;\n"],
    ["src/clients/web/input.ts", "export {};\n"],
    ["src/workspace-services/input.mjs", "export {};\n"],
    ["packages/launcher/input.mjs", "export {};\n"],
    ["packaging/runtime/input.mjs", "export {};\n"],
    ["scripts/install/runtime-postinstall.mjs", "// install\n"],
  ];
  for (const platform of Object.keys(golden)) {
    inputs.push([`packages/runtime-${platform}/package.json`, `${platform}\n`]);
    inputs.push([`scripts/release/package-${platform}.mjs`, `// ${platform}\n`]);
  }
  // 逆序落盘，以免测试仅依赖目录插入顺序；不复制仓库或创建业务数据。
  for (const [relative, bytes] of inputs.reverse()) {
    const file = path.join(root, relative);
    mkdirSync(path.dirname(file), { recursive: true });
    writeFileSync(file, bytes);
  }
  return root;
}

function observeFileReads(projectRoot, targetPlatform, scenario) {
  const source = `
    import fs from "node:fs";
    import path from "node:path";
    import { syncBuiltinESMExports } from "node:module";
    const [moduleUrl, projectRoot, targetPlatform, scenario] = JSON.parse(process.argv[1]);
    const original = { ...fs }, opened = new Map(), buffers = new Set(), enumeratedPaths = new Set();
    const failure = Object.assign(new Error("injected " + scenario), { code: "EIO" });
    const observation = { fullReads: 0, opens: 0, closes: 0, reads: 0, maxRequestedBytes: 0,
      metadataStats: 0, redundantMetadataStats: 0, enumeratedEntries: 0 };
    const isInput = (file) => typeof file === "string" && file.startsWith(projectRoot + path.sep);
    fs.readdirSync = (directory, ...args) => {
      const entries = original.readdirSync(directory, ...args);
      if (isInput(directory)) {
        for (const entry of entries) {
          enumeratedPaths.add(path.join(directory, entry.name));
          observation.enumeratedEntries++;
        }
      }
      return entries;
    };
    fs.lstatSync = (file, ...args) => {
      if (isInput(file)) {
        observation.metadataStats++;
        if (enumeratedPaths.has(file)) observation.redundantMetadataStats++;
      }
      return original.lstatSync(file, ...args);
    };
    fs.readFileSync = (file, ...args) => {
      if (isInput(file)) {
        observation.fullReads++;
        throw new Error("构建指纹不得整份读取输入正文");
      }
      return original.readFileSync(file, ...args);
    };
    fs.openSync = (file, ...args) => {
      if (isInput(file) && scenario === "open-error") throw failure;
      const fd = original.openSync(file, ...args);
      if (isInput(file)) { opened.set(fd, file); observation.opens++; }
      return fd;
    };
    fs.readSync = (fd, buffer, offset, length, position) => {
      if (opened.has(fd)) {
        observation.reads++;
        observation.maxRequestedBytes = Math.max(observation.maxRequestedBytes, length);
        buffers.add(buffer);
        if (scenario === "read-error") throw failure;
        if (scenario === "short-reads") length = Math.min(length, 301);
      }
      return original.readSync(fd, buffer, offset, length, position);
    };
    fs.closeSync = (fd) => {
      const input = opened.delete(fd);
      original.closeSync(fd);
      if (input) {
        observation.closes++;
        if (scenario === "close-error") throw failure;
      }
    };
    syncBuiltinESMExports();
    const { computeBuildInputFingerprint } = await import(moduleUrl);
    try { observation.digest = computeBuildInputFingerprint({ projectRoot, targetPlatform }); }
    catch (error) { observation.error = error.message; observation.originalError = error === failure; }
    observation.remainingDescriptors = opened.size;
    observation.bufferCount = buffers.size;
    console.log(JSON.stringify(observation));
  `;
  return runNodeObservation(projectRoot, targetPlatform, scenario, source);
}

function runNodeObservation(projectRoot, targetPlatform, scenario, source, nodeOptions = []) {
  const args = [
    ...nodeOptions,
    "--input-type=module", "--eval", source,
    JSON.stringify([moduleUrl, projectRoot, targetPlatform, scenario]),
  ];
  const result = spawnSync("node", args, { cwd: projectRoot, encoding: "utf8", timeout: 5000 });
  const platformArtifacts = path.join(artifactsRunRoot, targetPlatform);
  mkdirSync(platformArtifacts, { recursive: true });
  const scenarioArtifacts = path.join(platformArtifacts, scenario);
  mkdirSync(scenarioArtifacts);
  writeFileSync(path.join(scenarioArtifacts, "stdout.raw"), result.stdout ?? "", { flag: "wx" });
  writeFileSync(path.join(scenarioArtifacts, "stderr.raw"), result.stderr ?? "", { flag: "wx" });
  writeFileSync(path.join(scenarioArtifacts, "result.json"), `${JSON.stringify({
    command: ["node", ...args], cwd: projectRoot, exitCode: result.status, signal: result.signal,
    error: result.error ? { name: result.error.name, message: result.error.message, code: result.error.code } : null,
  }, null, 2)}\n`, { flag: "wx" });
  if (result.error || result.status !== 0) {
    throw new Error(`构建指纹探针失败: exit=${result.status} signal=${result.signal}\n${result.stderr ?? ""}`, { cause: result.error });
  }
  return JSON.parse(result.stdout);
}

function observeHostPathOrdering(projectRoot, targetPlatform) {
  const source = `
    import * as fs from "node:fs";
    import path from "node:path";
    import { createHash } from "node:crypto";
    import { SourceTextModule, SyntheticModule } from "node:vm";
    const [moduleUrl, projectRoot, targetPlatform] = JSON.parse(process.argv[1]);
    const productionSource = fs.readFileSync(new URL(moduleUrl), "utf8");
    const observations = {};
    for (const [host, hostPath] of [["posix", path.posix], ["windows", path.win32]]) {
      const hostRoot = host === "windows" ? "C:\\\\源码 工作区" : "/源码 工作区";
      const openedPaths = [];
      // 路径遵循模拟宿主，正文与Dirent仍来自同一份真实磁盘fixture。
      const relative = (file) => hostPath.relative(hostRoot, file);
      const realPath = (file) => path.join(projectRoot, ...relative(file).split(hostPath.sep));
      const filesystem = {
        lstatSync: (file) => fs.lstatSync(realPath(file)),
        readdirSync: (file, options) => fs.readdirSync(realPath(file), options),
        readlinkSync: (file) => fs.readlinkSync(realPath(file)),
        openSync: (file, flags) => {
          openedPaths.push(relative(file).split(hostPath.sep).join("/"));
          return fs.openSync(realPath(file), flags);
        },
        readSync: fs.readSync,
        closeSync: fs.closeSync,
      };
      const dependencies = {
        "node:fs": filesystem,
        "node:path": { default: hostPath },
        "node:crypto": { createHash },
      };
      const module = new SourceTextModule(productionSource, { identifier: moduleUrl });
      await module.link((specifier) => {
        const exports = dependencies[specifier];
        if (!exports) throw new Error("未预期的构建指纹依赖: " + specifier);
        return new SyntheticModule(Object.keys(exports), function () {
          for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
        });
      });
      await module.evaluate();
      const digest = module.namespace.computeBuildInputFingerprint({ projectRoot: hostRoot, targetPlatform });
      observations[host] = { digest, openedPaths };
    }
    console.log(JSON.stringify(observations));
  `;
  return runNodeObservation(projectRoot, targetPlatform, "cross-host-paths", source, ["--experimental-vm-modules"]);
}

describe("构建输入指纹的文件读取", () => {
  for (const targetPlatform of Object.keys(golden)) {
    test(`${targetPlatform}在POSIX和Windows路径规则下使用相同顺序及指纹`, () => {
      const projectRoot = sourceFixture(targetPlatform, "cross-host-paths");
      for (const relative of ["app/a/nested.py", "app/a0.py", "app/树/叶.py", "app/树0.py"]) {
        const file = path.join(projectRoot, relative);
        mkdirSync(path.dirname(file), { recursive: true });
        writeFileSync(file, `# ${relative}\n`);
      }
      const result = observeHostPathOrdering(projectRoot, targetPlatform);
      expect(result.windows.openedPaths).toEqual(result.posix.openedPaths);
      expect(result.windows.digest).toBe(result.posix.digest);
      expect(computeBuildInputFingerprint({ projectRoot, targetPlatform })).toBe(result.posix.digest);
    });

    test(`${targetPlatform}保留既有路径、正文及分隔符指纹`, () => {
      const projectRoot = sourceFixture(targetPlatform, "golden-format");
      expect(computeBuildInputFingerprint({ projectRoot, targetPlatform })).toBe(golden[targetPlatform]);
      mkdirSync(path.join(projectRoot, "app/out"));
      writeFileSync(path.join(projectRoot, "app/out/ignored.bin"), "ignored");
      expect(computeBuildInputFingerprint({ projectRoot, targetPlatform })).toBe(golden[targetPlatform]);
      writeFileSync(path.join(projectRoot, "app/零 空.py"), "changed");
      expect(computeBuildInputFingerprint({ projectRoot, targetPlatform })).not.toBe(golden[targetPlatform]);
    });

    test(`${targetPlatform}的shared源码变化会改变构建指纹`, () => {
      const projectRoot = sourceFixture(targetPlatform, "shared-source-change");
      const initialFingerprint = computeBuildInputFingerprint({ projectRoot, targetPlatform });
      writeFileSync(
        path.join(projectRoot, "src/shared/sseRuntimeValidators.js"),
        "export const validate = () => false;\n",
      );
      expect(computeBuildInputFingerprint({ projectRoot, targetPlatform })).not.toBe(initialFingerprint);
    });

    test(`${targetPlatform}只忽略构建工具及Launcher的开发文件并保留产品输入`, () => {
      const projectRoot = sourceFixture(targetPlatform, "input-exclusions");
      const fingerprint = () => computeBuildInputFingerprint({ projectRoot, targetPlatform });
      const initialFingerprint = fingerprint();
      const runtimeInstructionsPath = path.join(projectRoot, "packaging/runtime/AGENTS.md");
      const runtimeTestPath = path.join(projectRoot, "packaging/runtime/build-input-fingerprint.test.mjs");

      writeFileSync(runtimeInstructionsPath, "旧构建目录说明\n");
      writeFileSync(runtimeTestPath, "旧测试正文\n");
      expect(fingerprint()).toBe(initialFingerprint);
      writeFileSync(runtimeInstructionsPath, "更新构建目录说明\n");
      writeFileSync(runtimeTestPath, "更新测试正文\n");
      expect(fingerprint()).toBe(initialFingerprint);

      const testNamedDirectory = path.join(projectRoot, "packaging/runtime/fixture.test.mjs");
      mkdirSync(testNamedDirectory, { recursive: true });
      writeFileSync(path.join(testNamedDirectory, "retained.mjs"), "目录中的真实构建输入\n");
      const withTestNamedDirectory = fingerprint();
      expect(withTestNamedDirectory).not.toBe(initialFingerprint);
      writeFileSync(path.join(testNamedDirectory, "retained.mjs"), "目录中的更新构建输入\n");
      expect(fingerprint()).not.toBe(withTestNamedDirectory);

      const beforeLauncherDevelopmentFiles = fingerprint();
      for (const relative of ["packages/launcher/AGENTS.md", "packages/launcher/src/nested/AGENTS.md", "packages/launcher/src/nested/runtime.test.mjs"]) {
        const file = path.join(projectRoot, relative);
        mkdirSync(path.dirname(file), { recursive: true });
        writeFileSync(file, "Launcher开发输入\n");
        expect(fingerprint()).toBe(beforeLauncherDevelopmentFiles);
        writeFileSync(file, "更新Launcher开发输入\n");
        expect(fingerprint()).toBe(beforeLauncherDevelopmentFiles);
      }
      const launcherDirectoryFile = path.join(projectRoot, "packages/launcher/src/fixture.test.mjs/真实 模块.mjs");
      mkdirSync(path.dirname(launcherDirectoryFile), { recursive: true });
      writeFileSync(launcherDirectoryFile, "Launcher产品正文\n");
      const withLauncherDirectory = fingerprint();
      expect(withLauncherDirectory).not.toBe(beforeLauncherDevelopmentFiles);
      writeFileSync(launcherDirectoryFile, "更新Launcher产品正文\n");
      expect(fingerprint()).not.toBe(withLauncherDirectory);

      const applicationInstructionsPath = path.join(projectRoot, "app/AGENTS.md");
      writeFileSync(applicationInstructionsPath, "发行应用目录说明\n");
      const withApplicationInstructions = fingerprint();
      expect(withApplicationInstructions).not.toBe(initialFingerprint);
      writeFileSync(applicationInstructionsPath, "更新发行应用目录说明\n");
      expect(fingerprint()).not.toBe(withApplicationInstructions);

      const applicationTestPath = path.join(projectRoot, "app/runtime.test.mjs");
      writeFileSync(applicationTestPath, "发行应用目录测试\n");
      const withApplicationTest = fingerprint();
      expect(withApplicationTest).not.toBe(withApplicationInstructions);
      writeFileSync(applicationTestPath, "更新发行应用目录测试\n");
      expect(fingerprint()).not.toBe(withApplicationTest);

      const buildOwnerPath = path.join(
        projectRoot,
        "packaging/runtime",
        targetPlatform === "linux-x64" ? "build-linux-x64.mjs" : "build-windows-x64.mjs",
      );
      writeFileSync(buildOwnerPath, "原构建入口\n");
      const withBuildOwner = fingerprint();
      writeFileSync(buildOwnerPath, "更新构建入口\n");
      expect(fingerprint()).not.toBe(withBuildOwner);

      const buildResultPath = path.join(projectRoot, "out/packaging", targetPlatform, "build-result.json");
      mkdirSync(path.dirname(buildResultPath), { recursive: true });
      writeFileSync(buildResultPath, "stale success\n");
      const startingSourceFingerprint = beginRuntimeBuild({ projectRoot, targetPlatform, buildResultPath });
      expect(existsSync(buildResultPath)).toBe(false);
      writeFileSync(buildOwnerPath, "构建期间再次更新入口\n");
      expect(() => publishRuntimeBuildResult({
        projectRoot,
        targetPlatform,
        buildResultPath,
        startingSourceFingerprint,
        metadata: { platform: targetPlatform, version: "fixture", install_tarballs: {} },
      })).toThrow(/构建期间输入源码发生变化/);
      expect(existsSync(buildResultPath)).toBe(false);
    });

    test(`${targetPlatform}只发布构建起始版本，shared源码变化时拒绝成功metadata`, () => {
      const projectRoot = sourceFixture(targetPlatform, "build-publish");
      const buildResultPath = path.join(projectRoot, "out/packaging", targetPlatform, "build-result.json");
      mkdirSync(path.dirname(buildResultPath), { recursive: true });
      writeFileSync(buildResultPath, "stale success\n");
      const startingSourceFingerprint = beginRuntimeBuild({ projectRoot, targetPlatform, buildResultPath });
      expect(existsSync(buildResultPath)).toBe(false);
      publishRuntimeBuildResult({
        projectRoot,
        targetPlatform,
        buildResultPath,
        startingSourceFingerprint,
        metadata: { platform: targetPlatform, version: "fixture", install_tarballs: {} },
      });
      expect(JSON.parse(readFileSync(buildResultPath, "utf8")).source_fingerprint).toBe(startingSourceFingerprint);

      const changedSourceRoot = sourceFixture(targetPlatform, "source-changed-during-build");
      const changedBuildResultPath = path.join(changedSourceRoot, "out/packaging", targetPlatform, "build-result.json");
      mkdirSync(path.dirname(changedBuildResultPath), { recursive: true });
      writeFileSync(changedBuildResultPath, "stale success\n");
      const changedStartingFingerprint = beginRuntimeBuild({
        projectRoot: changedSourceRoot,
        targetPlatform,
        buildResultPath: changedBuildResultPath,
      });
      writeFileSync(
        path.join(changedSourceRoot, "src/shared/sseRuntimeValidators.js"),
        "export const validate = () => changed;\n",
      );
      expect(() => publishRuntimeBuildResult({
        projectRoot: changedSourceRoot,
        targetPlatform,
        buildResultPath: changedBuildResultPath,
        startingSourceFingerprint: changedStartingFingerprint,
        metadata: { platform: targetPlatform, version: "fixture", install_tarballs: {} },
      })).toThrow(/构建期间输入源码发生变化/);
      expect(existsSync(changedBuildResultPath)).toBe(false);
    });

    for (const scenario of ["bounded", "short-reads"]) {
      test(`${targetPlatform}/${scenario}复用有限缓冲区，短读取不丢失正文`, () => {
        const result = observeFileReads(sourceFixture(targetPlatform, scenario), targetPlatform, scenario);
        expect(result.error).toBeUndefined();
        expect(result.digest).toBe(golden[targetPlatform]);
        expect(result.fullReads).toBe(0);
        expect(result.bufferCount).toBe(1);
        expect(result.maxRequestedBytes).toBeLessThanOrEqual(64 * 1024);
        expect(result.reads).toBeGreaterThan(16);
        expect(result.opens).toBe(18);
        expect(result.closes).toBe(18);
        expect(result.remainingDescriptors).toBe(0);
        if (scenario === "bounded") {
          expect(result.metadataStats).toBeGreaterThan(0);
          expect(result.enumeratedEntries).toBeGreaterThan(0);
          expect(result.redundantMetadataStats).toBe(0);
        }
      });
    }

    test.skipIf(process.platform === "win32")(`${targetPlatform}保留链接目标，不遍历链接正文或要求目标存在`, () => {
      const projectRoot = sourceFixture(targetPlatform, "symbolic-links");
      const externalRoot = path.join(projectRoot, "外部 来源");
      mkdirSync(externalRoot);
      writeFileSync(path.join(externalRoot, "payload.txt"), "原始正文");
      const fileLink = path.join(projectRoot, "app/file-link");
      symlinkSync("../外部 来源/payload.txt", fileLink);
      symlinkSync("../外部 来源", path.join(projectRoot, "app/directory-link"), "dir");
      symlinkSync("../不存在的目标", path.join(projectRoot, "app/dangling-link"));
      const initialFingerprint = computeBuildInputFingerprint({ projectRoot, targetPlatform });
      writeFileSync(path.join(externalRoot, "payload.txt"), "链接正文变化不属于构建输入");
      expect(computeBuildInputFingerprint({ projectRoot, targetPlatform })).toBe(initialFingerprint);
      unlinkSync(fileLink);
      symlinkSync("../外部 来源/另一个目标", fileLink);
      expect(computeBuildInputFingerprint({ projectRoot, targetPlatform })).not.toBe(initialFingerprint);
    });
  }

  for (const scenario of ["open-error", "read-error", "close-error"]) {
    test(`${scenario}保留异常且不遗留已取得的描述符`, () => {
      const result = observeFileReads(sourceFixture("linux-x64", scenario), "linux-x64", scenario);
      expect(result.originalError).toBe(true);
      expect(result.digest).toBeUndefined();
      expect(result.closes).toBe(scenario === "open-error" ? 0 : 1);
      expect(result.remainingDescriptors).toBe(0);
    });
  }
});
