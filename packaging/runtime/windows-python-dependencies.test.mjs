import { describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

const outputRoot = path.resolve("out/tests/packaging/runtime/windows-python-dependencies");
const runId = randomUUID();
const fixtureRunRoot = path.join(outputRoot, "workspace", runId);
const artifactsRunRoot = path.join(outputRoot, "artifacts", runId);
for (const kind of ["workspace", "artifacts"]) {
  mkdirSync(path.join(outputRoot, kind), { recursive: true });
  mkdirSync(path.join(outputRoot, kind, runId));
}

async function observeDependencies(moduleUrl, scenario) {
  const { default: assert } = await import("node:assert/strict");
  const fs = await import("node:fs");
  const { default: path } = await import("node:path");
  const projectRoot = process.cwd();
  process.env.BOXTEAM_PROJECT_ROOT = projectRoot;
  fs.writeFileSync(path.join(projectRoot, "package.json"), '{"version":"fixture"}\n');
  const outputRoot = path.join(projectRoot, "output");
  const runtimePackageRoot = path.join(projectRoot, "runtime");
  const pythonExecutable = path.join(runtimePackageRoot, "python", "python.exe");
  const sitePackagesRoot = path.join(runtimePackageRoot, "python", "Lib", "site-packages");
  const requirementsPath = path.join(outputRoot, "requirements.locked.txt");
  const crossRequirementsPath = path.join(outputRoot, "requirements.cross.locked.txt");
  const cleanupDirectories = [];
  const originalReaddir = fs.default.readdirSync;
  // 只观测独立Node中的真实目录枚举；命令仍是替身，不安装任何依赖。
  fs.default.readdirSync = (directory, options) => {
    const entries = originalReaddir(directory, options);
    if (options?.withFileTypes && (directory === sitePackagesRoot || directory.startsWith(`${sitePackagesRoot}${path.sep}`))) {
      cleanupDirectories.push(path.relative(sitePackagesRoot, directory).split(path.sep).join("/") || ".");
    }
    return entries;
  };
  const { syncBuiltinESMExports } = await import("node:module");
  syncBuiltinESMExports();
  const { installWindowsPythonDependencies } = await import(moduleUrl);
  const { PYTHON_RUNTIME_WINDOWS_X64 } = await import(new URL("./versions.mjs", moduleUrl));
  const write = (relative, body) => {
    const file = path.join(sitePackagesRoot, relative);
    fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, body);
  };
  fs.mkdirSync(outputRoot, { recursive: true });
  write("preexisting.so", "native build leaves package content alone");
  const header = ["# 导出的固定依赖", "--index-url https://example.invalid/simple"];
  const sourceBlocks = [
    "commentjson==0.9.0", "    # via fixture", "lark_parser==0.12.0", "    # via fixture",
    "litellm==1.2.0", "    # via fixture",
  ];
  const targetBlock = ["requests==2.3.4 \\", "    --hash=sha256:fixture", "    # via fixture", ""];
  const locked = [...header, ...sourceBlocks, ...targetBlock].join("\r\n");
  const oldPackageFiles = ["commentjson/__init__.py", "commentjson-0.9.0.dist-info/METADATA",
    "lark/__init__.py", "lark_parser-0.12.0.dist-info/METADATA", "litellm/__init__.py",
    "litellm-1.2.0.dist-info/METADATA"];
  const calls = [], failureMarker = new Error(`injected ${scenario}`);
  let failure;
  try {
    const result = installWindowsPythonDependencies({ pythonExecutable, outputRoot, runtimePackageRoot,
      crossBuild: scenario !== "native", run(command, args) {
        calls.push([command, args]);
        if (args[0] === "export") {
          if (scenario === "export-failure") throw failureMarker;
          fs.writeFileSync(requirementsPath, locked);
        } else if (args.includes("--no-build")) {
          for (const file of oldPackageFiles) write(file, "old wheel");
          write("litellm_extra/__init__.py", "unrelated package");
          write("litellm_extra-1.0.0.dist-info/METADATA", "unrelated metadata");
          write("other/native.pyd", "Windows extension");
          if (scenario === "target-failure") throw failureMarker;
        } else if (args.includes("--no-deps")) {
          for (const file of oldPackageFiles) assert.equal(fs.existsSync(path.join(sitePackagesRoot, file)), false);
          assert.equal(fs.existsSync(path.join(sitePackagesRoot, "lark")), false);
          assert.equal(fs.readFileSync(path.join(sitePackagesRoot, "litellm_extra/__init__.py"), "utf8"), "unrelated package");
          assert.equal(fs.readFileSync(path.join(sitePackagesRoot, "litellm_extra-1.0.0.dist-info/METADATA"), "utf8"), "unrelated metadata");
          if (scenario === "source-failure") throw failureMarker;
          for (const root of ["commentjson", "lark", "litellm"]) {
            write(`${root}/nested/native.so`, "host extension");
            write(`${root}/nested/module.py`, "source body");
            write(`${root}/nested/native.pyd`, "target extension");
          }
          if (scenario === "unexpected-native") {
            write("unrelated/native.so", "must reject");
            write("litellm_extra/nested/native.so", "similar package name must not be removed");
          }
          fs.rmSync(path.join(sitePackagesRoot, "preexisting.so"));
        }
      },
    });
    assert.equal(result, undefined);
  } catch (error) { failure = error; }
  const exportCommand = ["uv", ["export", "--locked", "--no-dev", "--format", "requirements-txt", "--output-file", requirementsPath]];
  const targetCommand = ["uv", ["pip", "install", "--target", sitePackagesRoot, "--python-version",
    PYTHON_RUNTIME_WINDOWS_X64.version, "--python-platform", "x86_64-pc-windows-msvc", "--no-build", "--requirements", crossRequirementsPath]];
  const sourceCommand = ["uv", ["pip", "install", "--target", sitePackagesRoot, "--no-deps",
    "commentjson==0.9.0", "lark_parser==0.12.0", "litellm==1.2.0"]];
  if (scenario === "native") {
    assert.equal(failure, undefined);
    assert.deepEqual(calls, [exportCommand, ["uv", ["pip", "install", "--python", pythonExecutable, "--requirements", requirementsPath]]]);
    assert.equal(fs.existsSync(crossRequirementsPath), false);
    assert.equal(fs.readFileSync(path.join(sitePackagesRoot, "preexisting.so"), "utf8"), "native build leaves package content alone");
  } else {
    const count = scenario === "export-failure" ? 1 : scenario === "target-failure" ? 2 : 3;
    assert.deepEqual(calls, [exportCommand, targetCommand, sourceCommand].slice(0, count));
    if (scenario.endsWith("-failure")) assert.equal(failure, failureMarker);
    else if (scenario === "unexpected-native") {
      assert.match(failure?.message ?? "", /Windows 目标 Python 包含 Linux 原生扩展/);
      assert(failure.message.includes(path.join(sitePackagesRoot, "unrelated/native.so")));
      assert(failure.message.includes(path.join(sitePackagesRoot, "litellm_extra/nested/native.so")));
      assert.equal(fs.existsSync(path.join(sitePackagesRoot, "unrelated/native.so")), true);
      assert.equal(fs.existsSync(path.join(sitePackagesRoot, "litellm_extra/nested/native.so")), true);
    } else assert.equal(failure, undefined);
    if (scenario !== "export-failure") assert.equal(fs.readFileSync(crossRequirementsPath, "utf8"), [...header, ...targetBlock].join("\n") + "\n");
    if (["cross", "unexpected-native"].includes(scenario)) {
      for (const root of ["commentjson", "lark", "litellm"]) {
        assert.equal(fs.existsSync(path.join(sitePackagesRoot, root, "nested/native.so")), false);
        assert.equal(fs.readFileSync(path.join(sitePackagesRoot, root, "nested/module.py"), "utf8"), "source body");
        assert.equal(fs.readFileSync(path.join(sitePackagesRoot, root, "nested/native.pyd"), "utf8"), "target extension");
      }
      assert.equal(fs.readFileSync(path.join(sitePackagesRoot, "other/native.pyd"), "utf8"), "Windows extension");
    }
  }
  if (["cross", "unexpected-native"].includes(scenario)) {
    const expectedDirectories = [".", "litellm_extra", "litellm_extra-1.0.0.dist-info", "other",
      ...["commentjson", "lark", "litellm"].flatMap((root) => [root, `${root}/nested`])];
    if (scenario === "unexpected-native") expectedDirectories.push("unrelated", "litellm_extra/nested");
    assert.deepEqual([...new Set(cleanupDirectories)].sort(), expectedDirectories.sort());
  } else assert.deepEqual(cleanupDirectories, []);
  return { scenario, commands: calls.length, cleanupDirectories };
}

async function observeBuilderDependencyCall(builderUrl, mode) {
  const { default: assert } = await import("node:assert/strict");
  const fs = await import("node:fs");
  const { default: path } = await import("node:path");
  const { createContext, SourceTextModule, SyntheticModule } = await import("node:vm");
  const projectRoot = process.cwd();
  process.env.BOXTEAM_PROJECT_ROOT = projectRoot;
  fs.writeFileSync(path.join(projectRoot, "package.json"), '{"version":"fixture"}\n');
  fs.writeFileSync(path.join(projectRoot, "pyproject.toml"), "fixture");
  for (const relative of ["app", "configs", "resources", "src/shared", "src/clients/web",
    "src/workspace-services/protocol",
    "src/workspace-services/terminal/server", "src/workspace-services/terminal/protocol",
    "src/workspace-services/terminal/shared",
    "src/workspace-services/browser/server", "src/workspace-services/browser/protocol",
    "packages/launcher", "packaging/runtime"]) {
    fs.mkdirSync(path.join(projectRoot, relative), { recursive: true });
    fs.writeFileSync(path.join(projectRoot, relative, "fixture.txt"), relative);
  }
  for (const [relative, content] of [
    ["bun.lock", "fixture lock\n"], ["uv.lock", "fixture lock\n"],
    ["packages/runtime-windows-x64/package.json", '{"version":"fixture"}\n'],
    ["scripts/release/package-windows-x64.mjs", "export {};\n"],
    ["scripts/install/runtime-postinstall.mjs", "export {};\n"],
  ]) {
    const file = path.join(projectRoot, relative);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, content);
  }
  const outputRoot = path.join(projectRoot, "out/packaging/windows-x64");
  const runtimePackageRoot = path.join(outputRoot, "stage/runtime-windows-x64/package");
  const pythonExecutable = path.join(runtimePackageRoot, "python/python.exe");
  const stop = new Error("fixture在Python依赖准备边界结束");
  const commands = [];
  let installations = 0;
  const context = createContext({ console, Buffer, process: {
    env: { BOXTEAM_PROJECT_ROOT: projectRoot }, cwd: () => projectRoot, arch: "x64",
    platform: mode === "native" ? "win32" : "linux", argv: mode === "cross" ? ["--cross"] : [],
  } });
  // 执行原构建入口全文及真实最小源码复制；下载、解包和依赖安装均停在明确边界。
  const module = new SourceTextModule(fs.readFileSync(new URL(builderUrl), "utf8"), { identifier: builderUrl, context });
  await module.link(async (specifier) => {
    let exports;
    if (specifier === "./pinned-archive-download.mjs") exports = {
      downloadPinnedWindowsPython: async () => path.join(projectRoot, "python.tgz"),
      downloadPinnedWindowsNode: async () => path.join(projectRoot, "node.zip"),
    };
    else if (specifier === "./windows-python-dependencies.mjs") exports = {
      installWindowsPythonDependencies(options) {
        installations++;
        assert.equal(options.pythonExecutable, pythonExecutable);
        assert.equal(options.outputRoot, outputRoot);
        assert.equal(options.runtimePackageRoot, runtimePackageRoot);
        assert.equal(options.crossBuild, mode === "cross");
        assert.equal(options.run("fixture-run", ["argument"], { capture: true }), "fixture output");
        throw stop;
      },
    };
    else {
      exports = { ...await import(specifier.startsWith(".") ? new URL(specifier, builderUrl).href : specifier) };
      if (specifier === "node:child_process") exports.spawnSync = (command, args, options) => {
        commands.push([command, [...args], options.cwd]);
        if (command === "tar") {
          fs.mkdirSync(path.dirname(pythonExecutable), { recursive: true }); fs.writeFileSync(pythonExecutable, "fixture");
        }
        return { status: 0, stdout: "fixture output\n" };
      };
    }
    return new SyntheticModule(Object.keys(exports), function () {
      for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
    }, { context });
  });
  await assert.rejects(module.evaluate(), (error) => error === stop);
  assert.equal(installations, 1);
  assert.deepEqual(commands, [
    ["tar", ["-xzf", path.join(projectRoot, "python.tgz"), "-C", runtimePackageRoot], projectRoot],
    ["fixture-run", ["argument"], projectRoot],
  ]);
  assert.equal(fs.readFileSync(path.join(runtimePackageRoot, "application/resources/fixture.txt"), "utf8"), "resources");
  assert.equal(fs.readFileSync(path.join(runtimePackageRoot, "application/src/workspace-services/terminal/shared/fixture.txt"), "utf8"), "src/workspace-services/terminal/shared");
  return { scenario: mode, commands: commands.length };
}

function runObservation(observer, source, group, scenario) {
  const fixtureGroup = path.join(fixtureRunRoot, group);
  const artifactsGroup = path.join(artifactsRunRoot, group);
  mkdirSync(fixtureGroup, { recursive: true });
  mkdirSync(artifactsGroup, { recursive: true });
  const scenarioRoot = path.join(fixtureGroup, scenario);
  const artifacts = path.join(artifactsGroup, scenario);
  mkdirSync(scenarioRoot);
  mkdirSync(artifacts);
  const fixtureRoot = path.join(scenarioRoot, "依赖 fixture");
  mkdirSync(fixtureRoot);
  const args = ["--experimental-vm-modules", "--input-type=module", "--eval",
    `${observer.toString()}\nconsole.log(JSON.stringify(await ${observer.name}(...JSON.parse(process.argv[1]))));`,
    JSON.stringify([new URL(source, import.meta.url).href, scenario]),
  ];
  const run = spawnSync("node", args, { cwd: fixtureRoot, encoding: "utf8", timeout: 5000 });
  writeFileSync(path.join(artifacts, "stdout.raw"), run.stdout ?? "", { flag: "wx" });
  writeFileSync(path.join(artifacts, "stderr.raw"), run.stderr ?? "", { flag: "wx" });
  writeFileSync(path.join(artifacts, "result.json"), `${JSON.stringify({
    command: ["node", ...args], cwd: fixtureRoot, exitCode: run.status, signal: run.signal,
    error: run.error ? { name: run.error.name, message: run.error.message, code: run.error.code } : null,
  }, null, 2)}\n`, { flag: "wx" });
  if (run.error || run.status !== 0) throw new Error(`Windows依赖探针失败: exit=${run.status} signal=${run.signal ?? "none"}\n${run.stderr ?? ""}`, { cause: run.error });
  return JSON.parse(run.stdout);
}

describe("Windows Python依赖准备的唯一owner", () => {
  for (const scenario of ["native", "cross", "export-failure", "target-failure", "source-failure", "unexpected-native"]) {
    test(`${scenario}保留锁定依赖、命令顺序与文件边界`, () => {
      const observation = runObservation(observeDependencies, "./windows-python-dependencies.mjs", "dependency-owner", scenario);
      expect(observation.scenario).toBe(scenario);
      expect(observation.cleanupDirectories).toHaveLength(new Set(observation.cleanupDirectories).size);
    });
  }
  for (const mode of ["native", "cross"]) {
    test(`真实构建入口/${mode}传入目录、模式与原命令runner`, () => {
      expect(runObservation(observeBuilderDependencyCall, "./build-windows-x64.mjs", "builder-wiring", mode).scenario).toBe(mode);
    });
  }
});
